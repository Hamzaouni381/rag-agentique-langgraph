"""Assemblage du graphe de production : vrais LLM, retriever, SQL et memoire Postgres."""
import logging
from functools import lru_cache

from app.config import get_settings
from app.graph.builder import build_graph, make_input

logger = logging.getLogger(__name__)

__all__ = ["get_graph", "make_input"]


def _postgres_checkpointer():
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=get_settings().psycopg_url,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    saver = PostgresSaver(pool)
    saver.setup()  # cree les tables checkpoints* si absentes
    return saver


def _sql_backend_for(owner: str):
    from app.sql_backend import get_sql_backend

    try:
        backend = get_sql_backend(owner)
        backend.list_tables()
        return backend
    except Exception as e:  # pas de role lecteur / schema : la branche SQL est desactivee
        logger.warning("Backend SQL indisponible (%s) : routage documents/chat uniquement", e)
        return None


@lru_cache
def get_graph():
    from app.llm import get_chat_model
    from app.retriever import retrieve, summary_chunks

    s = get_settings()
    return build_graph(
        llm=get_chat_model(),
        retrieve_fn=retrieve,
        sql_backend=_sql_backend_for,
        summary_fn=summary_chunks,
        relevance_threshold=s.relevance_threshold if s.rerank_enabled else 0.0,
        max_rewrites=s.max_rewrites,
        max_sql_retries=s.max_sql_retries,
        checkpointer=_postgres_checkpointer(),
    )
