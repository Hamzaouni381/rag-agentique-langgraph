"""Loader LangChain qui encapsule les parseurs maison de la V1 (tableaux PDF/Word en Markdown,
en-tetes repetes supprimes, pages logiques Word). Repli sur les loaders standard en cas d'echec."""
import logging
from collections.abc import Iterator
from pathlib import Path

from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document

logger = logging.getLogger(__name__)

DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".md"}
TABULAR_EXTENSIONS = {".xlsx", ".xls", ".csv"}


class StructuredDocumentLoader(BaseLoader):
    """Une page (PDF) ou section logique (Word) = un Document, metadonnees source/page."""

    def __init__(self, file_path: str | Path):
        self.path = Path(file_path)

    def lazy_load(self) -> Iterator[Document]:
        ext = self.path.suffix.lower()
        try:
            pages = self._parse(ext)
        except Exception as e:  # parseur maison en echec -> loader standard
            logger.warning("Parseur structure en echec sur %s (%s), repli loader standard", self.path.name, e)
            yield from self._fallback(ext)
            return

        for p in pages:
            text = (p.get("text") or "").strip()
            if not text:
                continue
            yield Document(
                page_content=text,
                metadata={
                    "source": self.path.name,
                    "page": p.get("page"),
                    "doc_type": ext.lstrip("."),
                },
            )

    def _parse(self, ext: str) -> list[dict]:
        if ext == ".pdf":
            from app.parsers.pdf_parser import parse_pdf

            return parse_pdf(str(self.path))
        if ext == ".docx":
            from app.parsers.word_parser import parse_word

            return parse_word(str(self.path))
        if ext == ".doc":
            from app.parsers.word_parser import parse_doc

            return parse_doc(str(self.path))
        if ext in {".txt", ".md"}:
            return [{"text": self.path.read_text(encoding="utf-8", errors="ignore"), "page": None}]
        raise ValueError(f"Extension non supportee : {ext}")

    def _fallback(self, ext: str) -> Iterator[Document]:
        if ext == ".pdf":
            from langchain_community.document_loaders import PyMuPDFLoader

            for d in PyMuPDFLoader(str(self.path)).lazy_load():
                d.metadata = {"source": self.path.name, "page": d.metadata.get("page", 0) + 1, "doc_type": "pdf"}
                yield d
        elif ext == ".docx":
            from langchain_community.document_loaders import Docx2txtLoader

            for d in Docx2txtLoader(str(self.path)).lazy_load():
                d.metadata = {"source": self.path.name, "page": None, "doc_type": "docx"}
                yield d
