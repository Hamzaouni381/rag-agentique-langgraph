"""Acces PostgreSQL : vector store pgvector, record manager (indexation incrementale), engine SQLAlchemy."""
from functools import lru_cache

from langchain_classic.indexes import SQLRecordManager
from langchain_core.documents import Document
from langchain_postgres import PGVector
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import get_settings
from app.llm import get_embeddings


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_vectorstore() -> PGVector:
    s = get_settings()
    return PGVector(
        embeddings=get_embeddings(),
        collection_name=s.collection_name,
        connection=get_engine(),
        use_jsonb=True,
    )


@lru_cache
def get_record_manager() -> SQLRecordManager:
    """Memorise le hash de chaque chunk indexe : re-ingerer un dossier ne cree pas de doublons
    et les chunks d'un fichier modifie/supprime sont nettoyes (cleanup='incremental'/'full')."""
    s = get_settings()
    rm = SQLRecordManager(namespace=f"pgvector/{s.collection_name}", engine=get_engine())
    rm.create_schema()
    return rm


def load_all_chunks() -> list[Document]:
    """Tous les chunks de la collection (alimente l'index BM25 de la recherche hybride)."""
    s = get_settings()
    sql = text(
        """
        SELECT e.document, e.cmetadata
        FROM langchain_pg_embedding e
        JOIN langchain_pg_collection c ON c.uuid = e.collection_id
        WHERE c.name = :name
        """
    )
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"name": s.collection_name}).all()
    return [Document(page_content=doc, metadata=meta or {}) for doc, meta in rows]
