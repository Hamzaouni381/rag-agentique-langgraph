# 1. Vue d'ensemble

## 1.1 Le problème résolu

Dans une entreprise, l'information est éparpillée :
- dans des **documents** (procédures, manuels, règlements, rapports) au format PDF ou Word ;
- dans des **tableaux** (Excel, CSV) : incidents, ventes, mesures…

Un utilisateur veut poser une question en français, sans savoir **où** est l'information ni **comment** la chercher :
- « Que faut-il faire avant d'ouvrir le capot du portique ? » → la réponse est dans un **document Word**.
- « Quel est le retard total cumulé sur la ligne T2 ? » → la réponse est un **calcul** sur un **tableau Excel**.

Le système doit :
1. comprendre la question, y compris quand elle fait référence à la précédente (« Et sa température ? ») ;
2. **choisir seul** la bonne source (documents ou tableaux) ;
3. retrouver l'information ou la calculer ;
4. répondre **sans inventer**, en citant ses sources, et dire « je ne sais pas » quand l'information n'existe pas.

## 1.2 Le pitch en 30 secondes

> « C'est un assistant RAG agentique. Un graphe LangGraph analyse chaque question et choisit entre trois stratégies. La première est une recherche documentaire hybride : vecteurs pgvector plus BM25, puis un reranker cross-encoder, avec une boucle corrective qui reformule la question si le contexte trouvé n'est pas pertinent. La deuxième est un Text-to-SQL qui corrige lui-même ses erreurs, sur les tableaux Excel importés dans PostgreSQL. La troisième est une simple réponse conversationnelle. Tout est stocké dans un seul PostgreSQL : les vecteurs, les tables et la mémoire des conversations. Le tout est exposé par une API FastAPI en streaming, avec une interface React, tracé dans LangSmith, testé et intégré en CI. »

## 1.3 Architecture générale

```
┌──────────────────────────────────────────────────────────────────────────┐
│ NAVIGATEUR — Interface React (chat, étapes en direct, sources, SQL)       │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ HTTP  /api/...   (réponses en streaming SSE)
┌───────────────────────────────▼──────────────────────────────────────────┐
│ SERVEUR — FastAPI (port 8001)                                             │
│   /api/ask/stream ──► GRAPHE LANGGRAPH (le "cerveau" agentique)           │
│                         contextualize → route ─┬─► branche RAG            │
│                                                ├─► branche SQL            │
│                                                └─► branche chat           │
│   /api/upload ──────► INGESTION (parsing, découpage, embeddings)          │
└───────┬───────────────────────┬────────────────────────────┬─────────────┘
        │                       │                            │
┌───────▼────────┐   ┌──────────▼───────────┐   ┌────────────▼─────────────┐
│ OLLAMA (local) │   │ MODÈLES HUGGINGFACE  │   │ POSTGRESQL + pgvector     │
│ LLM qwen2.5    │   │ (dans le process)    │   │ (conteneur Docker)        │
│ ou OpenAI API  │   │ • embeddings e5-small│   │ • vecteurs des chunks     │
│                │   │ • reranker bge-base  │   │ • tables Excel (tabular)  │
└────────────────┘   └──────────────────────┘   │ • mémoire conversations   │
                                                │ • registre d'indexation   │
        LangSmith (cloud, optionnel) ◄── traces └───────────────────────────┘
```

**Les 4 composants qui tournent** :
1. **PostgreSQL** dans un conteneur Docker. C'est la seule base de données.
2. **Ollama**, le serveur qui fait tourner le LLM en local. On peut le remplacer par l'API OpenAI.
3. **Le serveur Python** (FastAPI). Il contient le graphe, l'ingestion, et les modèles d'embedding et de reranking chargés en mémoire.
4. **Le navigateur**, où tourne l'interface React. Celle-ci est servie par le serveur Python lui-même.

## 1.4 Les deux grands flux

### Flux 1 — Ingestion (on prépare les données, une fois)
```
fichier PDF/Word ──► parseur structuré ──► pages en Markdown ──► découpage en chunks
     ──► embedding de chaque chunk (vecteur de 384 nombres) ──► PostgreSQL (pgvector)

fichier Excel/CSV ──► pandas ──► une table SQL par feuille ──► PostgreSQL (schéma tabular)
```

### Flux 2 — Interrogation (à chaque question)
```
question ──► [contextualize] rendre la question autonome grâce à l'historique
         ──► [route] choisir : documents / database / chat
         ──► documents : rechercher → noter la pertinence → (reformuler et rechercher) → rédiger
         ──► database  : écrire le SQL → l'exécuter → (corriger si erreur) → commenter le résultat
         ──► chat      : répondre directement
         ──► réponse + sources + SQL, sauvegardée dans la mémoire de la conversation
```

## 1.5 Le trajet complet d'une question (exemple réel)

Conversation réelle sur le corpus de démo, avec le modèle `qwen2.5:1.5b`.

**Tour 1 — « Quel est l'indice de protection du portique PV-300 ? »**
1. **contextualize** : c'est le premier message de la conversation, il n'y a pas d'historique. La question est donc gardée telle quelle.
2. **route** : le LLM répond `documents`. Les garde-fous confirment ce choix.
3. **retrieve** :
   - la recherche vectorielle et BM25 sortent chacune jusqu'à 20 candidats ;
   - la fusion RRF les combine ;
   - le reranker note chaque passage et garde les 5 meilleurs. Le meilleur, le chunk « Caractéristiques techniques » de `procedure_PV300.docx`, obtient un score de **0,93**.
