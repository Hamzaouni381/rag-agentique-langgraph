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

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessageChunk
from pydantic import BaseModel, Field
from sqlalchemy import text

from app import audit
from app.admission import BUSY_MESSAGE, QueueFull, get_admission
from app.auth import LOGOUT_URL, SERVICE_USER, User, require_admin, require_user, thread_key
from app.config import get_settings
from app.loaders import DOCUMENT_EXTENSIONS, TABULAR_EXTENSIONS
from app.observability import run_config
from app.tenancy import ensure_schema, ensure_tenant, tenant_dir

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

logger = logging.getLogger(__name__)
_ready = threading.Event()


def _preload() -> None:
    """Charge tout avant d'accepter du trafic : une nouvelle version ne recoit des questions qu'une
    fois prete. En cas d'echec (base arretee...), on demarre quand meme : /api/health le signalera."""
    from app.graph import get_graph
    from app.llm import get_chat_model
    from app.retriever import get_cross_encoder

    try:
        ensure_schema()
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


@app.exception_handler(QueueFull)
def queue_full(_, __) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": BUSY_MESSAGE}, headers={"Retry-After": "30"})


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


@api.get("/me")
def me(user: User = Depends(require_user)):
    return {
        "user": user.id,
        "roles": sorted(user.roles),
        "is_admin": user.is_admin,
        # La cle de service n'ouvre pas de session : rien a fermer.
        "logout_url": LOGOUT_URL if get_settings().auth_mode == "easyauth" and user != SERVICE_USER else None,
    }


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


def require_tenant(user: User = Depends(require_user)) -> User:
    """Toute route qui touche a des donnees passe ici : le proprietaire vient de l'identite verifiee,
    jamais de la requete."""
    ensure_tenant(user.owner)
    return user


def _documents_dir(user: User) -> Path:
    path = tenant_dir(user.owner)
    path.mkdir(parents=True, exist_ok=True)
    return path


@api.get("/documents")
def list_documents(user: User = Depends(require_tenant)):
    directory = _documents_dir(user)
    files = [p for p in sorted(directory.iterdir()) if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS]
    return [
        {
            "name": p.name,
            "size": p.stat().st_size,
            "kind": "table" if p.suffix.lower() in TABULAR_EXTENSIONS else "document",
        }
        for p in files
    ]


@api.delete("/documents/{name}")
def delete_document(name: str, user: User = Depends(require_tenant)):
    from app.ingestion import ingest_directory

    path = _documents_dir(user) / Path(name).name  # Path(...).name : pas de traversee de dossier
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document introuvable")
    path.unlink()
    with get_admission().slot():
        # cleanup='full' retire de l'index les chunks et les tables du fichier supprime.
        result = ingest_directory(user.owner, cleanup="full")
    audit.record("document_delete", "ok", user_id=user.id, details={"file": path.name})
    return result


@api.post("/ingest")
def ingest(full_cleanup: bool = False, user: User = Depends(require_tenant)):
    from app.ingestion import ingest_directory

    with get_admission().slot():
        return ingest_directory(user.owner, cleanup="full" if full_cleanup else "incremental")


@api.post("/upload")
def upload(files: list[UploadFile] = File(...), user: User = Depends(require_tenant)):
    from app.ingestion import ingest_directory

    s = get_settings()
    incoming = {Path(f.filename or "").name: f.size or 0 for f in files}
    for f in files:
        name = Path(f.filename or "").name
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Extension non supportee : {name}")
        if (f.size or 0) > s.max_upload_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"Fichier trop volumineux (max {s.max_upload_mb} Mo) : {name}")

    target = _documents_dir(user)
    # Un fichier du meme nom est remplace : il ne compte qu'une fois.
    kept = sum(p.stat().st_size for p in target.iterdir() if p.is_file() and p.name not in incoming)
    if kept + sum(incoming.values()) > s.max_user_storage_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Espace de stockage dépassé (max {s.max_user_storage_mb} Mo).")

    saved = []
    for f in files:
        name = Path(f.filename or "").name
        with open(target / name, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(name)
    with get_admission().slot():
        result = ingest_directory(user.owner, cleanup="incremental")
    audit.record("document_upload", "ok", user_id=user.id, details={"files": saved})
    return {"saved": saved, **result}


@api.get("/audit", dependencies=[Depends(require_admin)])
def audit_log(limit: int = Query(default=50, ge=1, le=500)):
    return audit.recent(limit)


def _audit_question(
    user: User, request_id: str, thread_id: str, question: str, started: float, status: str, out: dict
) -> None:
    audit.record(
        "question",
        status,
        request_id=request_id,
        user_id=user.id,
        thread_id=thread_id,
        duration_ms=int((time.monotonic() - started) * 1000),
        question=question,
        route=out.get("route"),
        sql_query=out.get("sql") or None,
        details={"sources": sorted({s.get("source") or "?" for s in out.get("sources", [])})},
    )


@api.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, user: User = Depends(require_tenant)):
    from app.graph import get_graph, make_input

    thread_id = req.thread_id or str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    started = time.monotonic()
    status, out = "error", {}
    try:
        with get_admission().slot():
            out = get_graph().invoke(make_input(req.question, user.owner), run_config(thread_key(user, thread_id)))
        status = "ok"
    except QueueFull:
        status = "refused"
        raise
    finally:
        _audit_question(user, request_id, thread_id, req.question, started, status, out)
    return AskResponse(
        answer=out.get("answer", ""),
        route=out.get("route", ""),
        thread_id=thread_id,
        request_id=request_id,
        sources=out.get("sources", []),
        sql=out.get("sql") or None,
    )


@api.post("/ask/stream")
def ask_stream(req: AskRequest, user: User = Depends(require_tenant)):
    """SSE : `step` (file d'attente ou noeud termine), `token` (generation), `done` (reponse finale) ou `error`."""
    from app.graph import get_graph, make_input

    thread_id = req.thread_id or str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    graph = get_graph()
    config = run_config(thread_key(user, thread_id))

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def events():
        started = time.monotonic()
        status, final = "error", {}
        try:
            yield sse("step", {"node": "queue"})
            with get_admission().slot():
                graph_input = make_input(req.question, user.owner)
                for mode, chunk in graph.stream(graph_input, config, stream_mode=["updates", "messages"]):
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
            _audit_question(user, request_id, thread_id, req.question, started, status, final)

    return StreamingResponse(events(), media_type="text/event-stream")


app.include_router(api)

if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
