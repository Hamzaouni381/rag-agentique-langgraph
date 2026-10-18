# 10. Décisions, limites, évolutions, préparation d'entretien

## 10.1 Les décisions d'architecture (et comment les défendre)

| Décision | Alternative écartée | Justification |
|---|---|---|
| **Un seul PostgreSQL** (pgvector + tables + mémoire) | ChromaDB/Pinecone + SQLite + Redis | Une seule base à sauvegarder, sécuriser et déployer. Transactions et rôles SQL (indispensables pour la sécurité du Text-to-SQL). Service managé disponible chez tous les clouds. Limite : moins de fonctions de recherche avancées qu'une base vectorielle dédiée à très grande échelle. |
| **Graphe LangGraph explicite** | Agent ReAct libre (`create_agent` avec des outils) | Comportement borné et prévisible, coût maîtrisé, fonctionne avec un petit modèle local, chaque chemin est testable. |
| **Recherche hybride + RRF** | Vecteurs seuls | Les références techniques exactes (codes, numéros) sont mal servies par les embeddings, très bien par BM25. |
| **Score du reranker comme juge de pertinence** | LLM-juge par document (le *Corrective RAG* des tutoriels) | Un modèle déjà présent, une passe au lieu de 5 appels LLM. Bien plus rapide sur CPU. |
| **Refus sans appel LLM** si rien n'est pertinent | Laisser le LLM juger | Supprime la cause principale d'hallucination (LLM face à un contexte hors sujet). |
| **Garde-fous déterministes sur le routage** | Faire confiance au LLM | Mesuré : le petit modèle route mal. Défaut sûr = `documents`. |
| **Tableaux → SQL, pas → vecteurs** | Vectoriser les lignes Excel | Les questions sur des tableaux sont des calculs (sommes, comptes) ; seule une base les fait exactement. |
| **3 barrières de sécurité SQL** | Filtre par expressions régulières seul | Défense en profondeur ; le rôle lecture seule est infranchissable depuis une requête. |
| **Réutiliser les parseurs V1** | Loaders génériques LangChain | La qualité d'un RAG se joue au parsing (tableaux en Markdown, en-têtes retirés) ; c'était la vraie valeur métier de la V1. |
| **Dépendances injectées dans le graphe** | Composants créés à l'intérieur | Tests rapides et déterministes, changement de fournisseur trivial. |
| **`qwen2.5:1.5b` par défaut** | `qwen2.5:0.5b` (V1) | Mesuré : 4/8 contre 8/9 bonnes réponses. Le 0.5b ne sait pas suivre les consignes d'un agent. |
| **SSE** pour le streaming | WebSocket | Flux unidirectionnel suffisant, HTTP standard, c'est le format des API LLM. |

## 10.2 Résultats mesurés

Conditions : PC portable, CPU uniquement (pas de GPU NVIDIA), corpus de démonstration de 3 fichiers.

