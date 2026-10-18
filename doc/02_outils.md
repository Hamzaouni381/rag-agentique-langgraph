# 2. Les outils — définition, rôle, implémentation, à savoir

Chaque outil est présenté de la même façon :
- **C'est quoi** : la définition ;
- **Pourquoi ici** : le problème qu'il résout dans le projet ;
- **Comment on l'utilise** : l'implémentation ;
- **À savoir** : les pièges, les limites, ce qu'un expert doit connaître.

---

## 2.1 LangChain

**C'est quoi.** Un framework Python qui fournit des **briques standard** pour construire des applications LLM : modèles de chat, prompts, découpage de texte, vector stores, retrievers, chargeurs de documents… Toutes ces briques partagent une **interface commune**, le *Runnable* : chacune s'appelle avec `.invoke()`, `.stream()` et `.batch()`, et on peut les enchaîner avec l'opérateur `|`.

**L'écosystème est découpé en paquets** (depuis LangChain 1.0, octobre 2025) :

| Paquet | Contenu | Utilisé ici pour |
|---|---|---|
| `langchain-core` | Les abstractions : `Document`, messages, prompts, `Runnable`, `Embeddings`, `BaseLoader`, API `index()` | Partout |
| `langchain` (1.x) | Le cœur « agent » : `init_chat_model`, `create_agent` | `init_chat_model` |
| `langchain-classic` | Les anciennes briques de LangChain 0.x, déplacées : `EnsembleRetriever`, `SQLRecordManager`… | Fusion hybride, registre d'indexation |
| `langchain-community` | Intégrations communautaires : `BM25Retriever`, `SQLDatabase`, `HuggingFaceCrossEncoder`, loaders | BM25, SQL, reranker, repli PDF |
| `langchain-text-splitters` | Découpeurs de texte | `RecursiveCharacterTextSplitter` |
| `langchain-openai`, `langchain-ollama`, `langchain-huggingface`, `langchain-postgres` | Paquets « partenaires », un par fournisseur | LLM, embeddings, pgvector |

**Pourquoi ici.**
- **Changer de fournisseur** (Ollama ↔ OpenAI) se fait en changeant une ligne de configuration.
- On remplace des centaines de lignes de plomberie par des briques connues de tous les recruteurs et de toutes les équipes.

**Comment on l'utilise : la composition par `|` (LCEL).**
```
chaîne = PROMPT | LLM | StrOutputParser()
réponse = chaîne.invoke({"question": "...", "context": "..."})
```
1. Le **prompt** remplit son modèle avec les variables et produit une liste de messages (système + utilisateur).
2. Le **LLM** reçoit ces messages et renvoie un message IA.
3. Le **parser** extrait le texte de ce message.

Chaque appel LLM du graphe est une petite chaîne de ce type.

**À savoir.**
- **LangChain 1.0 a déplacé beaucoup de choses** vers `langchain-classic`. Un tutoriel de 2024 qui importe `from langchain.retrievers import EnsembleRetriever` ne marche plus : il faut `from langchain_classic.retrievers import ...`.
- Le `Runnable` donne **gratuitement** le streaming, le traitement par lots et le traçage LangSmith pour chaque brique.
- **Critique à connaître** : LangChain ajoute une couche d'abstraction. Sur un besoin simple, un appel direct au SDK est plus lisible. Ici on l'utilise pour les briques à forte valeur (retrievers, vector store, indexation) et pour l'interchangeabilité des fournisseurs.

---

## 2.2 LangGraph

**C'est quoi.** Une bibliothèque, de la même équipe que LangChain, pour construire des **agents sous forme de graphe d'états**. On y définit trois choses :
- un **état** : un dictionnaire typé, partagé par toutes les étapes ;
- des **nœuds** : des fonctions Python qui lisent l'état et renvoient les champs à mettre à jour ;
- des **arêtes** : des transitions, fixes (« après A, toujours B ») ou **conditionnelles** (« après A, une fonction choisit B ou C »).

Les arêtes conditionnelles permettent les **boucles** : reformuler puis rechercher à nouveau, corriger le SQL puis réexécuter.

