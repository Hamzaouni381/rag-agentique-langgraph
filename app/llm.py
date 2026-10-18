"""Fabriques LLM / embeddings. Le reste du code ne connait que les interfaces LangChain,
changer de fournisseur = changer une variable d'environnement."""
from functools import lru_cache

from langchain.chat_models import init_chat_model
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from app.config import get_settings


@lru_cache
def get_chat_model() -> BaseChatModel:
    s = get_settings()
    if s.llm_provider == "openai":
        return init_chat_model(
            s.openai_model, model_provider="openai", temperature=s.llm_temperature, max_tokens=s.llm_max_tokens
        )
    return init_chat_model(
        s.ollama_model,
        model_provider="ollama",
        base_url=s.ollama_base_url,
        temperature=s.llm_temperature,
        num_ctx=s.llm_num_ctx,
        num_predict=s.llm_max_tokens,
    )


@lru_cache
def get_embeddings() -> Embeddings:
    s = get_settings()
    if s.embedding_provider == "openai":
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(model=s.openai_embedding_model)

    from langchain_huggingface import HuggingFaceEmbeddings

    doc_kwargs = {"normalize_embeddings": True}
    query_kwargs = {"normalize_embeddings": True}
    if s.embedding_passage_prefix:
        doc_kwargs["prompt"] = s.embedding_passage_prefix
    if s.embedding_query_prefix:
        query_kwargs["prompt"] = s.embedding_query_prefix
    return HuggingFaceEmbeddings(
        model_name=s.embedding_model,
        model_kwargs={"device": "cpu"},
        encode_kwargs=doc_kwargs,
        query_encode_kwargs=query_kwargs,
    )
