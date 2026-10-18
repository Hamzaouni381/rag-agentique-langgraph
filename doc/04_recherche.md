# 4. La recherche : retrouver les bons passages

Ce chapitre décrit ce qui se passe dans le nœud `retrieve` du graphe. La fonction `retrieve(question)` de `app/retriever.py` reçoit une question et renvoie **les 5 passages les plus pertinents, chacun avec un score**.

## 4.1 Le pipeline en 4 étages

```
question : "Quel est l'indice de protection du portique PV-300 ?"
   │
   ├──► ÉTAGE 1a  Recherche VECTORIELLE (pgvector)     → 20 candidats proches en SENS
   │             "query: " + question → embedding → distance cosinus avec tous les chunks
   │
   ├──► ÉTAGE 1b  Recherche BM25 (mots-clés)          → 20 candidats qui partagent les MOTS
   │             "PV-300", "protection", "portique"...
   │
   ▼
ÉTAGE 2  FUSION RRF (EnsembleRetriever, poids 0,6 / 0,4) → une seule liste classée
   ▼
ÉTAGE 3  DÉDOUBLONNAGE (même fichier + même position + même début de texte)
   ▼
ÉTAGE 4  RERANKING cross-encoder (bge-reranker-base)    → score 0..1 pour chaque passage
         tri par score, on garde les 5 meilleurs (TOP_N)
   ▼
[ (chunk Caractéristiques, 0,93), (chunk Sécurité, 0,06), (…, 0,02), … ]
```

## 4.2 Étage 1a — La recherche vectorielle

1. La question est préfixée par `"query: "` (exigence du modèle e5), puis transformée en un vecteur de 384 nombres.
2. PostgreSQL calcule la **distance cosinus** entre ce vecteur et celui de chaque chunk, puis renvoie les `RETRIEVE_K = 20` plus proches.

**Ce qu'elle trouve bien** : les reformulations et les synonymes. « Est-ce que c'est étanche ? » retrouve « Indice de protection IP54 ».
**Ce qu'elle trouve mal** : les codes et références exactes (« PV-300 », « SN-48213-B »).

## 4.3 Étage 1b — La recherche BM25

Elle note chaque chunk selon les mots de la question :
- un **mot rare** dans le corpus (« PV-300 ») compte beaucoup ;
- un **mot fréquent** (« le », « de ») ne compte presque pas ;
- un mot répété apporte des points, avec un effet de saturation.

**Ce qu'elle trouve bien** : les références exactes, les noms propres, les codes.
**Ce qu'elle trouve mal** : les synonymes (« étanche » ne trouve pas « IP54 »).

L'index BM25 est construit **en mémoire** à la première question, à partir de tous les chunks lus dans PostgreSQL, puis réutilisé. Il est reconstruit après chaque ingestion.

## 4.4 Étage 2 — La fusion RRF

**Le problème** : les deux recherches produisent des scores **incomparables**. L'une donne une distance cosinus entre 0 et 2, l'autre un score BM25 non borné.

**La solution, *Reciprocal Rank Fusion*** : on oublie les scores et on ne garde que les **rangs**.
```
score_final(chunk) = 0,6 × 1/(60 + rang_vectoriel) + 0,4 × 1/(60 + rang_BM25)
```
Un chunk bien classé **dans les deux listes** remonte en tête. Un chunk présent dans une seule liste garde une chance. La constante 60 (valeur standard) adoucit l'écart entre le 1er et le 10e rang.

**Pourquoi 0,6 / 0,4** : le sens prime, mais les mots-clés gardent un poids réel, ce qui est essentiel pour les références techniques.

## 4.5 Étage 3 — Le dédoublonnage

Un même chunk peut sortir des deux recherches. On le reconnaît à son fichier, sa position (`start_index`) et ses 80 premiers caractères, et on ne le garde qu'une fois, ce qui évite de le noter deux fois au reranking.

## 4.6 Étage 4 — Le reranking