**Pourquoi ici.**
- **Le contrôle.** Un agent « libre », où le LLM appelle des outils en boucle comme il veut (le style *ReAct*), est imprévisible et coûteux. C'est encore pire avec un petit modèle local, qui gère mal les appels d'outils. Un graphe explicite garde la souplesse (choix de branche, boucles) tout en **bornant** le comportement : au plus 1 reformulation, au plus 1 correction SQL.
- **La lisibilité et les tests.** Chaque nœud est une fonction testable isolément. Le graphe se dessine automatiquement (`graph.get_graph().draw_mermaid()`).
- **La mémoire intégrée.** Le *checkpointer* sauvegarde l'état après chaque étape, ce qui donne la mémoire de conversation sans écrire de code de persistance.
- **Le streaming** : on peut diffuser les étapes terminées et les mots générés.

**Comment on l'utilise.**
- **L'état** (`GraphState`) contient : `messages` (l'historique), `question`, `standalone`, `search_query`, `route`, `documents`, `rewrites`, `sql`, `sql_result`, `sql_error`, `sql_attempts`, `answer`, `sources`.
- **Les 9 nœuds** : `contextualize`, `route`, `retrieve`, `rewrite`, `generate`, `write_sql`, `execute_sql`, `answer_sql`, `chat`.
- **Les 3 décisions conditionnelles** :
  - après `route` : documents, database ou chat ;
  - après `retrieve` : pertinent ou pas ;
  - après `execute_sql` : erreur ou pas.
- **La compilation** : `graph.compile(checkpointer=PostgresSaver(...))`.
- **L'appel** : `graph.invoke({"question": ..., "messages": [HumanMessage(...)]}, {"configurable": {"thread_id": "..."}})`.

Détail complet au [chapitre 5](05_graphe_langgraph.md).

**À savoir.**
- **Les reducers.** Par défaut, la valeur renvoyée par un nœud **remplace** le champ correspondant. Le champ `messages` est annoté avec le reducer `add_messages` : ses valeurs sont **ajoutées** à la liste au lieu de la remplacer. C'est ce qui accumule l'historique.
- **Le checkpointer sauvegarde à chaque étape** (*super-step*), pas seulement à la fin. Une conversation de quelques questions produit des dizaines de checkpoints : on en a mesuré 53 pour une conversation de test. Sans purge, la table grossit indéfiniment (voir [chapitre 8](08_exploitation.md)).
- **Les modes de streaming** : `updates` (ce que chaque nœud a modifié), `messages` (les mots générés par le LLM), `values` (l'état complet). On combine `updates` et `messages`.
- **Ne pas confondre** : *LangGraph* est la bibliothèque (gratuite, open source), *LangGraph Platform/Studio* est l'offre d'hébergement et de visualisation.

---

## 2.3 PostgreSQL + pgvector

**C'est quoi.**
- **PostgreSQL** est la base de données relationnelle open source de référence.
- **pgvector** est une **extension** qui ajoute un type de colonne `vector` et des opérateurs de distance : cosinus `<=>`, euclidienne `<->`, produit scalaire `<#>`. PostgreSQL devient ainsi aussi une base vectorielle.

**Pourquoi ici (et pas ChromaDB, Pinecone…).**
- **Une seule base pour tout** : vecteurs, tables métier issues d'Excel, mémoire des conversations et registre d'indexation. Il n'y a qu'une seule chose à sauvegarder, sécuriser, superviser et déployer.
- **Les transactions, les droits** (rôles) et le SQL standard : la sécurité du Text-to-SQL repose sur un rôle en lecture seule, impossible à faire avec une base vectorielle spécialisée.
- **Le standard en entreprise.** Les clouds proposent pgvector en service managé (Azure Database for PostgreSQL, AWS RDS, Supabase…).

**Comment on l'utilise.**
- Le conteneur Docker `pgvector/pgvector:pg16` (PostgreSQL 16 avec l'extension préinstallée).
- Le script `docker/init.sql` est exécuté **une seule fois**, à la création du volume. Il :
  - active l'extension `vector` ;
  - crée le schéma `tabular` ;
  - crée le rôle `rag_reader` (lecture seule).
- **Les tables présentes en base** :

| Table | Créée par | Contenu |
|---|---|---|
| `langchain_pg_collection` | langchain-postgres | Les « collections » (ici une seule : `documents`) |
| `langchain_pg_embedding` | langchain-postgres | Une ligne par chunk : `embedding` (vecteur), `document` (texte), `cmetadata` (JSON : source, page…) |
| `upsertion_record` | SQLRecordManager | Le registre d'indexation : empreinte de chaque chunk indexé + fichier d'origine |
| `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations` | PostgresSaver (LangGraph) | La mémoire des conversations |
| `tabular.<nom>` | ingestion pandas | Une table par feuille Excel ou fichier CSV |

**À savoir.**
- **Aucun index vectoriel n'est créé** (vérifié en base : seuls un index B-tree sur l'id et un index GIN sur les métadonnées existent). La recherche est donc **exacte** : elle compare la question à **tous** les vecteurs. C'est parfait jusqu'à quelques dizaines de milliers de chunks. Au-delà, il faut un index **HNSW** (recherche approximative, très rapide). Le piège : la colonne est déclarée `vector` sans dimension, il faut donc l'indexer avec un cast, par exemple `CREATE INDEX ON langchain_pg_embedding USING hnsw ((embedding::vector(384)) vector_cosine_ops)`. Le même cast doit alors figurer dans les requêtes. Sinon, il faut recréer la table avec une dimension fixe.
- **La dimension** dépend du modèle d'embedding (384 pour e5-small, 1536 pour `text-embedding-3-small` d'OpenAI). **Changer de modèle oblige à tout ré-indexer.**
- **La distance utilisée est le cosinus**. Les vecteurs sont normalisés à la création, donc cosinus et produit scalaire donnent le même classement.

