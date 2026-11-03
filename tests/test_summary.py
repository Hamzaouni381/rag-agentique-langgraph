"""Demandes de resume : detection, choix du document, couverture du document, prompt sans refus."""
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver

from app import retriever
from app.graph.builder import build_graph, is_summary_request, make_input, parse_route

SOURCES = {"reglement_T2.pdf", "procedure_PV300.docx"}


def test_detects_summary_requests():
    for q in ["résumé le document reglement T2", "Peux-tu résumer la procédure ?", "Fais une synthèse", "Synthétise"]:
        assert is_summary_request(q)
    for q in ["Quel est l'indice de protection ?", "Quelle est la consommation ?"]:
        assert not is_summary_request(q)


def test_named_source_finds_document_in_question():
    assert retriever.named_source("résumé le document reglement T2", SOURCES) == "reglement_T2.pdf"
    assert retriever.named_source("resume le règlement de la ligne T2", SOURCES) == "reglement_T2.pdf"
    assert retriever.named_source("synthèse de la procédure PV-300", SOURCES) == "procedure_PV300.docx"


def test_named_source_refuses_to_guess():
    assert retriever.named_source("résume le document", SOURCES) is None
    assert retriever.named_source("résume le document", {"seul.pdf"}) == "seul.pdf"
    assert retriever.named_source("résume le document", set()) is None


def _chunk(i, source="reglement_T2.pdf"):
    return Document(page_content=f"passage {i}", metadata={"source": source, "page": 1, "start_index": i})


def test_summary_chunks_keep_text_order_and_only_the_document(monkeypatch):
    chunks = [_chunk(2), _chunk(0, "procedure_PV300.docx"), _chunk(0), _chunk(1)]
    monkeypatch.setattr("app.vectorstore.load_all_chunks", lambda: chunks)
    docs = retriever.summary_chunks("résume le reglement T2")
    assert [d.page_content for d in docs] == ["passage 0", "passage 1", "passage 2"]
    assert {d.metadata["source"] for d in docs} == {"reglement_T2.pdf"}
    assert all(d.metadata["relevance"] == 1.0 for d in docs)


def test_long_document_is_sampled_from_start_to_end(monkeypatch):
    monkeypatch.setattr("app.vectorstore.load_all_chunks", lambda: [_chunk(i) for i in range(50)] + [_chunk(0, "b.pdf")])
    docs = retriever.summary_chunks("résume le reglement T2")
    assert len(docs) == retriever.SUMMARY_MAX_CHUNKS
    assert docs[0].page_content == "passage 0"
    assert docs[-1].page_content == "passage 49"


def test_short_summary_request_is_not_routed_to_chat():
    assert parse_route("chat", False, "résume le règlement", "résume le règlement") == "documents"


def test_graph_uses_summary_chunks_and_summary_prompt():
    prompts = []

    class SpyLLM(FakeListChatModel):
        def _call(self, messages, *args, **kwargs):
            prompts.append(messages[0].content)
            return super()._call(messages, *args, **kwargs)

    llm = SpyLLM(responses=["documents", "Le reglement fixe les regles. [reglement_T2.pdf]"])
    searched = []

    def search(q):
        searched.append(q)
        return []

    g = build_graph(llm, search, None, summary_fn=lambda q: [_chunk(0)], checkpointer=InMemorySaver())
    out = g.invoke(make_input("résumé le document reglement T2"), {"configurable": {"thread_id": "s"}})
    assert out["answer"].startswith("Le reglement")
    assert searched == []
    assert "Resume les passages" in prompts[-1]
    assert "n'apparait pas" not in prompts[-1]
