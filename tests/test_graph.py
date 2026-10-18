"""Tests du graphe LangGraph avec des faux (LLM scripte, retriever et SQL en memoire) : rapides,
deterministes, executables en CI sans base ni modele."""
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver

from app.graph.builder import build_graph, make_input
from app.prompts import NOT_FOUND


class FakeSql:
    schema = "tabular"

    def __init__(self, fail_first: bool = False):
        self.fail_first = fail_first
        self.calls = []

    def list_tables(self):
        return ["incidents"]

    def table_info(self):
        return 'CREATE TABLE tabular.incidents (ligne text, retard_min int)'

    def run(self, sql):
        self.calls.append(sql)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError('column "retard" does not exist')
        return "ligne | total\nA | 42"


def doc(text, score, source="manuel.pdf", page=3):
    return Document(page_content=text, metadata={"source": source, "page": page, "relevance": score})


def run(graph, question, thread="t1"):
    return graph.invoke(make_input(question), {"configurable": {"thread_id": thread}})


def test_rag_route_with_relevant_context():
    llm = FakeListChatModel(responses=["documents", "IP66 [manuel.pdf, page 3]"])
    g = build_graph(llm, lambda q: [doc("Indice de protection IP66", 0.9)], FakeSql(), checkpointer=InMemorySaver())
    out = run(g, "Quel est l'indice de protection ?")
    assert out["route"] == "documents"
    assert out["answer"].startswith("IP66")
    assert out["sources"][0]["source"] == "manuel.pdf"
    assert out["rewrites"] == 0


def test_irrelevant_context_triggers_rewrite_then_refuses_without_llm():
    queries = []

    def retriever(q):
        queries.append(q)
        return [doc("hors sujet", 0.01)]

    # 2 appels LLM seulement : route + rewrite. La generation est court-circuitee.
    llm = FakeListChatModel(responses=["documents", "indice protection boitier"])
    g = build_graph(llm, retriever, None, max_rewrites=1, checkpointer=InMemorySaver())
    out = run(g, "c'est etanche ?")
    assert queries == ["c'est etanche ?", "indice protection boitier"]
    assert out["answer"] == NOT_FOUND
    assert out["sources"] == []


def test_sql_route_self_corrects_after_error():
    sql = FakeSql(fail_first=True)
    llm = FakeListChatModel(responses=[
        "database",
        'SELECT "retard" FROM tabular.incidents',
        '```sql\nSELECT "ligne", SUM("retard_min") AS total FROM tabular.incidents GROUP BY "ligne"\n```',
        "La ligne A cumule 42 minutes de retard.",
    ])
    g = build_graph(llm, lambda q: [], sql, max_sql_retries=1, checkpointer=InMemorySaver())
    out = run(g, "Retard total par ligne ?")
    assert out["route"] == "database"
    assert len(sql.calls) == 2
    assert out["sql"].startswith("SELECT")          # markdown nettoye
    assert "42" in out["answer"]


def test_database_route_falls_back_to_documents_without_tables():
    llm = FakeListChatModel(responses=["database", "reponse"])
    g = build_graph(llm, lambda q: [doc("x", 0.9)], None, checkpointer=InMemorySaver())
    assert run(g, "combien d'incidents ?")["route"] == "documents"


def test_memory_contextualizes_follow_up_question():
    seen = []

    def retriever(q):
        seen.append(q)
        return [doc("contenu", 0.9)]

    llm = FakeListChatModel(responses=[
        "documents", "Reponse 1",                                   # tour 1 (pas d'historique)
        "Quelle est la temperature max de l'indicateur i20 ?",       # tour 2 : contextualisation
        "documents", "Reponse 2",
    ])
    g = build_graph(llm, retriever, None, checkpointer=InMemorySaver())
    run(g, "Parle-moi de l'indicateur i20", thread="conv")
    out = run(g, "Et sa temperature max ?", thread="conv")
    assert seen[-1] == "Quelle est la temperature max de l'indicateur i20 ?"
    assert len(out["messages"]) == 4  # 2 questions + 2 reponses persistees


def test_auxiliary_llm_failure_degrades_instead_of_crashing():
    class FlakyLLM(FakeListChatModel):
        def _call(self, messages, *args, **kwargs):
            if "question autonome" in messages[0].content:  # contextualisation
                raise RuntimeError("prediction aborted, token repeat limit reached")
            return super()._call(messages, *args, **kwargs)

    seen = []
    llm = FlakyLLM(responses=["documents", "R1", "documents", "R2"])
    g = build_graph(llm, lambda q: seen.append(q) or [doc("x", 0.9)], None, checkpointer=InMemorySaver())
    run(g, "Parle-moi du PV-300", thread="c")
    out = run(g, "Et son indice IP ?", thread="c")
    assert out["answer"] == "R2"
    # Faute de reformulation, la question precedente est concatenee pour garder le sujet.
    assert seen[-1] == "Parle-moi du PV-300 Et son indice IP ?"