---

## 2.4 langchain-postgres (PGVector)

**C'est quoi.** Le paquet officiel qui implémente le vector store LangChain au-dessus de PostgreSQL et pgvector.

**Comment on l'utilise.**
- **La création** : `PGVector(embeddings=..., collection_name="documents", connection=engine, use_jsonb=True)`.
- **La recherche** : `.as_retriever(search_kwargs={"k": 20})` donne un retriever qui renvoie les 20 chunks les plus proches.
- `use_jsonb=True` stocke les métadonnées en JSONB, ce qui les rend filtrables et indexées par l'index GIN.

**À savoir.**
- Le paquet propose aussi une classe plus récente, `PGVectorStore` (avec un `PGEngine`), qui permet de choisir le schéma de table. `PGVector` reste la plus simple et la plus répandue dans la documentation.
- On peut **filtrer par métadonnées** (`search_kwargs={"filter": {"source": "x.pdf"}}`), par exemple pour cloisonner les documents par utilisateur ou par client.

---

## 2.5 L'API d'indexation `index()` + SQLRecordManager

**C'est quoi.** Un mécanisme de LangChain pour **synchroniser** un ensemble de documents avec un vector store, sans doublons. Le *RecordManager* tient un registre : pour chaque chunk, une **empreinte** (hash SHA-256 du contenu et des métadonnées) et le **fichier d'origine** (`group_id`).

**Pourquoi ici.** Sans lui, relancer l'ingestion ajoute une seconde copie de chaque chunk. Les résultats de recherche sont alors pollués par des doublons, et un fichier modifié laisse ses anciens chunks en base.

**Comment ça marche.** À chaque ingestion, `index(chunks, record_manager, vectorstore, cleanup=..., source_id_key="source", key_encoder="sha256")` :
1. calcule l'empreinte de chaque chunk ;
2. **ignore** les chunks déjà connus (empreinte présente dans le registre) ;
3. **ajoute** les nouveaux chunks ;
4. **nettoie** selon le mode choisi :
   - `incremental` : supprime les anciens chunks des fichiers **ré-ingérés** dont le contenu a changé ;
   - `full` : supprime en plus les chunks des fichiers **qui ne sont plus dans le dossier**. C'est ce mode qu'on utilise quand on supprime un document depuis l'interface.

**Vérifié** : deux ingestions successives donnent `num_added: 5` puis `num_skipped: 5`.

**À savoir.**
- Le registre et le vector store doivent rester **synchronisés**. Si on vide la table des vecteurs à la main sans vider `upsertion_record`, l'ingestion croira que tout est déjà indexé.
- **Le mode `full` avec un dossier vide supprime tout.** C'est voulu, mais il faut le savoir.

---

## 2.6 Embeddings : sentence-transformers / HuggingFace (`intfloat/multilingual-e5-small`)

