"""API FastAPI : ingestion, question/reponse (JSON ou streaming SSE), sante.

Toutes les routes sont sous /api ; si frontend/dist existe (npm run build), l'interface React
est servie a la racine par le meme serveur."""
import json
import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessageChunk
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.config import get_settings
from app.loaders import DOCUMENT_EXTENSIONS, TABULAR_EXTENSIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

app = FastAPI(title="RAG agentique V2", version="2.0.0")
api = APIRouter(prefix="/api")

STREAMED_NODES = {"generate", "answer_sql", "chat"}
ALLOWED_EXTENSIONS = DOCUMENT_EXTENSIONS | TABULAR_EXTENSIONS
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def require_api_key(x_api_key: str | None = Header(default=None)):
    expected = get_settings().api_key
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Cle API invalide")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = Field(default=None, description="Identifiant de conversation (memoire)")


class AskResponse(BaseModel):
    answer: str
    route: str
    thread_id: str
    sources: list[dict] = []
    sql: str | None = None


@api.get("/health")
def health():
    from app.vectorstore import get_engine

    s = get_settings()
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        db = "ok"
    except Exception as e:
        db = f"error: {e.__class__.__name__}"
    model = s.openai_model if s.llm_provider == "openai" else s.ollama_model
    return {"database": db, "llm": s.llm_provider, "model": model, "embeddings": s.embedding_provider}


def _documents_dir() -> Path:
    path = Path(get_settings().documents_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


@api.get("/documents", dependencies=[Depends(require_api_key)])
def list_documents():
    files = [p for p in sorted(_documents_dir().iterdir()) if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS]
    return [
        {
            "name": p.name,
            "size": p.stat().st_size,
            "kind": "table" if p.suffix.lower() in TABULAR_EXTENSIONS else "document",
        }
        for p in files
    ]


@api.delete("/documents/{name}", dependencies=[Depends(require_api_key)])
def delete_document(name: str):
    from app.ingestion import ingest_directory

    path = _documents_dir() / Path(name).name  # Path(...).name : pas de traversee de dossier
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document introuvable")
    path.unlink()
    # cleanup='full' retire de l'index les chunks du fichier supprime.
    return ingest_directory(cleanup="full")


@api.post("/ingest", dependencies=[Depends(require_api_key)])
def ingest(full_cleanup: bool = False):
    from app.ingestion import ingest_directory

    return ingest_directory(cleanup="full" if full_cleanup else "incremental")


@api.post("/upload", dependencies=[Depends(require_api_key)])
def upload(files: list[UploadFile] = File(...)):
    from app.ingestion import ingest_directory

    target = _documents_dir()
    saved = []
    for f in files:
        name = Path(f.filename or "").name
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Extension non supportee : {name}")
        with open(target / name, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(name)
    return {"saved": saved, **ingest_directory(cleanup="incremental")}


def _run_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "run_name": "ask", "metadata": {"thread_id": thread_id}}


@api.post("/ask", response_model=AskResponse, dependencies=[Depends(require_api_key)])
def ask(req: AskRequest):
    from app.graph import get_graph, make_input

    thread_id = req.thread_id or str(uuid.uuid4())
    out = get_graph().invoke(make_input(req.question), _run_config(thread_id))
    return AskResponse(
        answer=out.get("answer", ""),
        route=out.get("route", ""),
        thread_id=thread_id,
        sources=out.get("sources", []),
        sql=out.get("sql") or None,
    )


@api.post("/ask/stream", dependencies=[Depends(require_api_key)])
def ask_stream(req: AskRequest):
    """SSE : `step` (noeud termine), `token` (generation), `done` (reponse finale) ou `error`."""
    from app.graph import get_graph, make_input

    thread_id = req.thread_id or str(uuid.uuid4())
    graph = get_graph()
    config = _run_config(thread_id)

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def events():
        try:
            for mode, chunk in graph.stream(make_input(req.question), config, stream_mode=["updates", "messages"]):
                if mode == "updates":
                    for node, update in chunk.items():
                        payload = {"node": node}
                        if node == "route" and update:
                            payload["route"] = update.get("route")
                        yield sse("step", payload)
                else:
                    msg, meta = chunk
                    # Seuls les fragments du LLM (AIMessageChunk) : le message complet ajoute a l'etat
                    # en fin de noeud est aussi emis par LangGraph et doublerait le texte.
                    if (isinstance(msg, AIMessageChunk) and meta.get("langgraph_node") in STREAMED_NODES
                            and msg.content):
                        yield sse("token", {"text": msg.content})
            final = graph.get_state(config).values
            yield sse("done", {
                "answer": final.get("answer", ""),
                "route": final.get("route", ""),
                "thread_id": thread_id,
                "sources": final.get("sources", []),
                "sql": final.get("sql") or None,
            })
        except Exception as e:  # l'erreur est remontee a l'interface au lieu de couper le flux
            logging.exception("Erreur pendant /ask/stream")
            yield sse("error", {"message": str(e)[:300]})

    return StreamingResponse(events(), media_type="text/event-stream")


app.include_router(api)

if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
