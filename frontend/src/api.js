// Client de l'API FastAPI. En dev, Vite redirige /api vers http://localhost:8001.

const API_KEY_STORAGE = "rag-v2-api-key";
const LOGIN_ATTEMPT_STORAGE = "rag-v2-login-attempt";

function loadApiKey() {
  try {
    return localStorage.getItem(API_KEY_STORAGE) || "";
  } catch {
    return "";
  }
}

let apiKey = loadApiKey();
let useApiKey = false;

function saveApiKey(key) {
  apiKey = key;
  try {
    localStorage.setItem(API_KEY_STORAGE, key);
  } catch {
    /* stockage indisponible : la cle reste en memoire jusqu'au rechargement */
  }
}

function forgetApiKey() {
  apiKey = "";
  try {
    localStorage.removeItem(API_KEY_STORAGE);
  } catch {
    /* stockage indisponible : rien a oublier */
  }
}

// Une connexion qui echoue relancerait la redirection sans fin : une seule tentative par 30 s.
function redirectToLogin(url) {
  try {
    const last = Number(sessionStorage.getItem(LOGIN_ATTEMPT_STORAGE) || 0);
    if (Date.now() - last < 30000) return false;
    sessionStorage.setItem(LOGIN_ATTEMPT_STORAGE, String(Date.now()));
  } catch {
    /* stockage indisponible : on tente quand meme */
  }
  window.location.assign(url);
  return true;
}

// Le navigateur n'envoie pas de cle d'emblee : si le serveur exige une connexion par compte, une
// cle gardee ne doit pas permettre de la contourner. Sans connexion par compte (developpement), on
// passe a la cle sur le premier 401 : celle deja gardee, sinon demandee une fois a l'utilisateur.
async function authFetch(url, options = {}) {
  const call = () =>
    fetch(url, { ...options, headers: { ...options.headers, ...(useApiKey ? { "X-API-Key": apiKey } : {}) } });
  let res = await call();
  if (res.status !== 401) return res;
  const loginUrl = await res
    .clone()
    .json()
    .then((body) => body.detail?.login_url)
    .catch(() => null);
  if (loginUrl) {
    forgetApiKey();
    useApiKey = false;
    if (redirectToLogin(loginUrl)) return new Promise(() => {}); // la page change : rien d'autre a afficher
    return res;
  }
  if (!useApiKey && apiKey) {
    useApiKey = true;
    res = await call();
    if (res.status !== 401) return res;
  }
  const entered = window.prompt("Clé d'accès à l'API :")?.trim();
  if (!entered) return res;
  saveApiKey(entered);
  useApiKey = true;
  return call();
}

async function request(path, options = {}) {
  const res = await authFetch(`/api${path}`, options);
  if (!res.ok) {
    let detail = `Erreur ${res.status}`;
    try {
      const body = (await res.json()).detail;
      detail = body?.message || body || detail;
    } catch {
      /* corps non JSON */
    }
    throw new Error(detail);
  }
  return res.json();
}

export const getHealth = () => request("/health");
export const getMe = () => request("/me");
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
