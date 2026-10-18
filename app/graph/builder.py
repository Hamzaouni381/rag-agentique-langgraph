"""Graphe LangGraph : routage agentique entre RAG correctif, Text-to-SQL auto-corrige et chat.

    START -> contextualize -> route --documents--> retrieve -> [pertinent ?] -> generate -> END
                                  |                    ^            | non
                                  |                    +-- rewrite <+ (max_rewrites)
                                  |--database--> write_sql -> execute_sql -> [erreur ?] -> answer_sql -> END
                                  |                  ^                 | oui (max_sql_retries)
                                  |                  +-----------------+
                                  +--chat------> chat -> END

Les dependances (LLM, retriever, backend SQL, checkpointer) sont injectees : les tests
remplacent tout par des faux sans base ni modele."""
import logging
import re
import unicodedata
from collections.abc import Callable

from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langgraph.graph import END, START, StateGraph

from app.graph.state import GraphState
from app.prompts import (
    CHAT_PROMPT,
    CONTEXTUALIZE_PROMPT,
    NOT_FOUND,
    RAG_PROMPT,
    REWRITE_PROMPT,
    ROUTER_PROMPT,
    SQL_ANSWER_PROMPT,
    SQL_ERROR_BLOCK,
    SQL_PROMPT,
)
from app.sql_backend import SqlBackend, clean_sql

logger = logging.getLogger(__name__)

HISTORY_TURNS = 6  # messages d'historique transmis au LLM


def format_context(docs: list[Document]) -> str:
    parts = []
    for i, d in enumerate(docs, 1):
        src = d.metadata.get("source", "?")
        page = d.metadata.get("page")
        ref = f"[{src}, page {page}]" if page else f"[{src}]"
        parts.append(f"--- Passage {i} {ref} ---\n{d.page_content}")
    return "\n\n".join(parts)


def doc_sources(docs: list[Document]) -> list[dict]:
    return [
        {"source": d.metadata.get("source"), "page": d.metadata.get("page"), "relevance": d.metadata.get("relevance")}
        for d in docs
    ]


ANALYTIC_INTENT = re.compile(
    r"\b(combien|nombre|total|totaux|somme|moyenne|moyen|max|maximum|min|minimum|cumul\w*|"
    r"classement|top|repartition|par (ligne|mois|jour|type|semaine|an)|liste des|statistiques?|"
    r"evolution|pourcentage|taux)\b",
    re.IGNORECASE,
)


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def has_analytic_intent(question: str) -> bool:
    return bool(ANALYTIC_INTENT.search(_ascii(question)))


_SCHEMA_NOISE = {"selec", "creat", "table", "rows", "from", "integ", "bigin", "times", "text", "doubl", "preci"}


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z0-9]{4,}", _ascii(text))}


def mentions_schema(question: str, table_info: str) -> bool:
    """La question cite-t-elle le vocabulaire des tables (noms de tables/colonnes, valeurs d'exemple) ?
    Comparaison sur les 5 premieres lettres pour absorber pluriels et accords."""
    return bool((_stems(question) - _SCHEMA_NOISE) & _stems(table_info))


def parse_route(text: str, has_tables: bool, question: str = "", raw_question: str | None = None) -> str:
    """Decision du LLM encadree par des garde-fous : un petit modele local route mal, donc on
    n'envoie vers SQL que si des tables existent ET que la question (autonome) est analytique,
    et vers chat que si le message brut est court."""
    raw_question = question if raw_question is None else raw_question
    t = text.strip().lower()
    if "database" in t or "base de donn" in t or "sql" in t:
        if has_tables and (not question or has_analytic_intent(question)):
            return "database"
        return "documents"
    if t.startswith("chat"):
        # "chat" = pas de recherche : reserve aux messages courts (salutations, remerciements).
        if not raw_question or len(raw_question.split()) <= 5:
            return "chat"
    return "documents"  # defaut sur : la recherche documentaire ne peut pas casser


def _is_valid_reformulation(text: str, original: str) -> bool:
    return bool(text) and text.rstrip().endswith("?") and len(text) <= 2 * len(original) + 80


# Pas de "il/elle" : trop ambigus ("faut-il", "y a-t-il").
FOLLOW_UP = re.compile(r"^(et|mais|alors|aussi)\b|\b(sa|son|ses|leur|leurs|celui|celle|"
                       r"ce dernier|cette derniere|le meme|la meme)\b", re.IGNORECASE)


def _is_follow_up(question: str) -> bool:
    return len(question.split()) <= 8 and bool(FOLLOW_UP.search(question.strip()))


