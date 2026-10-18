import { useEffect, useRef, useState } from "react";
import Message from "./Message.jsx";

const EXAMPLES = [
  "Quel est l'indice de protection du portique PV-300 ?",
  "Que faut-il faire avant d'ouvrir le capot ?",
  "Combien de temps sont conservés les objets trouvés ?",
  "Quel est le retard total cumulé sur la ligne T2 ?",
];

export default function Chat({ conversation, busy, onSend, onStop }) {
  const [input, setInput] = useState("");
  const bottomRef = useRef(null);
  const textareaRef = useRef(null);
  const messages = conversation.messages;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    textareaRef.current?.focus();
  }, [conversation.id]);

  function submit(text = input) {
    if (!text.trim() || busy) return;
    onSend(text.trim());
    setInput("");
  }

  return (
    <main className="chat">
      <div className="messages">
        {messages.length === 0 ? (
          <div className="welcome">
            <h1>Posez une question sur vos documents</h1>
            <p>
              L'assistant choisit seul la bonne source : <b>recherche dans les documents</b> (PDF, Word) ou{" "}
              <b>requête SQL</b> sur vos tableaux Excel. Il garde le fil de la conversation.
            </p>
            <div className="examples">
              {EXAMPLES.map((q) => (
                <button key={q} className="example" onClick={() => submit(q)}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((m, i) => <Message key={i} message={m} />)
        )}
        <div ref={bottomRef} />
      </div>

      <div className="composer">
        <textarea
          ref={textareaRef}
          rows={1}
          value={input}
          placeholder="Votre question… (Entrée pour envoyer, Maj+Entrée pour aller à la ligne)"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        {busy ? (
          <button className="send stop" onClick={onStop} title="Arrêter">
            ■
          </button>
        ) : (
          <button className="send" onClick={() => submit()} disabled={!input.trim()} title="Envoyer">
            ➤
          </button>
        )}
      </div>
    </main>
  );
}