4. **grade** : 0,93 ≥ seuil de 0,15, le contexte est donc pertinent. On passe à `generate`.
5. **generate** : le LLM reçoit les 5 passages et les règles (« réponds UNIQUEMENT avec les passages, copie les valeurs exactement… ») et répond **« L'indice de protection du portique PV-300 est IP54. »**
6. La question et la réponse sont ajoutées à la mémoire du `thread_id`.

**Tour 2 — « Et sa température de fonctionnement ? »**
1. **contextualize** : il y a un historique, donc le LLM doit rendre la question autonome. Le système vérifie que le résultat ressemble bien à une question. Sinon, comme c'est une relance (« Et… sa… »), il concatène la question précédente pour garder le sujet (le PV-300).
2. **route** → `documents` ; **retrieve** → même chunk, score **0,97**.
3. **generate** → « … sa température de fonctionnement est de **-20 °C à +55 °C**. »

**Tour 3 — « Quel est le retard total cumulé sur la ligne T2 ? »**
1. **route** : le LLM répond `database`. Les garde-fous vérifient que :
   - des tables existent ;
   - la question est analytique (« total », « cumulé ») ;
   - elle utilise le vocabulaire des tables (« retard », « ligne » apparaissent dans la table `incidents_septembre`).
   → route `database`.
2. **write_sql** : le LLM reçoit le schéma de la table, avec 3 lignes d'exemple, et écrit :
   `SELECT SUM(retard_min) AS total_retard FROM tabular.incidents_septembre WHERE ligne = 'T2'`
3. **execute_sql** : la requête est d'abord validée (un seul SELECT), puis exécutée en lecture seule avec un utilisateur qui n'a que le droit de lire. Résultat : `84`.
4. **answer_sql** : « Le retard total cumulé sur la ligne T2 est de **84 minutes**. » Le calcul est juste : 35 + 8 + 41.

**Tour 4 — « Quel est le salaire du directeur ? »** (l'information n'existe pas)
1. **retrieve** : le meilleur score est très bas, sous 0,15.
2. **grade** : pas pertinent → **rewrite**. Le LLM reformule en « Quelle est la rémunération du directeur ? ».
3. **retrieve** : toujours rien de pertinent.
4. **grade** : on a atteint le nombre maximal de reformulations (1), donc on passe à `generate`.
5. **generate** : le contexte n'est pas pertinent, donc **le LLM n'est même pas appelé**. Réponse fixe : « Cette information n'apparaît pas dans les passages fournis. »

## 1.6 Ce qui a changé par rapport à la V1

La V1 avait été écrite **entièrement à la main**, sans framework. La V2 garde les acquis de la V1 et remplace le code « plomberie » par les standards du marché.

| Sujet | V1 (code maison) | V2 |
|---|---|---|
| Stockage | ChromaDB (vecteurs) + SQLite (tables) + 2ᵉ Chroma (Vanna) | **Un seul PostgreSQL** : vecteurs + tables + mémoire |
| Orchestration | Fichier `rag_chain.py` d'environ 1 000 lignes, logique imbriquée | **Graphe LangGraph** : 9 nœuds explicites, testables un par un |
| Appel du LLM | Client HTTP écrit à la main pour Ollama et Azure | `init_chat_model` : OpenAI ou Ollama selon la configuration |
| SQL | Vanna + `schema_rag.py` (≈1 300 lignes) | `SQLDatabase` LangChain + boucle d'auto-correction |
| Ré-ingestion | Risque de doublons | API `index()` + RecordManager : **idempotente** |
| Mémoire | Aucune | Checkpointer Postgres par conversation |
| Observabilité | Logs fichiers | Traces **LangSmith** |
| Qualité | Scripts de test manuels | 27 tests automatiques + évaluation + **CI GitHub Actions** |

**Conservé de la V1** : les **parseurs PDF/Word**, qui transforment les tableaux en Markdown et retirent les en-têtes et pieds de page répétés, ainsi que les **règles métier du prompt** (copier les valeurs exactement, ne pas mélanger deux lignes d'un tableau…). Ce sont les éléments les plus « métier » de la V1, ceux qu'un framework ne fournit pas.

## 1.7 Carte des fichiers (pour s'y retrouver)

```
code_V2/
├─ app/                       le serveur Python
│  ├─ config.py               tous les réglages (lus depuis .env)
│  ├─ llm.py                  fabrique du LLM et du modèle d'embedding
│  ├─ loaders.py              lecture des PDF/Word → Documents LangChain
│  ├─ parsers/                parseurs PDF/Word repris de la V1
│  ├─ ingestion.py            découpage + indexation + import Excel
│  ├─ vectorstore.py          connexion PostgreSQL, PGVector, registre d'indexation
│  ├─ retriever.py            recherche hybride + reranking
│  ├─ sql_backend.py          description des tables + exécution SQL sécurisée
│  ├─ prompts.py              tous les prompts
│  ├─ graph/state.py          structure de l'état partagé du graphe
│  ├─ graph/builder.py        les 9 nœuds et les transitions du graphe
│  ├─ graph/__init__.py       assemblage "production" (vrais modèles + mémoire Postgres)
│  └─ api.py                  routes FastAPI + service de l'interface
├─ frontend/                  interface React (Vite)
├─ scripts/                   ingest, ask (chat console), make_samples (corpus démo)
├─ eval/                      évaluation (run_eval.py) + jeux de questions
├─ tests/                     tests automatiques
├─ docker/init.sql            création de l'extension vector, du schéma, du rôle lecture seule
├─ docker-compose.yml         PostgreSQL (+ API en option)
├─ Dockerfile                 image de production (interface + API)
├─ .github/workflows/ci.yml   intégration continue
└─ demarrer.bat               lancement en un double-clic
```
