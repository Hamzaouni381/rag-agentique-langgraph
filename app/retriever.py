"""Recherche hybride (pgvector + BM25 via EnsembleRetriever) puis rerank cross-encoder.

Le score du reranker est conserve dans metadata['relevance'] : le graphe s'en sert pour
decider si le contexte est pertinent ou s'il faut reformuler la question (RAG correctif)."""
import logging
import threading
from functools import lru_cache

from langchain_core.documents import Document

from app.config import get_settings

logger = logging.getLogger(__name__)

_bm25 = None
_bm25_lock = threading.Lock()


def invalidate_bm25() -> None:
    global _bm25
    with _bm25_lock:
        _bm25 = None


def _get_bm25(k: int):
    """Index BM25 construit en memoire a partir des chunks Postgres (reconstruit apres ingestion)."""
    global _bm25
    with _bm25_lock:
        if _bm25 is None:
            from langchain_community.retrievers import BM25Retriever

            from app.vectorstore import load_all_chunks

            chunks = load_all_chunks()
            if not chunks:
                return None
            _bm25 = BM25Retriever.from_documents(chunks, k=k)
        return _bm25


def get_base_retriever():
    from app.vectorstore import get_vectorstore

    s = get_settings()
    vector = get_vectorstore().as_retriever(search_kwargs={"k": s.retrieve_k})
    if not s.hybrid_search:
        return vector
    bm25 = _get_bm25(s.retrieve_k)
    if bm25 is None:
        return vector
    from langchain_classic.retrievers import EnsembleRetriever

    # Fusion par Reciprocal Rank Fusion ; le vecteur pese plus que le lexical.
    return EnsembleRetriever(retrievers=[vector, bm25], weights=[0.6, 0.4])


@lru_cache
def get_cross_encoder():
    from langchain_community.cross_encoders import HuggingFaceCrossEncoder

    return HuggingFaceCrossEncoder(model_name=get_settings().rerank_model, model_kwargs={"device": "cpu"})


def rerank(question: str, docs: list[Document], top_n: int) -> list[Document]:
    if not docs:
        return []
    scores = get_cross_encoder().score([(question, d.page_content) for d in docs])
    ranked = sorted(zip(docs, scores, strict=True), key=lambda x: float(x[1]), reverse=True)[:top_n]
    out = []
    for d, sc in ranked:
        d = Document(page_content=d.page_content, metadata={**d.metadata, "relevance": round(float(sc), 4)})
        out.append(d)
    return out


def retrieve(question: str) -> list[Document]:
    s = get_settings()
    candidates = get_base_retriever().invoke(question)
    # Deduplication (un chunk peut sortir du vecteur ET de BM25).
    seen, unique = set(), []
    for d in candidates:
        key = (d.metadata.get("source"), d.metadata.get("start_index"), d.page_content[:80])
        if key not in seen:
            seen.add(key)
            unique.append(d)
    if not s.rerank_enabled:
        return unique[: s.top_n]
    return rerank(question, unique, s.top_n)
