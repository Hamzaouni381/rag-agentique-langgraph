"""Configuration centralisee, lue depuis l'environnement / .env (pydantic-settings)."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM : "openai" (API OpenAI) ou "ollama" (local) ---
    llm_provider: Literal["openai", "ollama"] = "ollama"
    openai_model: str = "gpt-4o-mini"
    ollama_model: str = "qwen2.5:1.5b"  # 0.5b mesure trop faible (routage, reformulation)
    ollama_base_url: str = "http://localhost:11434"
    llm_temperature: float = 0.1
    llm_num_ctx: int = 4096
    llm_max_tokens: int = 768     # plafond de generation (sans lui un petit modele peut boucler)

    # --- Embeddings : "huggingface" (local, gratuit) ou "openai" ---
    embedding_provider: Literal["huggingface", "openai"] = "huggingface"
    embedding_model: str = "intfloat/multilingual-e5-small"
    openai_embedding_model: str = "text-embedding-3-small"
    # Prefixes exiges par les modeles e5 ; laisser vide pour bge / openai.
    embedding_query_prefix: str = "query: "
    embedding_passage_prefix: str = "passage: "

    # --- Reranker (cross-encoder) ---
    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-base"
    # Score sigmoid [0,1] minimal du meilleur passage pour considerer le contexte pertinent.
    relevance_threshold: float = 0.15

    # --- PostgreSQL : vecteurs (pgvector) + tables metier + memoire LangGraph ---
    database_url: str = "postgresql+psycopg://rag:rag@localhost:5432/rag"
    # Role en lecture seule, utilise UNIQUEMENT par l'outil SQL (defense en profondeur).
    reader_database_url: str = "postgresql+psycopg://rag_reader:rag_reader@localhost:5432/rag"
    collection_name: str = "documents"
    tabular_schema: str = "tabular"

    # --- Ingestion / retrieval ---
    documents_dir: str = "./data/documents"
    chunk_size: int = 1500
    chunk_overlap: int = 300
    retrieve_k: int = 20          # candidats avant rerank
    hybrid_search: bool = True    # vecteurs + BM25 (references exactes, numeros de serie...)
    top_n: int = 5                # passages envoyes au LLM
    max_rewrites: int = 1         # boucles de reformulation si contexte non pertinent
    max_sql_retries: int = 1      # boucles d'auto-correction SQL

    # --- API ---
    api_key: str = ""             # si defini, exige l'en-tete X-API-Key

    @property
    def psycopg_url(self) -> str:
        """URL au format libpq (sans le '+psycopg' de SQLAlchemy) pour le checkpointer."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