Le cross-encoder reçoit chaque paire **(question, passage)** et produit un score de pertinence entre 0 et 1. Contrairement à l'embedding, il **lit la question et le passage ensemble** : il « voit » que le passage répond vraiment à la question, et pas seulement qu'il parle du même sujet.

On trie par score et on garde les `TOP_N = 5` meilleurs. Le score est écrit dans les métadonnées de chaque passage (`relevance`). Il voyage ensuite jusqu'à l'interface, où il s'affiche sous forme de barre de pourcentage.

**Scores mesurés sur le corpus de démo** :

| Question | Meilleur passage | Score | 2ᵉ passage |
|---|---|---|---|
| Indice de protection du PV-300 | Caractéristiques techniques | 0,93 | 0,06 |
| Température de fonctionnement | Caractéristiques techniques | 0,97 | 0,03 |
| Avant d'ouvrir le capot | Sécurité | 0,95 | 0,00 |
| Salaire du directeur | (aucun pertinent) | < 0,15 | — |

L'écart est très net entre le bon passage et les autres, et c'est ce qui rend le seuil de pertinence fiable.

## 4.7 Le seuil de pertinence et la boucle corrective

C'est le lien entre la recherche et le graphe agentique. Le nœud de décision `grade` regarde **le meilleur score** des 5 passages :

```
meilleur score ≥ 0,15 (RELEVANCE_THRESHOLD) ?
   ├─ OUI ──► generate : le LLM rédige la réponse avec les passages
   └─ NON ──► déjà reformulé MAX_REWRITES fois (1) ?
                ├─ NON ──► rewrite : le LLM reformule (synonymes, termes techniques,
                │                     notations alternatives) ──► nouvelle recherche
                └─ OUI ──► generate : ne PAS appeler le LLM, répondre
                           "Cette information n'apparaît pas dans les passages fournis."
```

Ce schéma s'appelle le **RAG correctif** (*Corrective RAG*). Deux idées clés :
1. **On corrige avant de répondre.** Une première recherche ratée est souvent due à la formulation de l'utilisateur. La reformulation remplace son vocabulaire par celui des documents (« boîtier » → « coffret », « RS 485 » → « RS485 »).
2. **On refuse sans appeler le LLM.** Si aucun passage n'est pertinent, on n'envoie **rien** au LLM. Un LLM à qui l'on donne des passages hors sujet a tendance à « broder » une réponse plausible : c'est l'origine principale des hallucinations en RAG.

## 4.8 Les réglages (fichier `.env`)

| Variable | Défaut | Effet si on augmente | Effet si on diminue |
|---|---|---|---|
| `RETRIEVE_K` | 20 | Meilleur rappel (on rate moins), reranking plus lent | Plus rapide, risque de rater le bon passage |
| `TOP_N` | 5 | Plus de contexte pour le LLM, prompt plus long et plus lent | Contexte plus ciblé, risque d'information incomplète |
| `RELEVANCE_THRESHOLD` | 0,15 | Plus de refus, moins d'hallucinations | Moins de refus, plus de risque de hors-sujet |
| `HYBRID_SEARCH` | true | — | `false` = vecteurs seuls (moins bon sur les références) |
| `RERANK_ENABLED` | true | — | `false` = pas de reranking **et pas de seuil** (tout est jugé pertinent) |
| `MAX_REWRITES` | 1 | Plus de chances de trouver, plus lent | 0 = pas de reformulation |

## 4.9 À retenir pour l'expliquer

- « La recherche est **hybride** : sémantique avec pgvector, lexicale avec BM25. Je fusionne les deux par **RRF**, qui ne compare que des rangs. »
- « Un **cross-encoder** reclasse les candidats. Son score sert aussi de **juge de pertinence**, ce qui m'évite un appel LLM supplémentaire par document. »
- « Si rien n'est pertinent, je **reformule une fois** puis je **refuse sans appeler le LLM**. C'est mon principal rempart contre les hallucinations. »
