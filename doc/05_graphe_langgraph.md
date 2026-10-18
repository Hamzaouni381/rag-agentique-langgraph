# 5. Le graphe agentique (LangGraph)

C'est le **cerveau** du système. Il reçoit une question et décide de toutes les étapes jusqu'à la réponse.

## 5.1 Le graphe complet

```
                         START
                           │
                    ┌──────▼───────┐
                    │ contextualize│  rend la question autonome (historique)
                    └──────┬───────┘
                    ┌──────▼───────┐
                    │    route     │  LLM + garde-fous → documents / database / chat
                    └──┬────┬────┬─┘
          documents    │    │    │   chat
        ┌──────────────┘    │    └──────────────┐
 ┌──────▼──────┐   database │              ┌────▼────┐
 │  retrieve   │◄──┐        │              │  chat   │
 └──────┬──────┘   │  ┌─────▼─────┐        └────┬────┘
   grade│          │  │ write_sql │◄─┐          │
  ┌─────┴──────┐   │  └─────┬─────┘  │          │
  │pertinent ? │   │  ┌─────▼──────┐ │          │
  └─┬───────┬──┘   │  │execute_sql │ │          │
 oui│    non│      │  └─────┬──────┘ │          │
    │  ┌────▼────┐ │  erreur│ & essais restants │
    │  │ rewrite ├─┘        ├─────────┘          │
    │  └─────────┘      sinon│                    │
 ┌──▼──────┐         ┌──────▼─────┐              │
 │generate │         │ answer_sql │              │
 └──┬──────┘         └──────┬─────┘              │
    └──────────────────┬────┴────────────────────┘
                      END   (réponse sauvegardée dans la mémoire du thread)
```

## 5.2 L'état partagé (`GraphState`)

L'état est un dictionnaire qui circule de nœud en nœud. Chaque nœud **lit** l'état et **renvoie seulement les champs qu'il modifie** ; LangGraph les fusionne dans l'état.

| Champ | Rempli par | Rôle |
|---|---|---|
| `messages` | entrée + nœuds de réponse | **Historique** de la conversation (questions et réponses). Reducer `add_messages` : on **ajoute**, on ne remplace pas. **Persisté** par le checkpointer. |
| `question` | entrée | La question brute, telle que tapée |
| `standalone` | contextualize | La question **autonome** (« Et sa température ? » → « Quelle est la température du PV-300 ? ») |
| `search_query` | contextualize, rewrite | Ce qu'on envoie réellement à la recherche (peut être reformulé) |
| `route` | route | `documents`, `database` ou `chat` |
| `documents` | retrieve | Les 5 passages trouvés, chacun avec son score |
| `rewrites` | rewrite | Nombre de reformulations déjà faites |
| `sql`, `sql_result`, `sql_error`, `sql_attempts` | branche SQL | Requête générée, résultat, erreur éventuelle, nombre d'essais |
| `answer` | generate, answer_sql, chat | La réponse finale |
| `sources` | generate | Liste `{source, page, relevance}` affichée sous la réponse |

**À savoir** : `contextualize` **remet à zéro** les champs de travail (`rewrites`, `sql`, `documents`, `sources`…) à chaque nouvelle question. Sans cela, l'état sauvegardé de la question précédente « fuirait » dans la suivante : on verrait par exemple le SQL d'avant s'afficher sous une réponse documentaire.

## 5.3 Les nœuds, un par un

