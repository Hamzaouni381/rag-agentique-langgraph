"""Journal d'audit : qui a demande quoi, quand, avec quel resultat.

Double ecriture : une table PostgreSQL pour consulter (avec le texte de la question) et une ligne JSON
sur la sortie standard pour la supervision (sans ce texte, pour limiter les donnees personnelles).
Une panne de l'audit ne fait jamais echouer une reponse. Le role rag_reader de l'outil SQL n'a pas acces
a cette table : il ne lit que le schema tabular."""
import json
import logging
from functools import lru_cache

from sqlalchemy import text

from app.config import get_settings

logger = logging.getLogger("audit")


@lru_cache
def _ensure_table() -> None:
    from app.vectorstore import get_engine

    with get_engine().begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                event TEXT NOT NULL,
                status TEXT NOT NULL,
                request_id TEXT,
                user_id TEXT,
                thread_id TEXT,
                duration_ms INTEGER,
                question TEXT,
                route TEXT,
                sql_query TEXT,
                details JSONB
            )
            """
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS audit_log_created_at ON audit_log (created_at)"))
        conn.execute(
            text("DELETE FROM audit_log WHERE created_at < now() - make_interval(days => :days)"),
            {"days": get_settings().audit_retention_days},
        )


def record(
    event: str,
    status: str,
    *,
    request_id: str | None = None,
    user_id: str | None = None,
    thread_id: str | None = None,
    duration_ms: int | None = None,
    question: str | None = None,
    route: str | None = None,
    sql_query: str | None = None,
    details: dict | None = None,
) -> None:
    logger.info(json.dumps({
        "event": event, "status": status, "request_id": request_id, "user_id": user_id,
        "thread_id": thread_id, "duration_ms": duration_ms, "route": route,
    }, ensure_ascii=False))
    try:
        from app.vectorstore import get_engine

        _ensure_table()
        with get_engine().begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO audit_log
                        (event, status, request_id, user_id, thread_id, duration_ms, question, route,
                         sql_query, details)
                    VALUES
                        (:event, :status, :request_id, :user_id, :thread_id, :duration_ms, :question, :route,
                         :sql_query, CAST(:details AS JSONB))
                    """
                ),
                {
                    "event": event, "status": status, "request_id": request_id, "user_id": user_id,
                    "thread_id": thread_id, "duration_ms": duration_ms, "question": question, "route": route,
                    "sql_query": sql_query, "details": json.dumps(details or {}, ensure_ascii=False),
                },
            )
    except Exception:
        logger.exception("Ecriture du journal d'audit en echec")


def recent(limit: int) -> list[dict]:
    from app.vectorstore import get_engine

    _ensure_table()
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT created_at, event, status, request_id, user_id, thread_id, duration_ms, question, route,
                       sql_query, details
                FROM audit_log ORDER BY id DESC LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
    return [{**row, "created_at": row["created_at"].isoformat()} for row in rows]
