import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Libelles des noeuds du graphe LangGraph (app/graph/builder.py).
const STEP_LABELS = {
  contextualize: "Analyse de la question",
  route: "Choix de la source",
  retrieve: "Recherche dans les documents",
  rewrite: "Reformulation de la question",
  generate: "Rédaction de la réponse",
  write_sql: "Écriture de la requête SQL",
  execute_sql: "Exécution de la requête",
  answer_sql: "Analyse des résultats",
  chat: "Réponse directe",
};

const ROUTES = {
  documents: { label: "Documents", className: "route-docs" },
  database: { label: "Base de données", className: "route-db" },
  chat: { label: "Discussion", className: "route-chat" },
};

function uniqueSources(sources = []) {
  const seen = new Set();
  return sources.filter((s) => {
    const key = `${s.source}|${s.page}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export default function Message({ message }) {
  const [showSql, setShowSql] = useState(false);

  if (message.role === "user") {
    return (
      <div className="msg user">
        <div className="bubble">{message.content}</div>
      </div>
    );
  }

  const streaming = message.status === "streaming";
  const route = ROUTES[message.route];
  const sources = uniqueSources(message.sources);
  const steps = message.steps || [];

  return (
    <div className="msg assistant">
      <div className="avatar">◆</div>
      <div className="assistant-body">
        <div className="meta-row">
          {route && <span className={`route ${route.className}`}>{route.label}</span>}
          {!streaming && steps.length > 0 && (
            <span className="path" title="Étapes parcourues dans le graphe LangGraph">
              {steps.map((s) => STEP_LABELS[s] || s).join(" → ")}
            </span>
          )}
        </div>

        {streaming && (
          <ul className="steps">
            {steps.map((s, i) => (
              <li key={i} className="step done">
                ✓ {STEP_LABELS[s] || s}
              </li>
            ))}
            <li className="step running">
              <span className="spinner" /> En cours…
            </li>
          </ul>
        )}

        {message.content && (
          <div className={`answer ${message.status === "error" ? "error" : ""}`}>
            {message.status === "error" ? (
              <>⚠ {message.content}</>
            ) : (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
            )}
          </div>
        )}

        {!streaming && sources.length > 0 && (
          <div className="sources">
            <div className="sources-title">Sources</div>
            {sources.map((s) => (
              <div key={`${s.source}-${s.page}`} className="source">
                <span className="source-name">
                  {s.source}
                  {s.page ? ` · page ${s.page}` : ""}
                </span>
                {s.relevance != null && (
                  <span className="score" title="Score de pertinence du reranker">
                    <span className="score-bar">
                      <span style={{ width: `${Math.round(s.relevance * 100)}%` }} />
                    </span>
                    {Math.round(s.relevance * 100)} %
                  </span>
                )}
              </div>
            ))}
          </div>
        )}

        {!streaming && message.sql && (
          <div className="sql">
            <button className="link" onClick={() => setShowSql((v) => !v)}>
              {showSql ? "▾" : "▸"} Requête SQL exécutée
            </button>
            {showSql && <pre>{message.sql}</pre>}
          </div>
        )}
      </div>
    </div>
  );
}
