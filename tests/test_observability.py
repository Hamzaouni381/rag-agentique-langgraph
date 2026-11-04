"""Tests de l'observabilite Langfuse : sans reseau ni compte (les traces sont capturees en memoire)."""
import json

import pytest
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.observability as obs
from app.graph.builder import build_graph, make_input


class FakeSettings:
    langfuse_public_key = ""
    langfuse_secret_key = ""
    langfuse_base_url = "https://cloud.langfuse.com"


@pytest.fixture(autouse=True)
def reset_handler_cache():
    obs.get_langfuse_handler.cache_clear()
    yield
    obs.get_langfuse_handler.cache_clear()


def test_without_keys_tracing_is_off(monkeypatch):
    monkeypatch.setattr(obs, "get_settings", lambda: FakeSettings())
    config = obs.run_config("thread-1")
    assert "callbacks" not in config
    assert config["configurable"]["thread_id"] == "thread-1"
    obs.flush()  # sans cles : ne fait rien, ne plante pas


def test_with_keys_the_handler_is_attached(monkeypatch):
    s = FakeSettings()
    s.langfuse_public_key, s.langfuse_secret_key = "pk-lf-x", "sk-lf-x"
    monkeypatch.setattr(obs, "get_settings", lambda: s)
    monkeypatch.setattr(obs, "create_handler", lambda *a, **k: "HANDLER")
    config = obs.run_config("thread-1")
    assert config["callbacks"] == ["HANDLER"]
    assert config["metadata"]["langfuse_session_id"] == "thread-1"  # 1 conversation = 1 session


def test_tokens_of_each_llm_call_are_traced():
    exporter = InMemorySpanExporter()
    handler = obs.create_handler("pk-lf-test", "sk-lf-test", "http://localhost", span_exporter=exporter)
    llm = GenericFakeChatModel(messages=iter([
        AIMessage("documents", usage_metadata={"input_tokens": 120, "output_tokens": 1, "total_tokens": 121}),
        AIMessage("IP54 [m.docx, page 2]", usage_metadata={"input_tokens": 900, "output_tokens": 12, "total_tokens": 912}),
    ]))
    docs = [Document(page_content="IP54", metadata={"source": "m.docx", "page": 2, "relevance": 0.9})]
    graph = build_graph(llm, lambda q, owner: docs)

    config = {**obs.run_config("thread-42"), "callbacks": [handler]}
    graph.invoke(make_input("Quel est l'indice de protection ?", "proprietaire-test"), config)

    from langfuse import get_client

    get_client(public_key="pk-lf-test").flush()
    spans = exporter.get_finished_spans()
    usages = [
        json.loads(s.attributes["langfuse.observation.usage_details"])
        for s in spans
        if s.attributes.get("langfuse.observation.type") == "generation"
    ]
    # 2 appels LLM (routage puis generation), chacun avec ses tokens.
    assert usages == [{"input": 120, "output": 1, "total": 121}, {"input": 900, "output": 12, "total": 912}]
    names = {s.name for s in spans}
    assert {"route", "retrieve", "generate"} <= names  # les noeuds du graphe apparaissent dans la trace
    assert all(s.attributes.get("session.id") == "thread-42" for s in spans)
