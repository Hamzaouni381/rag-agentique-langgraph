# 9. Qualité : tests, évaluation, observabilité, CI

Un système LLM se vérifie à **trois niveaux** différents, qui ne répondent pas à la même question :

| Niveau | Question | Outil | Besoin d'un LLM ? |
|---|---|---|---|
| **Tests unitaires** | « Le code fait-il ce qu'il doit faire ? » (logique, routage, sécurité) | pytest | Non : faux LLM |
| **Évaluation** | « Les réponses sont-elles bonnes ? » | `eval/run_eval.py` + LangSmith | Oui : le vrai système |
| **Observabilité** | « Pourquoi cette réponse précise est-elle mauvaise ? » | Traces LangSmith | Oui, en production |

## 9.1 Les tests unitaires (27 tests, environ 2 secondes)

`pytest -q` lance tout. Aucun test n'a besoin d'Ollama, de PostgreSQL ou d'un modèle. Ils tournent donc partout, y compris en CI.

### `tests/test_graph.py` — les chemins du graphe
Le graphe est construit avec des **faux composants** (injection de dépendances) :
- `FakeListChatModel` : un LLM qui renvoie, dans l'ordre, des réponses écrites à l'avance ;
- une fausse recherche : une fonction qui renvoie des passages choisis, avec un score choisi ;
- `FakeSql` : un faux backend SQL, qui peut échouer au premier appel ;
- `InMemorySaver` : la mémoire en RAM.

