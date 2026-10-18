from typing import Annotated, Literal, TypedDict

from langchain_core.documents import Document
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

Route = Literal["documents", "database", "chat"]


class GraphState(TypedDict, total=False):
    # Historique de la conversation, persiste par le checkpointer (cle : thread_id).
    messages: Annotated[list[AnyMessage], add_messages]
    question: str             # question brute de l'utilisateur
    standalone: str           # question autonome (historique resolu)
    search_query: str         # requete effectivement envoyee au retriever (peut etre reformulee)
    route: Route
    documents: list[Document]
    rewrites: int
    sql: str
    sql_result: str
    sql_error: str
    sql_attempts: int
    answer: str
    sources: list[dict]
