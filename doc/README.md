# Documentation — Assistant RAG agentique V2

Cette documentation explique **tout le système** : ce qu'il fait, chaque outil utilisé, comment chaque brique est construite, comment l'exploiter, et pourquoi chaque choix a été fait.
Objectif : pouvoir **expliquer le fonctionnement complet de mémoire**, sans ouvrir le code, en entretien comme devant une équipe.

## Parcours de lecture

| # | Chapitre | Ce que tu sauras faire après |
|---|---|---|
| 1 | [Vue d'ensemble](01_vue_ensemble.md) | Expliquer le système en 2 minutes, dessiner l'architecture, raconter le trajet d'une question |
| 2 | [Les outils](02_outils.md) | Définir chaque outil (LangChain, LangGraph, pgvector, LangSmith…), dire pourquoi on l'a choisi et ce qu'il faut savoir |
| 3 | [Ingestion](03_ingestion.md) | Expliquer comment un PDF devient des vecteurs, et pourquoi on ne crée pas de doublons |
| 4 | [Recherche](04_recherche.md) | Expliquer recherche hybride, fusion RRF, reranking et seuil de pertinence |
| 5 | [Le graphe agentique](05_graphe_langgraph.md) | Décrire chaque nœud, chaque décision, la mémoire et les garde-fous |
| 6 | [Text-to-SQL](06_text_to_sql.md) | Expliquer la génération SQL, l'auto-correction et les 3 barrières de sécurité |
| 7 | [API et interface](07_api_et_interface.md) | Décrire les routes, le streaming SSE et l'interface React |
| 8 | [Exploitation](08_exploitation.md) | Installer, configurer, ajouter des documents, changer de modèle, dépanner |
| 9 | [Qualité : tests, évaluation, CI](09_qualite.md) | Expliquer la stratégie de test, l'évaluation LangSmith et la CI |
| 10 | [Décisions, limites, entretien](10_decisions_et_entretien.md) | Défendre chaque choix, connaître les limites mesurées, répondre aux questions d'entretien |

**Conseil** : lis les chapitres 1, 2 et 5 en premier. Ils donnent 80 % de la compréhension. Le chapitre 10 sert à préparer l'entretien.

## Lexique express

| Terme | Définition en une phrase |
|---|---|
| **RAG** | *Retrieval-Augmented Generation* : on cherche d'abord les passages utiles dans les documents, puis on demande au LLM de répondre **uniquement** avec ces passages. |
| **LLM** | Modèle de langage qui génère du texte (ici Qwen 2.5 via Ollama, ou GPT via OpenAI). |
| **Embedding** | Vecteur de nombres (ici 384) qui représente le sens d'un texte ; deux textes proches en sens ont des vecteurs proches. |
| **Chunk** | Morceau de document (≈1 500 caractères) indexé séparément. |
| **Vector store** | Base qui stocke les embeddings et retrouve les plus proches d'une question (ici PostgreSQL + pgvector). |
| **BM25** | Recherche par mots-clés classique (comme un moteur de recherche), très bonne sur les références exactes. |
| **Reranker / cross-encoder** | Modèle qui relit chaque paire (question, passage) et donne un score de pertinence précis. |
| **Agent / orchestration agentique** | Système où un LLM **décide** des étapes (quelle source interroger, reformuler ou non), au lieu d'un pipeline figé. |
| **Graphe LangGraph** | Description de l'agent sous forme de nœuds (étapes) et d'arêtes (transitions, parfois conditionnelles). |
| **State (état)** | Dictionnaire partagé que chaque nœud lit et enrichit (question, documents, réponse…). |
| **Checkpointer** | Composant LangGraph qui sauvegarde l'état après chaque étape : c'est la mémoire de conversation. |
| **thread_id** | Identifiant d'une conversation ; même thread_id = même mémoire. |
| **Text-to-SQL** | Le LLM traduit une question en requête SQL, exécutée sur les tables Excel importées. |
| **SSE** | *Server-Sent Events* : le serveur envoie des événements au navigateur au fil de l'eau (streaming). |
| **Trace** | Enregistrement détaillé d'une exécution (chaque appel LLM, ses entrées/sorties, sa durée) dans LangSmith. |
