"""API FastAPI : ingestion, question/reponse (JSON ou streaming SSE), sante.

Toutes les routes sont sous /api ; si frontend/dist existe (npm run build), l'interface React
est servie a la racine par le meme serveur."""
import json
import logging
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessageChunk
from pydantic import BaseModel, Field
from sqlalchemy import text

from app import audit
from app.admission import BUSY_MESSAGE, QueueFull, get_admission
from app.config import get_settings
from app.loaders import DOCUMENT_EXTENSIONS, TABULAR_EXTENSIONS
from app.observability import run_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

logger = logging.getLogger(__name__)
_ready = threading.Event()


def _preload() -> None:
    """Charge tout avant d'accepter du trafic : une nouvelle version ne recoit des questions qu'une
    fois prete. En cas d'echec (base arretee...), on demarre quand meme : /api/health le signalera."""
    from app.graph import get_graph
    from app.llm import get_chat_model
    from app.retriever import get_cross_encoder
    from app.vectorstore import get_vectorstore

    try:
        get_vectorstore()
        get_cross_encoder()
        get_graph()
        for attempt in range(10):  # le conteneur Ollama peut demarrer apres celui de l'API
            try:
                get_chat_model().invoke("Reponds uniquement : ok")
                break
            except Exception:
                if attempt == 9:
                    raise
                time.sleep(3)
    except Exception:
        logger.exception("Prechargement incomplet")
    finally:
        _ready.set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if get_settings().preload_models:
        threading.Thread(target=_preload, daemon=True).start()
    else:
        _ready.set()
    yield


app = FastAPI(title="RAG agentique V2", version="2.0.0", lifespan=lifespan)
api = APIRouter(prefix="/api")

STREAMED_NODES = {"generate", "answer_sql", "chat"}
ALLOWED_EXTENSIONS = DOCUMENT_EXTENSIONS | TABULAR_EXTENSIONS
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
SHARED_KEY_USER = "cle-partagee"  # pas encore d'identite individuelle


@app.exception_handler(QueueFull)
def queue_full(_, __) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": BUSY_MESSAGE}, headers={"Retry-After": "30"})


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
    request_id: str
    sources: list[dict] = []
    sql: str | None = None


@api.get("/live")
async def live():
    """Le processus repond. Asynchrone : reste disponible quand les threads attendent le LLM."""
    return {"status": "ok"}


@api.get("/ready")
async def ready():
    if not _ready.is_set():
        raise HTTPException(status_code=503, detail="Chargement des modeles en cours")
    return {"status": "ok"}


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
    with get_admission().slot():
        # cleanup='full' retire de l'index les chunks du fichier supprime.
        result = ingest_directory(cleanup="full")
    audit.record("document_delete", "ok", user_id=SHARED_KEY_USER, details={"file": path.name})
    return result


@api.post("/ingest", dependencies=[Depends(require_api_key)])
def ingest(full_cleanup: bool = False):
    from app.ingestion import ingest_directory

    with get_admission().slot():
        return ingest_directory(cleanup="full" if full_cleanup else "incremental")


@api.post("/upload", dependencies=[Depends(require_api_key)])
def upload(files: list[UploadFile] = File(...)):
    from app.ingestion import ingest_directory

    s = get_settings()
    for f in files:
        name = Path(f.filename or "").name
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Extension non supportee : {name}")
        if (f.size or 0) > s.max_upload_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"Fichier trop volumineux (max {s.max_upload_mb} Mo) : {name}")

    target = _documents_dir()
    saved = []
    for f in files:
        name = Path(f.filename or "").name
        with open(target / name, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(name)
    with get_admission().slot():
        result = ingest_directory(cleanup="incremental")
    audit.record("document_upload", "ok", user_id=SHARED_KEY_USER, details={"files": saved})
    return {"saved": saved, **result}


@api.get("/audit", dependencies=[Depends(require_api_key)])
def audit_log(limit: int = Query(default=50, ge=1, le=500)):
    return audit.recent(limit)


def _audit_question(request_id: str, thread_id: str, question: str, started: float, status: str, out: dict) -> None:
    audit.record(
        "question",
        status,
        request_id=request_id,
        user_id=SHARED_KEY_USER,
        thread_id=thread_id,
        duration_ms=int((time.monotonic() - started) * 1000),
        question=question,
        route=out.get("route"),
        sql_query=out.get("sql") or None,
        details={"sources": sorted({s.get("source") or "?" for s in out.get("sources", [])})},
    )


@api.post("/ask", response_model=AskResponse, dependencies=[Depends(require_api_key)])
def ask(req: AskRequest):
    from app.graph import get_graph, make_input

    thread_id = req.thread_id or str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    started = time.monotonic()
    status, out = "error", {}
    try:
        with get_admission().slot():
            out = get_graph().invoke(make_input(req.question), run_config(thread_id))
        status = "ok"
    except QueueFull:
        status = "refused"
        raise
    finally:
        _audit_question(request_id, thread_id, req.question, started, status, out)
    return AskResponse(
        answer=out.get("answer", ""),
        route=out.get("route", ""),
        thread_id=thread_id,
        request_id=request_id,
        sources=out.get("sources", []),
        sql=out.get("sql") or None,
    )


@api.post("/ask/stream", dependencies=[Depends(require_api_key)])
def ask_stream(req: AskRequest):
    """SSE : `step` (file d'attente ou noeud termine), `token` (generation), `done` (reponse finale) ou `error`."""
    from app.graph import get_graph, make_input

    thread_id = req.thread_id or str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    graph = get_graph()
    config = run_config(thread_id)

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def events():
        started = time.monotonic()
        status, final = "error", {}
        try:
            yield sse("step", {"node": "queue"})
            with get_admission().slot():
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
            status = "ok"
            yield sse("done", {
                "answer": final.get("answer", ""),
                "route": final.get("route", ""),
                "thread_id": thread_id,
                "request_id": request_id,
                "sources": final.get("sources", []),
                "sql": final.get("sql") or None,
            })
        except QueueFull:
            status = "refused"
            yield sse("error", {"message": BUSY_MESSAGE})
        except GeneratorExit:  # le navigateur a ferme la connexion
            status = "cancelled"
            raise
        except Exception as e:  # l'erreur est remontee a l'interface au lieu de couper le flux
            logging.exception("Erreur pendant /ask/stream")
            yield sse("error", {"message": str(e)[:300]})
        finally:
            _audit_question(request_id, thread_id, req.question, started, status, final)

    return StreamingResponse(events(), media_type="text/event-stream")


app.include_router(api)

if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