| Test | Scénario vérifié |
|---|---|
| `test_rag_route_with_relevant_context` | Route `documents`, passage pertinent, réponse générée, sources renvoyées, aucune reformulation |
| `test_irrelevant_context_triggers_rewrite_then_refuses_without_llm` | Passage non pertinent → reformulation → nouvelle recherche avec la question reformulée → refus **sans appel LLM** (le faux LLM n'a que 2 réponses ; un 3ᵉ appel ferait échouer le test) |
| `test_sql_route_self_corrects_after_error` | 1ʳᵉ requête en erreur → correction → 2ᵉ requête exécutée → nettoyage du Markdown → réponse chiffrée |
| `test_database_route_falls_back_to_documents_without_tables` | Le LLM dit `database` mais aucune table n'existe → `documents` |
| `test_memory_contextualizes_follow_up_question` | 2 tours dans le même thread : la question de relance est reformulée grâce à l'historique, 4 messages en mémoire |
| `test_auxiliary_llm_failure_degrades_instead_of_crashing` | La reformulation plante (erreur simulée) → pas de plantage, la question précédente est concaténée |

### `tests/test_components.py` — les briques
- **Sécurité SQL** :
  - `SELECT` et `WITH` acceptés ;
  - `DROP`, `UPDATE`, instructions multiples, `pg_sleep` et requête vide refusés ;
  - un nom de colonne contenant un mot interdit (`date_update`) reste accepté.
- **Nettoyage SQL** : retrait du Markdown et du texte parasite, une seule requête conservée.
- **Garde-fous de routage** : refus de SQL sans intention analytique, `chat` réservé aux messages courts, vocabulaire du schéma (« combien de temps… objets trouvés » ≠ table des incidents), détection des relances (« faut-il » n'est pas une relance).
- **Mise en forme du contexte** : la page n'est citée que si elle est connue.
- **Normalisation des noms de colonnes** : « N° Série / Instrument » → `n_serie_instrument`.
- **Ingestion Word de bout en bout** : un .docx avec titre, texte et tableau est créé, chargé, découpé ; on vérifie les métadonnées, la présence du tableau et le préfixe de fichier.

**Principe à retenir** : on ne teste pas « est-ce que le LLM répond bien » (c'est le rôle de l'évaluation), mais « est-ce que **mon code** réagit correctement à **chaque réponse possible** du LLM », y compris les mauvaises et les pannes.

## 9.2 L'évaluation (`eval/run_eval.py`)

```powershell
python -m eval.run_eval --file eval/questions_demo.txt          # évaluation standard
python -m eval.run_eval --file eval/questions_demo.txt --judge  # + LLM-juge de fidélité
```

1. **Chaque question** du fichier passe dans le **vrai** graphe, dans une conversation neuve.
2. **Les métriques calculées** :

| Métrique | Définition | Ce qu'elle détecte |
|---|---|---|
| `answered` | La réponse n'est pas le refus standard | Taux de réponse (trop bas = recherche ou seuil trop strict) |
| `cited` | Des sources existent **et** la réponse contient une citation `[…]` | Respect de la consigne de citation |
| `latency_s` | Durée en secondes | Performance |
| `groundedness` (option `--judge`) | Un LLM-juge vérifie que **chaque affirmation** de la réponse est appuyée par les passages | Hallucinations |

3. **Où vont les résultats** :
   - **sans clé LangSmith**, dans un CSV de `eval/results/` et un résumé à l'écran ;
   - **avec clé**, les questions deviennent un **dataset LangSmith** et chaque lancement une **expérience**, qu'on compare dans l'interface web (par exemple qwen 1.5b contre GPT-4o-mini, ou `TOP_N` 5 contre 8).

**Résultat réel** (6 questions, `qwen2.5:1.5b`) : `answered = 0,83` (5/6 ; la 6ᵉ est la question piège du salaire, refusée à juste titre), latence moyenne 11,9 s, **`cited = 0,00`**.

**Interpréter `cited = 0`** : le modèle 1.5b **ignore la consigne de citation**. Les sources sont pourtant renvoyées par l'API et affichées dans l'interface. C'est exactement le rôle d'une évaluation : **révéler un écart mesurable** entre le comportement voulu et le comportement réel. On peut le corriger de deux façons : un modèle plus grand, ou ajouter les citations par le code.

**À savoir** : le LLM-juge doit être **plus fort** que le modèle évalué. Avec `qwen 1.5b`, il juge mal ; avec `LLM_PROVIDER=openai`, il devient fiable. Une évaluation complète utiliserait aussi des **réponses de référence**, pour mesurer l'exactitude et le rappel du contexte (métriques RAGAS : `faithfulness`, `context_recall`…).

## 9.3 L'observabilité (traces LangSmith)

Avec `LANGSMITH_TRACING=true` et une clé, **chaque question** produit une trace qui contient :
- l'arbre des nœuds exécutés (contextualize → route → retrieve → …) ;
- pour chaque appel LLM : le prompt **exact** envoyé, la réponse, la durée et le nombre de tokens ;
- les passages renvoyés par la recherche ;
- les erreurs, avec leur emplacement.

**La méthode de diagnostic d'une mauvaise réponse** :
1. **La route est-elle bonne ?** Sinon, il faut ajuster le prompt du routeur ou les garde-fous.
2. **Les passages contiennent-ils la réponse ?** Sinon, le problème est **en amont** : parsing, chunking, recherche.
3. **Les passages sont bons, mais la réponse fausse ?** Le problème est dans la **génération** : modèle trop faible ou prompt à revoir.

## 9.4 La CI (GitHub Actions)

Le fichier `.github/workflows/ci.yml` se déclenche à chaque push sur `main` et à chaque pull request.

```
job "tests"                              job "docker" (si "tests" passe)
 1. récupère le code                      1. récupère le code
 2. installe Python 3.12 (avec cache)     2. construit l'image Docker complète
 3. installe torch CPU + dépendances         (interface React + API)
 4. ruff : vérification du code
 5. pytest : les 27 tests
```

**Ce que ça garantit** : aucun commit qui casse un test, le style du code ou la construction de l'image ne passe inaperçu. La suite logique (CD) serait de **publier l'image** dans un registre, puis de la **déployer** (Azure Container Apps, par exemple), une fois les tests passés.

**À savoir** : la CI **n'a pas encore tourné**, car le projet n'est pas encore poussé sur GitHub. Le fichier est prêt.
