"""Observabilite Langfuse : une trace par question, avec chaque noeud du graphe, chaque appel LLM,
ses tokens (entree / sortie) et son cout.

Active seulement si LANGFUSE_PUBLIC_KEY et LANGFUSE_SECRET_KEY sont definies (.env) : sans cles,
aucun appel reseau et aucun changement de comportement. Les traces partent en arriere-plan, par lots,
sans ralentir les reponses."""
import logging
from functools import lru_cache

from app.config import get_settings

logger = logging.getLogger(__name__)


def create_handler(public_key: str, secret_key: str, base_url: str, **client_kwargs):
    """Initialise le client Langfuse et renvoie le callback LangChain qui alimente les traces."""
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    # Cles passees explicitement : pydantic-settings lit .env mais ne remplit pas os.environ.
    Langfuse(public_key=public_key, secret_key=secret_key, base_url=base_url, **client_kwargs)
    return CallbackHandler(public_key=public_key)


@lru_cache
def get_langfuse_handler():
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return None
    logger.info("Traces Langfuse actives (%s)", s.langfuse_base_url)
    return create_handler(s.langfuse_public_key, s.langfuse_secret_key, s.langfuse_base_url)


def run_config(thread_id: str, run_name: str = "ask") -> dict:
    """Config d'execution du graphe : memoire (thread_id) et traces.

    Un thread_id = une session Langfuse : les questions d'une meme conversation sont regroupees
    et leurs tokens additionnes."""
    config = {
        "configurable": {"thread_id": thread_id},
        "run_name": run_name,
        "metadata": {"thread_id": thread_id, "langfuse_session_id": thread_id},
    }
    handler = get_langfuse_handler()
    if handler:
        config["callbacks"] = [handler]
    return config


def flush() -> None:
    """Envoie les traces en attente. A appeler en fin de script (CLI, evaluation) : l'envoi est
    asynchrone et un processus qui s'arrete trop tot perdrait les dernieres traces."""
    if get_langfuse_handler():
        from langfuse import get_client

        get_client(public_key=get_settings().langfuse_public_key).flush()
