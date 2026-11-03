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
    llm_num_thread: int | None = None  # Ollama : a fixer au quota CPU d'un conteneur (sinon throttling)
    llm_timeout_seconds: float = 180  # un appel LLM bloque ne doit pas occuper une place indefiniment

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

    # --- Langfuse (traces, tokens, couts) : actif seulement si les deux cles sont definies ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"  # region UE ; ou l'URL d'un Langfuse auto-heberge

    # --- API ---
    api_key: str = ""             # compte de service : scripts et tests (en-tete X-API-Key)
    auth_mode: Literal["none", "easyauth"] = "none"  # easyauth : connexion Entra geree par Container Apps
    preload_models: bool = False  # charge les modeles au demarrage ; /api/ready repond 503 d'ici la
    max_upload_mb: int = 25

    # --- Exploitation : file d'attente devant les traitements couteux, journal d'audit ---
    max_active_requests: int = 1
    request_queue_size: int = 20
    request_wait_seconds: float = 180  # reste sous la limite de 240 s de l'entree Container Apps
    audit_retention_days: int = 90

    @property
    def psycopg_url(self) -> str:
        """URL au format libpq (sans le '+psycopg' de SQLAlchemy) pour le checkpointer."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
