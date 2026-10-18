import { useEffect, useRef, useState } from "react";
import { deleteDocument, getHealth, listDocuments, uploadDocuments } from "../api.js";

const ACCEPT = ".pdf,.docx,.doc,.txt,.md,.xlsx,.xls,.csv";

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} o`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} Ko`;
  return `${(bytes / 1024 / 1024).toFixed(1)} Mo`;
}

export default function Sidebar({ conversations, activeId, onSelect, onNew, onDelete, disabled }) {
  const [docs, setDocs] = useState([]);
  const [health, setHealth] = useState(null);
  const [indexing, setIndexing] = useState("");
  const [error, setError] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef(null);

  const refreshDocs = () => listDocuments().then(setDocs).catch((e) => setError(e.message));

  useEffect(() => {
    refreshDocs();
    getHealth()
      .then(setHealth)
      .catch(() => setHealth({ database: "injoignable" }));
  }, []);

  async function upload(files) {
    if (!files?.length) return;
    setError("");
    setIndexing(`Indexation de ${files.length} fichier(s)…`);
    try {
      await uploadDocuments(files);
      await refreshDocs();
    } catch (e) {
      setError(e.message);
    } finally {
      setIndexing("");
    }
  }

  async function remove(name) {
    if (!window.confirm(`Supprimer « ${name} » et le retirer de l'index ?`)) return;
    setError("");
    setIndexing("Mise à jour de l'index…");
    try {
      await deleteDocument(name);
      await refreshDocs();
    } catch (e) {
      setError(e.message);
    } finally {
      setIndexing("");
    }
  }

  const healthy = health?.database === "ok";

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-logo">◆</span>
        <div>
          <div className="brand-name">Assistant RAG</div>
          <div className="brand-sub">LangChain · LangGraph</div>
        </div>
      </div>

      <button className="btn-primary" onClick={onNew} disabled={disabled}>
        + Nouvelle conversation
      </button>

      <div className="section-title">Conversations</div>
      <ul className="conv-list">
        {conversations.map((c) => (
          <li
            key={c.id}
            className={`conv-item ${c.id === activeId ? "active" : ""}`}
            onClick={() => !disabled && onSelect(c.id)}
          >
            <span className="conv-title">{c.title}</span>
            <button
              className="icon-btn"
              title="Supprimer la conversation"
              onClick={(e) => {
                e.stopPropagation();
                if (!disabled) onDelete(c.id);
              }}
            >
              ×
            </button>
          </li>
        ))}
      </ul>

      <div className="section-title">Documents indexés</div>
      <div
        className={`dropzone ${dragOver ? "over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          upload([...e.dataTransfer.files]);
        }}
        onClick={() => !indexing && fileInput.current?.click()}
      >
        {indexing ? (
          <span className="indexing">
            <span className="spinner" /> {indexing}
          </span>
        ) : (
          <>
            <strong>Ajouter des fichiers</strong>
            <span>glisser-déposer ou cliquer · PDF, Word, Excel, CSV</span>
          </>
        )}
        <input
          ref={fileInput}
          type="file"
          multiple
          accept={ACCEPT}
          hidden
          onChange={(e) => {
            upload([...e.target.files]);
            e.target.value = "";
          }}
        />
      </div>
      {error && <div className="error-text">{error}</div>}

      <ul className="doc-list">
        {docs.length === 0 && <li className="muted">Aucun document</li>}
        {docs.map((d) => (
          <li key={d.name} className="doc-item">
            <span className="doc-icon">{d.kind === "table" ? "▦" : "▤"}</span>
            <span className="doc-name" title={d.name}>
              {d.name}
            </span>
            <span className="doc-size">{formatSize(d.size)}</span>
            <button className="icon-btn" title="Supprimer" onClick={() => remove(d.name)} disabled={!!indexing}>
              ×
            </button>
          </li>
        ))}
      </ul>

      <div className="health">
        <span className={`dot ${healthy ? "ok" : "ko"}`} />
        {health
          ? healthy
            ? `Connecté · ${health.model}`
            : `Base : ${health.database}`
          : "Connexion…"}
      </div>
    </aside>
  );
}