### `contextualize` — rendre la question autonome
- **Le problème** : « Et sa température ? » n'a aucun sens pour la recherche. Il faut savoir qu'on parle du PV-300.
- **S'il n'y a pas d'historique**, la question est gardée telle quelle.
- **S'il y a un historique** :
  1. Le LLM reçoit les 6 derniers messages et la consigne « reformule en question autonome, ne réponds pas ».
  2. **Vérification** : la reformulation doit finir par « ? » et ne pas être démesurément longue. On a observé que les petits modèles **répondent** parfois au lieu de reformuler, par exemple « Je suis désolé… ».
  3. **Si elle est invalide** :
     - si la question est une **relance** (elle commence par « et/mais/alors », ou contient « sa/son/ses/celui… », et fait 8 mots au plus), on **concatène la question précédente** (« Quel est l'indice du PV-300 ? Et sa température ? ») ;
     - sinon on garde la question brute.
- **Si l'appel LLM plante** (erreur Ollama), on passe au repli, sans faire échouer la requête.

### `route` — choisir la stratégie
1. Le LLM reçoit la question et la liste des tables, puis répond un mot : `documents`, `database` ou `chat`.
2. **Les garde-fous déterministes** (fonction `parse_route` et suivantes) encadrent cette décision :

| Le LLM dit | Condition pour accepter | Sinon |
|---|---|---|
| `database` | des tables existent **ET** la question est **analytique** (combien, total, moyenne, cumul, par ligne…) **ET** elle **cite le vocabulaire des tables** (noms de tables ou colonnes, valeurs d'exemple ; comparaison sur les 5 premières lettres pour tolérer pluriels et accords) | `documents` |
| `chat` | le message brut fait **5 mots au plus** (salutation, remerciement) | `documents` |
| autre / illisible / erreur | — | `documents` |

- **Pourquoi `documents` par défaut** : c'est le choix le plus **sûr**. Au pire, la recherche ne trouve rien et le système refuse proprement. Un mauvais envoi vers SQL produit au contraire une réponse fausse mais convaincante.
- **Un exemple qui justifie le 3ᵉ garde-fou** : « **Combien** de temps sont conservés les objets trouvés ? » est « analytique » (« combien »), mais ne parle ni de retard, ni de ligne, ni d'incident. C'est donc `documents`, et la réponse (3 mois) est bien dans le règlement PDF.

### `retrieve` — chercher
Il appelle la fonction de recherche du [chapitre 4](04_recherche.md) avec `search_query` et range les 5 passages notés dans `documents`.

### `grade` — juger la pertinence (arête conditionnelle, pas un nœud)
- Si le meilleur score est **≥ 0,15**, on passe à `generate`.
- Sinon, s'il reste une reformulation possible, on passe à `rewrite`.
- Sinon, on passe à `generate`, qui refusera.

C'est une **fonction de routage** : elle ne modifie pas l'état, elle choisit seulement la suite.

### `rewrite` — reformuler
- Le LLM reformule la question autonome pour la recherche documentaire : synonymes, termes techniques, notations alternatives, suppression du verbiage.
- Le résultat remplace `search_query`, et `rewrites` augmente de 1.
- On retourne ensuite à `retrieve`. **C'est la boucle du graphe.**
- Si le LLM plante, on garde la question d'origine.

### `generate` — rédiger la réponse documentaire
- **Si aucun passage n'est pertinent**, on renvoie le message fixe « Cette information n'apparaît pas dans les passages fournis. » **sans appeler le LLM**.
- **Sinon**, le LLM reçoit :
  - un message **système** avec les règles métier (répondre uniquement avec les passages, copier les valeurs exactement, citer `[fichier, page X]`, signaler les contradictions, choisir une seule ligne de tableau…) ;
  - un message **utilisateur** avec les passages numérotés et référencés (`--- Passage 1 [procedure_PV300.docx, page 3] ---`) et la question.
- On renvoie la réponse, les sources (fichier, page, score) et un `AIMessage` ajouté à l'historique.

### `write_sql` — écrire la requête
Le LLM reçoit :
- le schéma des tables : un `CREATE TABLE` avec 3 lignes d'exemple, fourni par `SQLDatabase` de LangChain ;
- les règles : PostgreSQL, SELECT uniquement, préfixe de schéma `tabular.`, GROUP BY, LIMIT 100 ;
- la question ;
- **en cas de nouvel essai**, la requête précédente et le message d'erreur de PostgreSQL, avec la consigne « corrige-la ».

La sortie est nettoyée : suppression des balises ```` ```sql ````, du texte avant le SELECT, et des requêtes en trop après un `;`.

### `execute_sql` — exécuter
L'exécution est sécurisée (voir [chapitre 6](06_text_to_sql.md)).
- En cas de **succès**, le résultat va dans `sql_result`.
- En cas d'**erreur** (colonne inexistante, syntaxe…), le message va dans `sql_error`. Il n'y a pas de plantage.

### `after_sql` — décider (arête conditionnelle)
- S'il y a une erreur et qu'il reste des essais (`MAX_SQL_RETRIES = 1`, donc **2 essais au total**), on retourne à `write_sql`.
- Sinon, on passe à `answer_sql`.

### `answer_sql` — commenter le résultat
- **S'il y a toujours une erreur**, on renvoie un message fixe demandant de reformuler.
- **Sinon**, le LLM reçoit la question, le SQL et le résultat, et rédige une réponse chiffrée, avec la consigne de ne rien inventer.

### `chat` — répondre directement
Une réponse courte avec l'historique, sans aucune recherche. Ce nœud est réservé aux messages courts : « Bonjour », « merci »…

## 5.4 La robustesse : les appels « auxiliaires » ne plantent jamais

Il y a deux familles d'appels LLM :
- **les auxiliaires** (`contextualize`, `route`, `rewrite`) : ils passent par une fonction `_aux` qui intercepte toute erreur et renvoie une **valeur par défaut sûre** (question d'origine, route `documents`) ;
- **les appels de réponse** (`generate`, `answer_sql`, `chat`) : une erreur remonte. L'API la transforme en événement `error`, affiché proprement dans l'interface.

**Pourquoi** : on a observé en test qu'Ollama interrompt parfois une génération (« token repeat limit reached »). Avant ce mécanisme, toute la question échouait pour une simple reformulation ratée.

## 5.5 La mémoire de conversation

**Le mécanisme.** On compile le graphe avec un **checkpointer** `PostgresSaver`. À chaque appel, on fournit un `thread_id`, l'identifiant de la conversation :
1. LangGraph **recharge** le dernier état sauvegardé de ce thread, y compris `messages`, l'historique complet.
2. Il y **ajoute** la nouvelle question (reducer `add_messages`).
3. Il exécute le graphe et **sauvegarde** l'état après chaque étape.
4. La réponse est ajoutée à `messages` par le nœud final.

Au tour suivant avec le même `thread_id`, `contextualize` voit donc l'historique.

**Dans l'interface**, chaque conversation de la barre latérale a son propre `thread_id` (un UUID). « Nouvelle conversation » crée un nouveau thread, donc une mémoire vierge.

**Les limites choisies** : seuls les **6 derniers messages** sont transmis au LLM (`HISTORY_TURNS`), pour ne pas saturer sa fenêtre de contexte. L'historique complet reste stocké.

## 5.6 L'injection de dépendances (ce qui rend le graphe testable)

La fonction `build_graph(llm, retrieve_fn, sql_backend, checkpointer, …)` **reçoit** ses composants au lieu de les créer elle-même.
- **En production**, `app/graph/__init__.py` lui passe le vrai LLM, la vraie recherche, le vrai PostgreSQL et le `PostgresSaver`.
- **En test**, on lui passe :
  - un **faux LLM** (`FakeListChatModel`), qui renvoie des réponses écrites à l'avance ;
  - une fausse recherche (une simple fonction) ;
  - un faux SQL ;
  - une mémoire en RAM (`InMemorySaver`).

On teste ainsi **chaque chemin du graphe** en 2 secondes, sans modèle ni base, de façon déterministe ([chapitre 9](09_qualite.md)).

**Si PostgreSQL ou le rôle `rag_reader` est indisponible** au démarrage, la branche SQL est désactivée (`sql_backend = None`) : le système continue de fonctionner en mode documents et chat.

## 5.7 Pourquoi un graphe explicite plutôt qu'un agent « ReAct » libre

| | Agent ReAct (le LLM appelle des outils en boucle) | Graphe explicite (notre choix) |
|---|---|---|
| Qui décide des étapes | Le LLM, à chaque tour | Le graphe ; le LLM ne décide qu'aux points prévus |
| Coût / latence | Imprévisible (nombre de tours variable) | Borné (max 1 reformulation, max 1 correction SQL) |
| Petits modèles locaux | Fonctionne mal (appels d'outils mal formés) | Fonctionne (décisions simples, encadrées) |
| Tests | Difficiles | Chaque chemin testable |
| Souplesse | Maximale | Suffisante pour un cas d'usage défini |

**Phrase pour l'entretien** : « J'ai choisi un graphe à **autonomie contrôlée** : le LLM décide de la route et des reformulations, mais dans un cadre borné et testable. Pour un usage métier, on préfère la prévisibilité à l'autonomie maximale. »

## 5.8 À retenir pour l'expliquer

- **9 nœuds, 3 décisions, 2 boucles** : reformulation-recherche et correction SQL.
- **L'état** est partagé ; `messages` est cumulatif et persisté par le **checkpointer Postgres**, indexé par `thread_id`.
- **Le routage** est fait par le LLM mais **encadré par des garde-fous déterministes**, avec `documents` comme choix par défaut sûr.
- **Les appels auxiliaires dégradent proprement** au lieu de planter.
- **Le graphe est injectable**, donc entièrement testable sans LLM.
