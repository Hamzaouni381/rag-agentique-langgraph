import { useCallback, useEffect, useRef, useState } from "react";
import { askStream } from "./api.js";
import Sidebar from "./components/Sidebar.jsx";
import Chat from "./components/Chat.jsx";

const STORAGE_KEY = "rag-v2-conversations";

function loadConversations() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || [];
  } catch {
    return [];
  }
}

function newConversation() {
  return { id: crypto.randomUUID(), title: "Nouvelle conversation", messages: [], updatedAt: Date.now() };
}

export default function App() {
  const [conversations, setConversations] = useState(() => {
    const saved = loadConversations();
    return saved.length ? saved : [newConversation()];
  });
  const [activeId, setActiveId] = useState(() => conversations[0].id);
  const [busy, setBusy] = useState(false);
  const abortRef = useRef(null);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
    } catch {
      /* stockage indisponible : l'appli fonctionne sans persistance */
    }
  }, [conversations]);

  const active = conversations.find((c) => c.id === activeId) || conversations[0];

  const updateConversation = useCallback((id, fn) => {
    setConversations((list) => list.map((c) => (c.id === id ? { ...fn(c), updatedAt: Date.now() } : c)));
  }, []);

  // Met a jour le dernier message (la reponse en cours) de la conversation.
  const patchLastMessage = useCallback(
    (id, fn) =>
      updateConversation(id, (c) => {
        const messages = [...c.messages];
        messages[messages.length - 1] = fn(messages[messages.length - 1]);
        return { ...c, messages };
      }),
    [updateConversation]
  );

  async function send(question) {
    if (!question.trim() || busy) return;
    const id = active.id;
    updateConversation(id, (c) => ({
      ...c,
      title: c.messages.length ? c.title : question.slice(0, 48),
      messages: [
        ...c.messages,
        { role: "user", content: question },
        { role: "assistant", content: "", steps: [], status: "streaming" },
      ],
    }));
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await askStream(
        question,
        id,
        {
          step: ({ node, route }) =>
            patchLastMessage(id, (m) => ({ ...m, steps: [...m.steps, node], route: route || m.route })),
          token: ({ text }) => patchLastMessage(id, (m) => ({ ...m, content: m.content + text })),
          done: (d) =>
            patchLastMessage(id, (m) => ({
              ...m,
              content: d.answer,
              route: d.route,
              sources: d.sources,
              sql: d.sql,
              status: "done",
            })),
          error: ({ message }) => patchLastMessage(id, (m) => ({ ...m, status: "error", content: message })),
        },
        controller.signal
      );
    } catch (e) {
      const stopped = e.name === "AbortError";
      patchLastMessage(id, (m) => ({
        ...m,
        status: stopped ? "done" : "error",
        content: stopped ? m.content || "(génération arrêtée)" : e.message,
      }));
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  }

  function createConversation() {
    const conv = newConversation();
    setConversations((list) => [conv, ...list]);
    setActiveId(conv.id);
  }

  function removeConversation(id) {
    setConversations((list) => {
      const rest = list.filter((c) => c.id !== id);
      const next = rest.length ? rest : [newConversation()];
      if (id === activeId) setActiveId(next[0].id);
      return next;
    });
  }

  return (
    <div className="app">
      <Sidebar
        conversations={conversations}
        activeId={active.id}
        onSelect={setActiveId}
        onNew={createConversation}
        onDelete={removeConversation}
        disabled={busy}
      />
      <Chat
        conversation={active}
        busy={busy}
        onSend={send}
        onStop={() => abortRef.current?.abort()}
      />
    </div>
  );
}
