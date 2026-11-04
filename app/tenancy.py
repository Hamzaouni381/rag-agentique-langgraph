"""Cloisonnement entre utilisateurs : chacun ne voit que ses documents et ses tables.

L'isolation est portee par PostgreSQL, pas seulement par le code. Une erreur applicative ne suffit donc pas
a croiser les donnees :
- passages : Row-Level Security sur `langchain_pg_embedding`. Le role `rag_app`, ni proprietaire ni BYPASSRLS,
  se connecte avec `app.owner_id` fixe a l'ouverture de la connexion ;
- tables Excel/CSV : un schema et un role sans login par utilisateur. Le SQL ecrit par le LLM s'execute
  avec ce role, qui ne peut lire aucun autre schema ;
- fichiers : un dossier par utilisateur.

La cle du proprietaire est un hash de l'identifiant immuable de la personne : elle sert sans risque dans les
noms de schemas, de roles et de dossiers."""
import hashlib
import re
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import get_settings

_OWNER_KEY = re.compile(r"^[0-9a-f]{24}$")
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
_SCHEMA_LOCK = 72_001  # verrou consultatif : plusieurs repliques peuvent demarrer en meme temps
_TENANT_LOCK = 72_002


def owner_key(subject: str) -> str:
    return hashlib.sha256(subject.encode()).hexdigest()[:24]


def checked_owner(owner: str) -> str:
    """Ces valeurs finissent dans du SQL et des chemins : rien d'autre qu'une cle valide n'y entre."""
    if not _OWNER_KEY.match(owner):
        raise ValueError("Cle de proprietaire invalide")
    return owner


def tenant_schema(owner: str) -> str:
    return f"t_{checked_owner(owner)}"


def tenant_role(owner: str) -> str:
    return f"r_{checked_owner(owner)}"


def tenant_dir(owner: str) -> Path:
    return Path(get_settings().documents_dir) / checked_owner(owner)


def _role_of(database_url: str) -> str:
    role = make_url(database_url).username or ""
    if not _ROLE_NAME.match(role):
        raise ValueError(f"Nom de role inattendu : {role!r}")
    return role


@lru_cache
def ensure_schema() -> None:
    """Migration idempotente : recherche plein texte et Row-Level Security sur les passages."""
    from langchain_postgres import PGVector

    from app.llm import get_embeddings
    from app.vectorstore import get_engine

    s = get_settings()
    app_role = _role_of(s.app_database_url)
    engine = get_engine()
    PGVector(embeddings=get_embeddings(), collection_name=s.collection_name, connection=engine, use_jsonb=True)
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": _SCHEMA_LOCK})
        conn.execute(text(
            """
            ALTER TABLE langchain_pg_embedding ADD COLUMN IF NOT EXISTS fts tsvector
            GENERATED ALWAYS AS (to_tsvector('french', document) || to_tsvector('simple', document)) STORED
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS langchain_pg_embedding_fts ON langchain_pg_embedding USING gin (fts)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS langchain_pg_embedding_owner "
            "ON langchain_pg_embedding ((cmetadata->>'owner_id'))"
        ))
        conn.execute(text("ALTER TABLE langchain_pg_embedding ENABLE ROW LEVEL SECURITY"))
        conn.execute(text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT FROM pg_policies
                    WHERE tablename = 'langchain_pg_embedding' AND policyname = 'owner_isolation'
                ) THEN
                    CREATE POLICY owner_isolation ON langchain_pg_embedding TO {app_role}
                    USING (cmetadata->>'owner_id' = current_setting('app.owner_id', true))
                    WITH CHECK (cmetadata->>'owner_id' = current_setting('app.owner_id', true));
                END IF;
            END $$
            """
        ))
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON langchain_pg_embedding TO {app_role}"))
        conn.execute(text(f"GRANT SELECT ON langchain_pg_collection TO {app_role}"))


@lru_cache(maxsize=1024)
def ensure_tenant(owner: str) -> None:
    """Cree le schema et le role d'un utilisateur a sa premiere visite."""
    from app.vectorstore import get_engine

    ensure_schema()
    schema, role = tenant_schema(owner), tenant_role(owner)
    reader = _role_of(get_settings().reader_database_url)
    with get_engine().begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": _TENANT_LOCK})
        if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).first():
            conn.execute(text(f"CREATE ROLE {role} NOLOGIN NOINHERIT"))
        # SET sans INHERIT : le lecteur n'a aucun droit tant qu'il n'a pas pris ce role a la connexion.
        conn.execute(text(f"GRANT {role} TO {reader} WITH INHERIT FALSE, SET TRUE"))
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {role}"))
        conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role}"))
        conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {role}"))
