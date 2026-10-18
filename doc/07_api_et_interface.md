# 7. L'API et l'interface web

## 7.1 Les routes de l'API (FastAPI, préfixe `/api`)

| Méthode | Route | Rôle | Entrée | Sortie |
|---|---|---|---|---|
| GET | `/api/health` | État du système | — | `{"database": "ok", "llm": "ollama", "model": "qwen2.5:1.5b", "embeddings": "huggingface"}` |
| GET | `/api/documents` | Liste des fichiers indexables | — | `[{"name", "size", "kind": "document" \| "table"}]` |
| POST | `/api/upload` | Déposer des fichiers **et** les indexer | formulaire multipart `files` | fichiers enregistrés + statistiques d'ingestion |
| DELETE | `/api/documents/{nom}` | Supprimer un fichier **et** le retirer de l'index | — | statistiques d'ingestion (mode `full`) |
| POST | `/api/ingest` | Ré-indexer tout le dossier | `?full_cleanup=true` (optionnel) | statistiques d'ingestion |
| POST | `/api/ask` | Poser une question (réponse complète d'un coup) | `{"question", "thread_id"}` | `{"answer", "route", "thread_id", "sources", "sql"}` |
| POST | `/api/ask/stream` | Poser une question (réponse **en streaming**) | `{"question", "thread_id"}` | flux SSE |

- La **documentation interactive** générée par FastAPI est sur `http://localhost:8001/docs`.
- **Toutes les autres URL** (`/`, `/assets/…`) servent l'interface React compilée (`frontend/dist`).

**La sécurité de l'API.**
- Si `API_KEY` est défini dans `.env`, toutes les routes (sauf `/health`) exigent l'en-tête `X-API-Key`.
- **L'upload** n'accepte que les extensions autorisées.
- **La suppression** utilise uniquement le **nom** du fichier (`Path(nom).name`) : un nom comme `../../config` ne peut pas sortir du dossier des documents (protection contre le *path traversal*).
- **La question** est limitée à 2 000 caractères (validation Pydantic).

## 7.2 Le protocole de streaming (`/api/ask/stream`)

La réponse est un flux d'événements SSE. Chaque événement a un **type** et des **données JSON** :

```
event: step     data: {"node": "contextualize"}
event: step     data: {"node": "route", "route": "database"}
event: step     data: {"node": "write_sql"}
event: step     data: {"node": "execute_sql"}
event: token    data: {"text": "Le"}
event: token    data: {"text": " retard"}
   …  (un événement par fragment de texte généré)
event: step     data: {"node": "answer_sql"}
event: done     data: {"answer": "...84 minutes.", "route": "database", "thread_id": "...",
                       "sources": [], "sql": "SELECT SUM(retard_min) ..."}
```

| Événement | Quand | Usage dans l'interface |
|---|---|---|
| `step` | Chaque fois qu'un nœud du graphe se termine | Affiche « ✓ Recherche dans les documents » en direct ; le nœud `route` donne aussi la route choisie |
| `token` | Chaque fragment généré par le LLM dans un nœud de réponse (`generate`, `answer_sql`, `chat`) | Affiche le texte au fur et à mesure |
| `done` | À la fin | Remplace le texte par la réponse finale, affiche les sources et le SQL |
| `error` | En cas d'exception | Affiche l'erreur dans une bulle rouge au lieu de couper la connexion |

**Comment c'est produit côté serveur.**
- On appelle `graph.stream(..., stream_mode=["updates", "messages"])` :
  - le mode `updates` signale la fin de chaque nœud ;
  - le mode `messages` transmet les fragments générés par le LLM.
- **Le filtre important** : on ne transmet que les fragments de type `AIMessageChunk`, venus **des nœuds de réponse**. En effet, LangGraph émet aussi le message complet quand un nœud l'ajoute à l'état. Sans ce filtre, la réponse s'affichait **en double** ; le bug a été observé puis corrigé.
- **Les tokens des nœuds internes ne sont pas diffusés** (routage, reformulation, SQL) : l'utilisateur ne voit pas « documents » ou une requête SQL s'écrire dans sa réponse.

## 7.3 L'interface React (`frontend/`)

### Les composants
```
App.jsx            état global : conversations, conversation active, envoi d'une question
├─ Sidebar.jsx     barre latérale : nouvelle conversation, liste des conversations,
│                  dépôt/liste/suppression des documents, voyant d'état du serveur
└─ Chat.jsx        zone de chat : page d'accueil avec exemples, liste des messages, zone de saisie
   └─ Message.jsx  un message : badge de route, étapes, réponse (Markdown), sources avec score, SQL
api.js             appels à l'API + lecture du flux SSE
styles.css         mise en forme
```

### Le cycle d'une question dans l'interface
1. L'utilisateur tape sa question et appuie sur Entrée.
2. `App` ajoute deux messages à la conversation : la question, et une réponse **vide** au statut `streaming`.
3. `api.js` appelle `/api/ask/stream` avec `thread_id = identifiant de la conversation`.
4. **Pour chaque événement reçu** :
   - `step` → le nom du nœud est ajouté aux étapes de la réponse (et la route est mémorisée) ;
   - `token` → le texte est ajouté au contenu de la réponse ;
   - `done` → le contenu est remplacé par la réponse finale, les sources et le SQL sont ajoutés, et le statut passe à `done` ;
   - `error` → statut `error`, message affiché en rouge.
5. Le bouton ■ (Arrêter) annule la requête (`AbortController`).

**La lecture du flux** : on n'utilise pas `EventSource`, l'API standard du navigateur, car elle ne sait faire que des requêtes GET. `api.js` utilise donc `fetch` en POST et lit le corps morceau par morceau. Il découpe sur les doubles sauts de ligne (`\n\n`, fin d'un événement SSE), sépare `event:` et `data:`, puis appelle le bon gestionnaire.

### Ce que voit l'utilisateur
- **Un badge de route** : *Documents* (bleu), *Base de données* (vert) ou *Discussion* (violet).
- **Pendant le traitement** : la liste des étapes cochées et une roue « En cours… ».
- **Après** : le parcours complet sur une ligne (« Analyse de la question → Choix de la source → Recherche dans les documents → Rédaction de la réponse »). On voit ainsi s'il y a eu une reformulation.
- **Les sources** : fichier, page et barre de pertinence en %. C'est le score du reranker.
- **« ▸ Requête SQL exécutée »** : un bloc dépliable avec le SQL.

### Où sont stockées les conversations
- **Côté navigateur** (`localStorage`) : la liste des conversations et leurs messages, pour l'affichage.
- **Côté serveur** (PostgreSQL, checkpointer) : la **mémoire** utilisée par le LLM, indexée par `thread_id`.

**Conséquence** : vider le cache du navigateur efface l'affichage, mais pas la mémoire serveur. Inversement, supprimer une conversation dans l'interface ne purge pas les checkpoints en base.

### Développement et production
- **Production** : `npm run build` produit `frontend/dist`, que FastAPI sert sur le **même port** que l'API (8001). C'est ce que fait `demarrer.bat`.
- **Développement** : `npm run dev` lance Vite sur le port 5173 avec rechargement à chaud. Vite **redirige** les appels `/api` vers le port 8001 (le *proxy* défini dans `vite.config.js`), donc il n'y a aucune configuration CORS à gérer.
