"""Recherche hybride par utilisateur (pgvector + plein texte PostgreSQL via EnsembleRetriever) puis
rerank cross-encoder.

Le score du reranker est conserve dans metadata['relevance'] : le graphe s'en sert pour
decider si le contexte est pertinent ou s'il faut reformuler la question (RAG correctif)."""
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from app.config import get_settings

logger = logging.getLogger(__name__)

SUMMARY_MAX_CHUNKS = 6  # ~2 400 tokens : laisse la place a la reponse dans num_ctx=4096


class KeywordRetriever(BaseRetriever):
    """Branche lexicale de la recherche hybride, servie par PostgreSQL."""

    owner: str
    k: int

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        from app.vectorstore import keyword_search

        return keyword_search(query, self.owner, self.k)


def get_base_retriever(owner: str):
    from app.vectorstore import get_vectorstore

    s = get_settings()
    # Le filtre double la Row-Level Security de la base : deux protections independantes.
    vector = get_vectorstore(owner).as_retriever(search_kwargs={"k": s.retrieve_k, "filter": {"owner_id": owner}})
    if not s.hybrid_search:
        return vector
    from langchain_classic.retrievers import EnsembleRetriever

    # Fusion par Reciprocal Rank Fusion ; le vecteur pese plus que le lexical.
    return EnsembleRetriever(retrievers=[vector, KeywordRetriever(owner=owner, k=s.retrieve_k)], weights=[0.6, 0.4])


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


def retrieve(question: str, owner: str) -> list[Document]:
    s = get_settings()
    candidates = get_base_retriever(owner).invoke(question)
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


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower())


def named_source(question: str, sources: set[str]) -> str | None:
    """Le document vise par la question : celui dont le nom de fichier y a le plus de mots.
    Sans mot en commun ou a egalite : aucun, sauf si le corpus ne contient qu'un document."""
    if len(sources) == 1:
        return next(iter(sources))
    compact = _alnum(question).replace(" ", "")
    scores = {}
    for source in sources:
        words = [w for w in _alnum(Path(source).stem).split() if len(w) >= 3 or any(c.isdigit() for c in w)]
        scores[source] = sum(w in compact for w in words)
    ranked = sorted(scores.values(), reverse=True)
    if not ranked or ranked[0] == 0 or (len(ranked) > 1 and ranked[0] == ranked[1]):
        return None
    return max(scores, key=scores.get)


def summary_chunks(question: str, owner: str) -> list[Document]:
    """Passages du document vise, dans l'ordre du texte : un resume doit couvrir le document,
    pas les passages les plus proches de la question. Trop long : echantillon regulier."""
    from app.vectorstore import load_chunks

    chunks = load_chunks(owner)
    source = named_source(question, {d.metadata["source"] for d in chunks if d.metadata.get("source")})
    if source is None:
        return []
    doc = sorted(
        (d for d in chunks if d.metadata.get("source") == source),
        key=lambda d: (d.metadata.get("page") or 0, d.metadata.get("start_index") or 0),
    )
    if len(doc) > SUMMARY_MAX_CHUNKS:
        step = (len(doc) - 1) / (SUMMARY_MAX_CHUNKS - 1)
        doc = [doc[round(i * step)] for i in range(SUMMARY_MAX_CHUNKS)]
    return [Document(page_content=d.page_content, metadata={**d.metadata, "relevance": 1.0}) for d in doc]