| Mesure | Valeur |
|---|---|
| Bonnes réponses, `qwen2.5:0.5b` | 4/8 (routage, reformulation et SQL défaillants) |
| Bonnes réponses, `qwen2.5:1.5b` | **8/9**. Seul échec : un `GROUP BY` mal écrit |
| Score du reranker sur le bon passage | 0,93 – 0,97 (les autres passages : < 0,06) |
| Latence moyenne par question (1.5b) | ≈ 12 s ; 1ʳᵉ question ≈ 35 s (chargement des modèles) |
| Taux de citation dans le texte (`cited`) | 0 % avec le 1.5b (sources renvoyées à part par l'API) |
| Ré-ingestion sans changement | 5/5 chunks ignorés (idempotence vérifiée) |
| Tentative de `DROP TABLE` avec le rôle lecteur | Refusée par PostgreSQL |
| Tests automatiques | 27/27, ≈ 2 s |

## 10.3 Les limites (à connaître et à assumer)

1. **Les garde-fous de routage sont des heuristiques** (listes de mots, comparaison de vocabulaire). Ils compensent un petit modèle, mais peuvent se tromper sur des formulations inattendues. Avec un modèle plus capable, on les remplacerait par une **sortie structurée** (`with_structured_output`, schéma Pydantic) et on ne garderait que le défaut sûr.
2. **L'index BM25 vit en mémoire**, donc il ne se partage pas entre plusieurs instances. À l'échelle, il faut passer à la recherche plein texte PostgreSQL (`tsvector`).
3. **Pas d'index HNSW** : la recherche vectorielle est exacte, donc linéaire. C'est suffisant jusqu'à quelques dizaines de milliers de chunks.
4. **Tout est synchrone** : chaque question occupe un thread du serveur. Pour beaucoup d'utilisateurs simultanés, il faut une version asynchrone (`ainvoke`, `AsyncPostgresSaver`).
5. **Pas d'authentification par utilisateur ni de cloisonnement des documents** : tout le monde voit tout le corpus. Il faudrait des filtres par métadonnées dans la recherche, et un rôle SQL par périmètre.
6. **La mémoire n'est jamais purgée** (checkpoints).
7. **Le Text-to-SQL a un schéma minimal** (3 lignes d'exemple) et ne détecte pas une requête **valide mais fausse**.
8. **Les citations dans le texte ne sont pas garanties** avec un petit modèle.

## 10.4 Les évolutions possibles (par priorité)

1. **Passer à OpenAI ou à un modèle local plus grand**, puis relancer l'évaluation pour mesurer le gain.
2. **Un jeu d'évaluation avec réponses de référence** et des métriques RAGAS (`faithfulness`, `answer_correctness`, `context_recall`), suivi dans LangSmith.
3. **Le *human-in-the-loop*** : `interrupt()` LangGraph avant l'exécution d'un SQL sensible, pour qu'un humain valide.
4. **Version asynchrone** + recherche plein texte PostgreSQL + index HNSW, pour monter en charge.
5. **Authentification** (Entra ID, comme la V1 avec MSAL) et cloisonnement des documents par utilisateur.
6. **Le déploiement continu** : publication de l'image en CI → Azure Container Apps + Azure Database for PostgreSQL (pgvector disponible).
7. **Le routage par sortie structurée**, et des outils supplémentaires branchés comme nouvelles branches du graphe (API métier, recherche web…).

## 10.5 Préparation d'entretien

### Le pitch en 2 minutes
> « J'ai d'abord développé un RAG entièrement à la main, sans framework, pour comprendre chaque brique : parsing, chunking, embeddings, reranking, Text-to-SQL. Puis je l'ai réécrit avec les standards du marché.
>
> L'orchestration est un **graphe LangGraph** qui route chaque question entre trois stratégies :
> - un **RAG correctif** : recherche hybride pgvector + BM25, fusion RRF, reranker cross-encoder. Si le contexte n'est pas pertinent, l'agent reformule ; s'il ne trouve toujours rien, il refuse **sans appeler le LLM**, ce qui coupe la principale source d'hallucination ;
> - un **Text-to-SQL auto-corrigé** sur les tableaux Excel importés, sécurisé par trois barrières dont un rôle PostgreSQL en lecture seule ;
> - une réponse conversationnelle simple.
>
> J'ai **tout mis dans PostgreSQL** : vecteurs, tables et mémoire des conversations via le checkpointer LangGraph. L'ingestion est incrémentale grâce au RecordManager de LangChain. L'ensemble est exposé en **FastAPI avec streaming SSE**, avec une interface React qui montre les étapes de l'agent en direct. C'est tracé dans **LangSmith**, couvert par 27 tests qui tournent sans LLM grâce à l'injection de dépendances, et intégré dans une **CI GitHub Actions**.
>
> J'ai **mesuré** : le modèle 0.5b donnait 4 bonnes réponses sur 8, le 1.5b en donne 8 sur 9, et l'évaluation a révélé que le petit modèle ne cite pas ses sources dans le texte. C'est le genre d'écart que je mesure avant de livrer. »

### Questions probables et réponses

**« Pourquoi LangGraph plutôt qu'une simple chaîne LangChain ? »**
Une chaîne est linéaire. J'avais besoin de **branches** (documents, SQL ou chat), de **boucles** (reformuler puis rechercher, corriger le SQL puis réexécuter) et de **mémoire persistée**. LangGraph donne les trois, avec un état typé et un checkpointer.

**« Pourquoi pas un agent ReAct avec des outils ? »**
Pour la prévisibilité. Un agent libre fait un nombre de tours variable, donc un coût et une latence variables, et les petits modèles gèrent mal les appels d'outils. Mon graphe donne au LLM des décisions **ponctuelles et bornées**. Pour un usage métier, c'est le bon compromis.

**« Comment limites-tu les hallucinations ? »**
Il y a quatre niveaux :
1. un prompt strict : uniquement les passages, valeurs copiées, citations ;
2. un reranker dont le score sert de seuil de pertinence ;
3. une reformulation unique si le contexte est mauvais ;
4. un **refus sans appel LLM** si rien n'est pertinent.

Côté SQL, le calcul est fait par la base, pas par le LLM.

**« Pourquoi une recherche hybride ? »**
Les embeddings captent le sens mais ratent les références exactes (codes produit, numéros de série). BM25 fait l'inverse. Je fusionne par **RRF**, qui ne combine que des rangs : les scores des deux méthodes ne sont pas sur la même échelle.

**« À quoi sert le reranker si on a déjà une recherche vectorielle ? »**
L'embedding encode question et passage séparément : c'est rapide, mais approximatif. Le cross-encoder les lit **ensemble** : c'est précis, mais lent. On fait donc un premier tri large et rapide, puis un reclassement précis sur une quarantaine de candidats. En bonus, son score me sert de juge de pertinence.

**« Comment sécurises-tu le Text-to-SQL ? »**
Trois barrières :
1. un filtre statique (un seul SELECT, mots interdits) ;
2. une transaction READ ONLY avec un délai maximal ;
3. surtout, un **rôle PostgreSQL qui n'a que SELECT** sur le schéma des données.

Même une injection de prompt réussie ne peut rien écrire. J'ai vérifié qu'un DROP est refusé par la base.

**« Comment gères-tu la mémoire de conversation ? »**
Avec le checkpointer PostgreSQL de LangGraph, indexé par `thread_id`. Le premier nœud reformule la question de relance en question autonome à partir des 6 derniers messages. S'il échoue, et seulement pour une vraie relance (« Et sa… »), il concatène la question précédente.

**« Comment évites-tu les doublons à la ré-ingestion ? »**
Avec l'API `index()` de LangChain et un `SQLRecordManager` : chaque chunk a une empreinte SHA-256. Les chunks connus sont ignorés, ceux d'un fichier modifié sont remplacés, et le mode `full` retire ceux des fichiers supprimés.

**« Comment testes-tu un système qui dépend d'un LLM ? »**
Je sépare deux choses :
- les **tests unitaires**, avec un faux LLM aux réponses scriptées : je vérifie que **mon code** réagit bien à chaque réponse possible, y compris les mauvaises et les pannes. C'est déterministe, ça prend 2 secondes et ça tourne en CI ;
- l'**évaluation**, sur le vrai système avec un dataset : je mesure la **qualité**, et LangSmith compare les expériences entre elles.

**« Comment passerais-tu en production ? »**
- Le code est déjà conteneurisé (Dockerfile multi-étapes), et la CI fait lint, tests et build.
- J'ajouterais le déploiement continu vers Azure Container Apps et une base PostgreSQL managée avec pgvector.
- Il faudrait aussi : authentification, cloisonnement des documents, version asynchrone, index HNSW et recherche plein texte en base, purge de la mémoire, alertes sur les traces LangSmith.

**« Quelle est la plus grosse limite de ton système ? »**
Le routage repose en partie sur des **heuristiques**, pour compenser un petit modèle local. Avec un modèle plus capable, je passerais à une sortie structurée validée par Pydantic. L'autre limite : le Text-to-SQL ne détecte pas une requête valide mais sémantiquement fausse. Il faudrait une validation, par un humain ou par un second LLM.

**« Qu'as-tu appris en passant de ta V1 maison à LangChain ? »**
- La V1 m'a fait comprendre chaque mécanisme en profondeur.
- La V2 m'a appris à **ne pas réinventer** la plomberie (vector store, indexation, mémoire, streaming), pour concentrer l'effort sur ce qui a de la valeur métier : le parsing, les règles du prompt, la sécurité et l'évaluation.
- J'ai aussi appris à **mesurer avant de conclure** : c'est un test chiffré qui m'a fait abandonner le modèle 0.5b.

### Les chiffres à connaître par cœur
- **9** nœuds, **3** décisions, **2** boucles bornées (1 reformulation, 1 correction SQL).
- Chunks de **1 500** caractères avec **300** de chevauchement ; **20** candidats par recherche ; **5** passages au LLM ; seuil de pertinence de **0,15**.
- Embeddings de **384** dimensions (e5-small, préfixes `query:` / `passage:`) ; fusion RRF pondérée **0,6 / 0,4**.
- **3** barrières SQL ; délai maximal de **10 s** ; **100** lignes maximum.
- **8/9** avec qwen 1.5b contre **4/8** avec le 0.5b ; **27** tests.