def build_graph(
    llm: BaseChatModel,
    retrieve_fn: Callable[[str], list[Document]],
    sql_backend: SqlBackend | None = None,
    *,
    relevance_threshold: float = 0.15,
    max_rewrites: int = 1,
    max_sql_retries: int = 1,
    checkpointer=None,
):
    to_text = StrOutputParser()

    def _aux(prompt, inputs: dict, default: str) -> str:
        """Appel LLM auxiliaire (reformulation, routage) : en cas d'echec on degrade
        vers une valeur par defaut au lieu de faire echouer toute la requete."""
        try:
            return (prompt | llm | to_text).invoke(inputs).strip() or default
        except Exception as e:
            logger.warning("Appel LLM auxiliaire en echec (%s), valeur par defaut utilisee", e)
            return default

    def _history(state: GraphState):
        # Tout sauf le dernier message (la question courante).
        return state.get("messages", [])[:-1][-HISTORY_TURNS:]

    # ---------- noeuds communs ----------
    def contextualize(state: GraphState) -> dict:
        question = state["question"]
        history = _history(state)
        standalone = question
        if history:
            standalone = _aux(CONTEXTUALIZE_PROMPT, {"history": history, "question": question}, "")
            if not _is_valid_reformulation(standalone, question):
                # Le modele a repondu au lieu de reformuler. Question de relance ("Et sa temperature ?") :
                # on concatene la question precedente pour garder le sujet ; sinon question brute.
                standalone = question
                if _is_follow_up(question):
                    previous = next((m.content for m in reversed(history) if m.type == "human"), "")
                    standalone = f"{previous} {question}".strip()
        return {
            "standalone": standalone,
            "search_query": standalone,
            "rewrites": 0,
            "sql_attempts": 0,
            "sql_error": "",
            "documents": [],
            "sources": [],
            "sql": "",
        }

    def route(state: GraphState) -> dict:
        tables = sql_backend.list_tables() if sql_backend else []
        raw = _aux(ROUTER_PROMPT, {"question": state["standalone"], "tables": ", ".join(tables) or "(aucune)"},
                   "documents")
        decision = parse_route(raw, bool(tables), question=state["standalone"], raw_question=state["question"])
        if decision == "database" and not mentions_schema(state["standalone"], sql_backend.table_info()):
            decision = "documents"  # "combien de temps..." sans lien avec les tables
        logger.info("Route : %s (brut=%r)", decision, raw[:40])
        return {"route": decision}

    # ---------- branche RAG ----------
    def retrieve(state: GraphState) -> dict:
        return {"documents": retrieve_fn(state["search_query"])}

    def grade(state: GraphState) -> str:
        docs = state.get("documents", [])
        best = max((d.metadata.get("relevance", 1.0) for d in docs), default=0.0)
        if docs and best >= relevance_threshold:
            return "generate"
        return "rewrite" if state.get("rewrites", 0) < max_rewrites else "generate"

    def rewrite(state: GraphState) -> dict:
        new_q = _aux(REWRITE_PROMPT, {"question": state["standalone"]}, state["standalone"])
        logger.info("Reformulation : %r -> %r", state["standalone"], new_q)
        return {"search_query": new_q, "rewrites": state.get("rewrites", 0) + 1}

    def generate(state: GraphState) -> dict:
        docs = state.get("documents", [])
        best = max((d.metadata.get("relevance", 1.0) for d in docs), default=0.0)
        if not docs or best < relevance_threshold:
            # Pas d'appel LLM sur un contexte hors sujet : c'est la que naissent les hallucinations.
            return {"answer": NOT_FOUND, "sources": [], "messages": [AIMessage(NOT_FOUND)]}
        answer = (RAG_PROMPT | llm | to_text).invoke(
            {"context": format_context(docs), "question": state["standalone"]}
        )
        return {"answer": answer, "sources": doc_sources(docs), "messages": [AIMessage(answer)]}

    # ---------- branche SQL ----------
    def write_sql(state: GraphState) -> dict:
        error_block = ""
        if state.get("sql_error"):
            error_block = SQL_ERROR_BLOCK.format(sql=state.get("sql", ""), error=state["sql_error"])
        raw = (SQL_PROMPT | llm | to_text).invoke({
            "schema": getattr(sql_backend, "schema", "tabular"),
            "table_info": sql_backend.table_info(),
            "question": state["standalone"],
            "error_block": error_block,
        })
        return {"sql": clean_sql(raw), "sql_attempts": state.get("sql_attempts", 0) + 1}

    def execute_sql(state: GraphState) -> dict:
        try:
            return {"sql_result": sql_backend.run(state["sql"]), "sql_error": ""}
        except Exception as e:
            logger.warning("SQL en echec : %s", e)
            return {"sql_error": str(e)[:500], "sql_result": ""}

    def after_sql(state: GraphState) -> str:
        if state.get("sql_error") and state.get("sql_attempts", 0) <= max_sql_retries:
            return "write_sql"
        return "answer_sql"

    def answer_sql(state: GraphState) -> dict:
        if state.get("sql_error"):
            answer = "Je n'ai pas pu interroger la base de donnees pour cette question. Pouvez-vous la reformuler ?"
        else:
            answer = (SQL_ANSWER_PROMPT | llm | to_text).invoke(
                {"question": state["standalone"], "sql": state["sql"], "result": state["sql_result"]}
            )
        return {"answer": answer, "messages": [AIMessage(answer)]}

    # ---------- chat ----------
    def chat(state: GraphState) -> dict:
        answer = (CHAT_PROMPT | llm | to_text).invoke({"history": _history(state), "question": state["question"]})
        return {"answer": answer, "messages": [AIMessage(answer)]}

    g = StateGraph(GraphState)
    for name, fn in [
        ("contextualize", contextualize), ("route", route),
        ("retrieve", retrieve), ("rewrite", rewrite), ("generate", generate),
        ("write_sql", write_sql), ("execute_sql", execute_sql), ("answer_sql", answer_sql),
        ("chat", chat),
    ]:
        g.add_node(name, fn)

    g.add_edge(START, "contextualize")
    g.add_edge("contextualize", "route")
    g.add_conditional_edges(
        "route", lambda s: s["route"],
        {"documents": "retrieve", "database": "write_sql", "chat": "chat"},
    )
    g.add_conditional_edges("retrieve", grade, {"generate": "generate", "rewrite": "rewrite"})
    g.add_edge("rewrite", "retrieve")
    g.add_edge("generate", END)
    g.add_edge("write_sql", "execute_sql")
    g.add_conditional_edges("execute_sql", after_sql, {"write_sql": "write_sql", "answer_sql": "answer_sql"})
    g.add_edge("answer_sql", END)
    g.add_edge("chat", END)

    return g.compile(checkpointer=checkpointer)


def make_input(question: str) -> dict:
    return {"question": question, "messages": [HumanMessage(question)]}
