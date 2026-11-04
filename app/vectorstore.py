"""Acces PostgreSQL : vector store pgvector et recherche plein texte par utilisateur, record manager
(indexation incrementale), engines SQLAlchemy."""
from functools import lru_cache

from langchain_classic.indexes import SQLRecordManager
from langchain_core.documents import Document
from langchain_postgres import PGVector
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.llm import get_embeddings
from app.tenancy import checked_owner


@lru_cache
def get_engine() -> Engine:
    """Role proprietaire : migrations, tables Excel, memoire, audit. Jamais pour lire les passages."""
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache(maxsize=256)
def owner_engine(owner: str) -> Engine:
    """Role `rag_app`, soumis a la Row-Level Security : chaque connexion ne voit que ce proprietaire.
    Sans pool : le petit serveur PostgreSQL limite les connexions, et il y a un engine par utilisateur."""
    return create_engine(
        get_settings().app_database_url,
        poolclass=NullPool,
        connect_args={"options": f"-c app.owner_id={checked_owner(owner)}"},
    )


@lru_cache(maxsize=256)
def get_vectorstore(owner: str) -> PGVector:
    s = get_settings()
    return PGVector(
        embeddings=get_embeddings(),
        collection_name=s.collection_name,
        connection=owner_engine(owner),
        use_jsonb=True,
        create_extension=False,
    )


@lru_cache(maxsize=256)
def get_record_manager(owner: str) -> SQLRecordManager:
    """Memorise le hash de chaque chunk indexe : re-ingerer un dossier ne cree pas de doublons
    et les chunks d'un fichier modifie/supprime sont nettoyes (cleanup='incremental'/'full').
    Un espace par utilisateur : le nettoyage complet de l'un ne peut pas toucher l'autre."""
    s = get_settings()
    namespace = f"pgvector/{s.collection_name}/{checked_owner(owner)}"
    rm = SQLRecordManager(namespace=namespace, engine=get_engine())
    rm.create_schema()
    return rm


def load_chunks(owner: str) -> list[Document]:
    """Tous les chunks d'un utilisateur (resumes)."""
    sql = text(
        """
        SELECT e.document, e.cmetadata
        FROM langchain_pg_embedding e
        JOIN langchain_pg_collection c ON c.uuid = e.collection_id
        WHERE c.name = :name AND e.cmetadata->>'owner_id' = :owner
        """
    )
    with owner_engine(owner).connect() as conn:
        rows = conn.execute(sql, {"name": get_settings().collection_name, "owner": owner}).all()
    return [Document(page_content=doc, metadata=meta or {}) for doc, meta in rows]


def keyword_search(query: str, owner: str, k: int) -> list[Document]:
    """Recherche par mots-cles dans PostgreSQL, sans index a garder en memoire : toutes les repliques
    voient un document des qu'il est depose. Le dictionnaire `french` absorbe pluriels et accords,
    `simple` garde les references telles quelles (PV-300) ; un seul mot commun suffit (OU)."""
    sql = text(
        """
        WITH q AS (
            SELECT regexp_replace(plainto_tsquery('french', :query)::text, '&|<->', '|', 'g')::tsquery
                || regexp_replace(plainto_tsquery('simple', :query)::text, '&|<->', '|', 'g')::tsquery AS terms
        )
        SELECT e.document, e.cmetadata
        FROM langchain_pg_embedding e
        JOIN langchain_pg_collection c ON c.uuid = e.collection_id, q
        WHERE c.name = :name AND e.cmetadata->>'owner_id' = :owner AND e.fts @@ q.terms
        ORDER BY ts_rank(e.fts, q.terms, 1) DESC
        LIMIT :k
        """
    )
    params = {"query": query, "name": get_settings().collection_name, "owner": owner, "k": k}
    with owner_engine(owner).connect() as conn:
        rows = conn.execute(sql, params).all()
    return [Document(page_content=doc, metadata=meta or {}) for doc, meta in rows]
