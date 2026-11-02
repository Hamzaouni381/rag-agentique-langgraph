// Client de l'API FastAPI. En dev, Vite redirige /api vers http://localhost:8001.

const API_KEY_STORAGE = "rag-v2-api-key";

function loadApiKey() {
  try {
    return localStorage.getItem(API_KEY_STORAGE) || "";
  } catch {
    return "";
  }
}

let apiKey = loadApiKey();

function saveApiKey(key) {
  apiKey = key;
  try {
    localStorage.setItem(API_KEY_STORAGE, key);
  } catch {
    /* stockage indisponible : la cle reste en memoire jusqu'au rechargement */
  }
}

// Envoie X-API-Key si on en a une. La cle n'est jamais dans le JS publie : sur un 401, on la
// demande une fois a l'utilisateur et elle reste dans son navigateur.
async function authFetch(url, options = {}) {
  const call = () =>
    fetch(url, { ...options, headers: { ...options.headers, ...(apiKey ? { "X-API-Key": apiKey } : {}) } });
  const res = await call();
  if (res.status !== 401) return res;
  const entered = window.prompt("Clé d'accès à l'API :")?.trim();
  if (!entered) return res;
  saveApiKey(entered);
  return call();
}

async function request(path, options = {}) {
  const res = await authFetch(`/api${path}`, options);
  if (!res.ok) {
    let detail = `Erreur ${res.status}`;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* corps non JSON */
    }
    throw new Error(detail);
  }
  return res.json();
}

export const getHealth = () => request("/health");
export const listDocuments = () => request("/documents");
export const deleteDocument = (name) => request(`/documents/${encodeURIComponent(name)}`, { method: "DELETE" });

export function uploadDocuments(files) {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  return request("/upload", { method: "POST", body: form });
}

// Lit le flux SSE de /ask/stream et appelle handlers.step / token / done / error.
export async function askStream(question, threadId, handlers, signal) {
  const res = await authFetch("/api/ask/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, thread_id: threadId }),
    signal,
  });
  if (!res.ok) throw new Error(`Erreur serveur ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = "message";
      let data = "";
      for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) handlers[event]?.(JSON.parse(data));
    }
  }
}
