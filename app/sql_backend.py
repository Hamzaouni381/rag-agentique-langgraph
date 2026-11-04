"""Outil SQL : schema decrit par LangChain SQLDatabase, execution en lecture seule.

Trois barrieres : (1) filtre statique SELECT/WITH unique, sans acces au catalogue, (2) transaction READ ONLY +
statement_timeout, (3) role PostgreSQL de l'utilisateur, qui n'a que SELECT sur son propre schema."""
import re
from functools import lru_cache
from typing import Protocol

from app.config import get_settings
from app.tenancy import tenant_role, tenant_schema

# Le catalogue (pg_*, information_schema) est lisible par tout role : il revelerait les noms des autres
# utilisateurs et de leurs tables. set_config pourrait tenter de changer de role.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|merge|call|vacuum|"
    r"pg_\w+|information_schema|set_config|lo_import|dblink)\b",
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
    def __init__(self, engine, schema: str, max_rows: int = 100, timeout_ms: int = 10_000):
        self.engine = engine
        self.schema = schema
        self.max_rows = max_rows
        self.timeout_ms = timeout_ms

    def _db(self):
        """SQLDatabase fige la liste des tables a sa creation : on la recree pour voir les fichiers
        Excel deposes depuis le demarrage."""
        from langchain_community.utilities import SQLDatabase

        return SQLDatabase(self.engine, schema=self.schema, sample_rows_in_table_info=3)

    def list_tables(self) -> list[str]:
        return sorted(self._db().get_usable_table_names())

    def table_info(self) -> str:
        return self._db().get_table_info()

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


@lru_cache(maxsize=256)
def get_sql_backend(owner: str) -> SqlBackend:
    """Chaque connexion prend le role de l'utilisateur des son ouverture : le SQL du LLM ne peut lire que
    son schema, meme s'il cite celui d'un autre. Sans pool : le serveur limite les connexions."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    engine = create_engine(
        get_settings().reader_database_url,
        poolclass=NullPool,
        connect_args={"options": f"-c role={tenant_role(owner)}"},
    )
    return PostgresSqlBackend(engine, tenant_schema(owner))
