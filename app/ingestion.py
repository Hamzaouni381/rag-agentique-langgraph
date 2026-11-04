"""Ingestion par utilisateur : documents -> chunks -> pgvector (incremental) ; Excel/CSV -> tables PostgreSQL."""
import logging
import re
import unicodedata
from pathlib import Path

import pandas as pd
from langchain_classic.indexes import index
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy import inspect, text

from app.config import get_settings
from app.loaders import DOCUMENT_EXTENSIONS, TABULAR_EXTENSIONS, StructuredDocumentLoader
from app.tenancy import ensure_tenant, tenant_dir, tenant_schema

logger = logging.getLogger(__name__)


def get_splitter() -> RecursiveCharacterTextSplitter:
    s = get_settings()
    # Separateurs ordonnes pour couper d'abord entre sections Markdown, puis paragraphes,
    # et ne casser une ligne de tableau ("| ... |") qu'en dernier recours.
    return RecursiveCharacterTextSplitter(
        chunk_size=s.chunk_size,
        chunk_overlap=s.chunk_overlap,
        separators=["\n## ", "\n### ", "\n\n", "\n", ". ", " ", ""],
        add_start_index=True,
    )


def split_documents(docs: list[Document]) -> list[Document]:
    chunks = get_splitter().split_documents(docs)
    for c in chunks:
        # Le nom du fichier en tete du chunk aide l'embedding a desambiguiser les documents proches.
        c.page_content = f"[{c.metadata.get('source')}]\n{c.page_content}"
    return chunks


def _slug(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip().lower()).strip("_")
    return s or "col"


def ingest_tabular(path: Path, owner: str) -> list[str]:
    """Chaque feuille Excel / fichier CSV devient une table du schema de l'utilisateur."""
    from app.vectorstore import get_engine

    schema = tenant_schema(owner)
    if path.suffix.lower() == ".csv":
        frames = {path.stem: pd.read_csv(path, sep=None, engine="python")}
    else:
        frames = pd.read_excel(path, sheet_name=None)

    tables = []
    for sheet, df in frames.items():
        df = df.dropna(how="all").dropna(axis=1, how="all")
        if df.empty:
            continue
        df.columns = [_slug(str(c)) for c in df.columns]
        table = _slug(f"{path.stem}_{sheet}" if len(frames) > 1 else path.stem)[:60]
        df.to_sql(table, get_engine(), schema=schema, if_exists="replace", index=False)
        tables.append(table)
        logger.info("Table %s.%s : %d lignes", schema, table, len(df))
    return tables


def _drop_orphan_tables(owner: str, kept: list[str]) -> list[str]:
    """Les donnees d'un fichier Excel supprime ne doivent pas rester interrogeables."""
    from app.vectorstore import get_engine

    schema = tenant_schema(owner)
    orphans = [t for t in inspect(get_engine()).get_table_names(schema=schema) if t not in kept]
    with get_engine().begin() as conn:
        for table in orphans:
            conn.execute(text(f'DROP TABLE "{schema}"."{table}"'))
    return orphans


def ingest_directory(owner: str, directory: str | None = None, cleanup: str = "incremental") -> dict:
    """Ingere le dossier d'un utilisateur. `cleanup='full'` supprime aussi ce qui venait de ses fichiers
    disparus (chunks et tables), jamais les donnees d'un autre utilisateur."""
    from app.vectorstore import get_record_manager, get_vectorstore

    ensure_tenant(owner)
    root = Path(directory) if directory else tenant_dir(owner)
    files = sorted(p for p in root.rglob("*") if p.is_file())

    docs, tables, errors = [], [], []
    for f in files:
        ext = f.suffix.lower()
        try:
            if ext in DOCUMENT_EXTENSIONS:
                docs.extend(StructuredDocumentLoader(f).load())
            elif ext in TABULAR_EXTENSIONS:
                tables.extend(ingest_tabular(f, owner))
        except Exception as e:
            logger.exception("Echec ingestion %s", f.name)
            errors.append({"file": f.name, "error": str(e)})

    chunks = split_documents(docs)
    for chunk in chunks:
        chunk.metadata["owner_id"] = owner  # base de la Row-Level Security ; rend aussi le hash propre a chacun
    stats = index(
        chunks,
        get_record_manager(owner),
        get_vectorstore(owner),
        cleanup=cleanup,
        source_id_key="source",
        key_encoder="sha256",
    )
    # Une erreur de lecture ne doit pas faire passer les tables d'un fichier existant pour orphelines.
    if cleanup == "full" and not errors:
        _drop_orphan_tables(owner, tables)
    return {"files": len(files), "chunks": len(chunks), "tables": tables, "index": stats, "errors": errors}