**C'est quoi.** Un modèle d'embedding transforme un texte en un vecteur de nombres. Deux textes de sens proche donnent deux vecteurs proches, même s'ils n'emploient pas les mêmes mots (« étanche » ≈ « indice de protection IP54 »).

**Le modèle choisi.** `multilingual-e5-small`, de Microsoft :
- **384 dimensions** ;
- **multilingue**, donc bon en français ;
- **léger** (≈ 470 Mo), il tourne sur un processeur classique.

Il est utilisé via `langchain-huggingface` (`HuggingFaceEmbeddings`), qui s'appuie sur la bibliothèque `sentence-transformers`.

**À savoir (très important).**
- **Les préfixes e5.** Ce modèle a été entraîné avec des préfixes : `"query: "` devant les questions et `"passage: "` devant les documents. Les oublier dégrade nettement la recherche. On les passe en paramètres (`encode_kwargs={"prompt": "passage: "}` pour les documents, `query_encode_kwargs={"prompt": "query: "}` pour les questions). D'autres modèles, comme bge ou OpenAI, n'en veulent pas : il faut alors vider `EMBEDDING_QUERY_PREFIX` et `EMBEDDING_PASSAGE_PREFIX`.
- **La normalisation** (`normalize_embeddings=True`) : les vecteurs sont ramenés à une longueur de 1, ce qui rend le cosinus cohérent.
- **L'alternative OpenAI** : `EMBEDDING_PROVIDER=openai` utilise `text-embedding-3-small` (1536 dimensions, payant, meilleure qualité). Cela demande une **ré-ingestion complète**.
- Au premier lancement, le modèle est téléchargé depuis HuggingFace puis mis en cache dans `~/.cache/huggingface`.

---

## 2.7 BM25 et la recherche hybride (EnsembleRetriever, RRF)

**C'est quoi.**
- **BM25** : algorithme de recherche **par mots-clés**, le classique des moteurs de recherche. Il note un document selon la présence des mots de la question : un mot rare pèse plus, un mot répété apporte des points mais avec saturation, et les documents longs sont pénalisés.
- **Recherche hybride** : on lance **en parallèle** la recherche vectorielle (le sens) et BM25 (les mots exacts), puis on **fusionne** les deux classements.
- **RRF (Reciprocal Rank Fusion)** : la méthode de fusion. Chaque document reçoit `Σ poids / (60 + rang)` sur les deux listes. Elle ne compare que des **rangs**, pas des scores, ce qui évite de mélanger des échelles incompatibles (une distance cosinus et un score BM25).

**Pourquoi ici.** Les embeddings sont mauvais sur les **références exactes** : numéros de série, codes (« PV-300 », « SN-48213-B »), acronymes. BM25 les retrouve parfaitement. Les deux méthodes se complètent.

**Comment on l'utilise.**
- `BM25Retriever` (langchain-community) est construit **en mémoire** à partir de tous les chunks lus dans PostgreSQL.
- `EnsembleRetriever(retrievers=[vecteur, bm25], weights=[0.6, 0.4])` : le sens pèse un peu plus que les mots-clés.

**À savoir.**
- **L'index BM25 vit dans la mémoire du processus Python.** Il est construit à la première question, puis invalidé et reconstruit après chaque ingestion. Avec **plusieurs workers ou serveurs**, chacun a son propre index, et seul celui qui a fait l'ingestion est invalidé. En production multi-instances, il faut passer à la recherche plein texte native de PostgreSQL (`tsvector`, index GIN), qui reste en base.
- BM25 tient tout le corpus en RAM : c'est adapté à quelques dizaines de milliers de chunks.
- Désactivable avec `HYBRID_SEARCH=false`.

---

## 2.8 Le reranker (cross-encoder `BAAI/bge-reranker-base`)

