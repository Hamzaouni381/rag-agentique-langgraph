"""Outil SQL : schema decrit par LangChain SQLDatabase, execution en lecture seule.

Trois barrieres : (1) filtre statique SELECT/WITH unique, (2) transaction READ ONLY +
statement_timeout, (3) role PostgreSQL `rag_reader` qui n'a que SELECT sur le schema tabular."""
import re
from functools import lru_cache
from typing import Protocol

from app.config import get_settings

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|merge|call|vacuum|"
    r"pg_sleep|pg_read_file|lo_import|dblink)\b",
    re.IGNORECASE,
)


class UnsafeSQLError(ValueError):
    pass


def clean_sql(raw: str) -> str:
    """Retire les balises markdown que les LLM ajoutent souvent autour du SQL."""
    sql = re.sub(r"```(?:sql)?", "", raw, flags=re.IGNORECASE).strip()
    m = re.search(r"\b(with|select)\b", sql, flags=re.IGNORECASE)
    sql = sql[m.start():] if m else sql
    # Les petits modeles enchainent parfois plusieurs requetes : on garde la premiere.
    # (validate_sql reste la barriere de securite, ceci n'est que du nettoyage.)
    return sql.split(";")[0].strip()


def validate_sql(sql: str) -> str:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise UnsafeSQLError("Requete vide.")
    if ";" in sql:
        raise UnsafeSQLError("Une seule instruction autorisee.")
    if not re.match(r"^(select|with)\b", sql, flags=re.IGNORECASE):
        raise UnsafeSQLError("Seules les requetes SELECT sont autorisees.")
    if _FORBIDDEN.search(sql):
        raise UnsafeSQLError("Mot-cle interdit dans la requete.")
    return sql


class SqlBackend(Protocol):
    def list_tables(self) -> list[str]: ...
    def table_info(self) -> str: ...
    def run(self, sql: str) -> str: ...


class PostgresSqlBackend:
    def __init__(self, max_rows: int = 100, timeout_ms: int = 10_000):
        from langchain_community.utilities import SQLDatabase
        from sqlalchemy import create_engine

        s = get_settings()
        self.schema = s.tabular_schema
        self.max_rows = max_rows
        self.timeout_ms = timeout_ms
        self.engine = create_engine(s.reader_database_url, pool_pre_ping=True)
        self.db = SQLDatabase(self.engine, schema=self.schema, sample_rows_in_table_info=3)

    def list_tables(self) -> list[str]:
        return sorted(self.db.get_usable_table_names())

    def table_info(self) -> str:
        return self.db.get_table_info()

    def run(self, sql: str) -> str:
        from sqlalchemy import text

        sql = validate_sql(sql)
        with self.engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(f"SET LOCAL statement_timeout = {int(self.timeout_ms)}"))
            result = conn.execute(text(sql))
            cols = list(result.keys())
            rows = result.fetchmany(self.max_rows)
            conn.rollback()
        if not rows:
            return "(aucune ligne)"
        lines = [" | ".join(cols)] + [" | ".join("" if v is None else str(v) for v in r) for r in rows]
        return "\n".join(lines)


@lru_cache
def get_sql_backend() -> SqlBackend:
    return PostgresSqlBackend()