**C'est quoi.** Il y a deux familles de modèles :
- un **bi-encoder** (le modèle d'embedding) encode la question et le passage **séparément**, puis compare les vecteurs. C'est rapide, mais approximatif ;
- un **cross-encoder** lit la question et le passage **ensemble**, dans la même entrée, et produit un score de pertinence. C'est beaucoup plus précis, mais trop lent pour parcourir tout le corpus.

**La stratégie classique en deux étages** :
1. la recherche hybride ramène une quarantaine de candidats, vite ;
2. le cross-encoder les **reclasse** précisément et on garde les **5 meilleurs** (`TOP_N`).

**Comment on l'utilise.** On utilise `HuggingFaceCrossEncoder` (langchain-community) avec le modèle `BAAI/bge-reranker-base` (≈ 1,1 Go, multilingue). Le score de chaque passage, entre 0 et 1, est **conservé** dans ses métadonnées (`relevance`).

**Pourquoi garder le score.** Il sert de **juge de pertinence** au graphe : si le meilleur score est sous `RELEVANCE_THRESHOLD` (0,15), le contexte est jugé hors sujet. On reformule alors, ou on refuse de répondre. On évite ainsi un appel LLM « juge » coûteux par document, ce que font beaucoup de tutoriels de *Corrective RAG*.

**À savoir.**
- Les scores mesurés sont nets : **0,93 à 0,97** pour le bon passage, **< 0,06** pour les autres.
- Le seuil de 0,15 est un **réglage empirique** : trop haut, on refuse des réponses valides ; trop bas, on laisse passer du hors-sujet.
- C'est l'étape la plus coûteuse en CPU (environ 20 s au premier appel, le temps de charger le modèle, puis environ 1 s).

---

## 2.9 Ollama, OpenAI et `init_chat_model`

**C'est quoi.**
- **Ollama** : un serveur local qui télécharge et exécute des LLM open source (Qwen, Llama, Mistral…) avec une API HTTP sur le port 11434. Il ne coûte rien, et aucune donnée ne sort de la machine.
- **OpenAI API** : des modèles hébergés (GPT-4o-mini…), payants à l'usage, de bien meilleure qualité.
- **`init_chat_model`** (LangChain 1.x) : une fonction unique qui crée le bon client selon le fournisseur (`model_provider="ollama"` ou `"openai"`). Le reste du code ne voit qu'un `BaseChatModel` générique.

**Comment on l'utilise.**
- **`LLM_PROVIDER=ollama`** : modèle `qwen2.5:1.5b`, température 0,1 (réponses stables, peu créatives), `num_ctx=4096` (taille de la fenêtre de contexte), **`num_predict=768`** (nombre maximal de mots générés).
- **`LLM_PROVIDER=openai`** + `OPENAI_API_KEY` : modèle `gpt-4o-mini`, `max_tokens=768`.

**À savoir (leçons apprises en testant).**
- **Il faut toujours plafonner la génération.** Sans `num_predict`, Ollama ne limite pas la longueur. Un petit modèle peut alors boucler en répétant la même phrase jusqu'à remplir le contexte : un test a ainsi bloqué **20 minutes**.
- **La taille du modèle compte énormément pour un agent.** `qwen2.5:0.5b` répond mais **raisonne mal** : il route mal, et il répond au lieu de reformuler. Mesure faite : 4/8 avec le 0.5b, **8/9** avec le 1.5b, sur les mêmes questions.
- Sur CPU, compte 5 à 15 s par réponse avec le 1.5b. Le premier appel est plus lent, le temps que le modèle se charge en mémoire.

---

## 2.10 LangSmith

**C'est quoi.** La plateforme d'**observabilité et d'évaluation** de LangChain (service cloud, offre gratuite pour un développeur). Elle offre deux choses :
- **les traces** : chaque exécution du graphe apparaît comme un arbre (nœud → prompt envoyé → réponse du LLM → durée, tokens, erreurs) ;
- **les datasets et l'évaluation** : on enregistre un jeu de questions, on lance le système dessus, on calcule des métriques. Chaque lancement devient une « expérience » comparable aux précédentes.

**Comment on l'utilise.**
- **Pour les traces, aucun code** : il suffit de 3 variables d'environnement (`LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`). LangChain et LangGraph envoient les traces automatiquement.
- **Pour l'évaluation**, le script `eval/run_eval.py` : s'il trouve une clé, il crée le dataset et lance `client.evaluate(...)` ; sinon, il fait la même évaluation en local et écrit un CSV.

**À savoir.**
- **En production, c'est l'outil n°1 pour comprendre une mauvaise réponse** : on voit si le problème vient du routage, de la recherche (mauvais passages) ou de la génération (bons passages mais mauvaise réponse).
- **Les traces contiennent les données** (questions, passages). Pour des documents confidentiels, il faut vérifier la politique de l'entreprise : LangSmith peut être auto-hébergé, ou les entrées et sorties masquées.

---

## 2.11 FastAPI et SSE

**C'est quoi.**
- **FastAPI** est un framework web Python. Il apporte :
  - la validation automatique des requêtes par des modèles **Pydantic** ;
  - une documentation interactive générée automatiquement (`/docs`) ;
  - de bonnes performances.
- **SSE (Server-Sent Events)** est un format de streaming HTTP simple. Le serveur garde la connexion ouverte et envoie des blocs `event: <type>\ndata: <json>\n\n` au fil de l'eau.

**Pourquoi SSE (et pas WebSocket).** On a besoin d'un flux **unidirectionnel** (serveur → navigateur) pendant une réponse. SSE passe par du HTTP normal (proxys, authentification, logs inchangés), et c'est le standard des API LLM : OpenAI comme Anthropic streament en SSE.

**Comment on l'utilise.** Toutes les routes sont sous `/api`. La route `/api/ask/stream` renvoie une `StreamingResponse` qui émet 4 types d'événements : `step`, `token`, `done` et `error` (détail au [chapitre 7](07_api_et_interface.md)).

---

## 2.12 pydantic-settings

**C'est quoi.** Une bibliothèque qui lit la configuration depuis les **variables d'environnement** et le fichier **`.env`**, puis la **valide et la convertit** automatiquement (texte → nombre, booléen, liste de choix).

**Comment on l'utilise.** La classe `Settings` (`app/config.py`) déclare chaque réglage avec son type et sa valeur par défaut. La variable d'environnement `TOP_N=5` remplit automatiquement le champ `top_n`, sans tenir compte de la casse. Une valeur invalide, comme `LLM_PROVIDER=mistral`, fait planter le démarrage avec un message clair, plutôt que de provoquer un bug plus loin.

---

## 2.13 React + Vite

**C'est quoi.**
- **React** est une bibliothèque JavaScript pour construire des interfaces à partir de **composants** : des fonctions qui affichent un morceau d'écran à partir de données.
- **Vite** est l'outil de développement et de compilation. Il recharge l'interface instantanément en développement, et `npm run build` produit des fichiers statiques optimisés dans `frontend/dist`.

**Comment on l'utilise.** Il y a 4 composants : `App`, `Sidebar`, `Chat` et `Message`, plus un module `api.js`. En production, **FastAPI sert directement `frontend/dist`** : une seule adresse, et pas de problème de CORS. Détail au [chapitre 7](07_api_et_interface.md).

---

## 2.14 Docker et Docker Compose

**C'est quoi.**
- **Docker** exécute des applications dans des **conteneurs** isolés, avec leurs dépendances.
- **Compose** décrit plusieurs conteneurs dans un fichier YAML et les lance ensemble.

**Comment on l'utilise.**
- **`docker compose up -d db`** lance uniquement PostgreSQL. C'est le mode de développement : le serveur Python tourne sur la machine.
- **`docker compose --profile full up --build`** lance PostgreSQL **et** l'API conteneurisée. Le profil `full` évite de lancer l'API par erreur.
- **Le `Dockerfile` est multi-étapes** :
  1. une image Node compile l'interface React ;
  2. une image Python installe torch CPU puis les dépendances ;
  3. on copie seulement le résultat compilé de l'interface.

  L'image finale ne contient donc ni Node ni les sources de l'interface.
- **Les volumes** `pgdata` et `hf-cache` gardent les données et les modèles téléchargés d'un redémarrage à l'autre.

**À savoir.** On installe **torch en version CPU d'abord**. Sinon, `sentence-transformers` tire la version CUDA (≈ 2 Go de plus), inutile sans GPU NVIDIA.

---

## 2.15 pytest, ruff et GitHub Actions

- **pytest** est le framework de tests Python. Il y a 27 tests ; ils n'ont besoin ni de LLM ni de base de données, grâce à l'injection de dépendances ([chapitre 9](09_qualite.md)).
- **ruff** est un linter Python ultra-rapide. Il vérifie le style, les erreurs probables et l'ordre des imports. Les règles sont configurées dans `pyproject.toml` : E, F, I, UP et B.
- **GitHub Actions** est la CI de GitHub. À chaque push ou pull request, le fichier `.github/workflows/ci.yml` lance le lint, les tests, puis le build de l'image Docker. Un commit qui casse un test est signalé immédiatement.
