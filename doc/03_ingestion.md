# 3. L'ingestion : des fichiers aux données interrogeables

L'ingestion transforme les fichiers déposés dans `data/documents/` en données que le système sait interroger. On la déclenche de trois façons :
- avec l'interface (glisser-déposer) ;
- avec l'API (`POST /api/upload`, `POST /api/ingest`) ;
- en ligne de commande (`python -m scripts.ingest`).

## 3.1 Vue d'ensemble

```
data/documents/
  ├─ *.pdf, *.docx, *.doc, *.txt, *.md ──► BRANCHE DOCUMENTS
  │      1. Chargement structuré     (StructuredDocumentLoader → parseurs V1)
  │      2. Découpage en chunks      (RecursiveCharacterTextSplitter, 1500 car., chevauchement 300)
  │      3. Préfixe du nom de fichier en tête de chaque chunk
  │      4. Indexation synchronisée  (index() + RecordManager → embeddings → pgvector)
  │
  └─ *.xlsx, *.xls, *.csv ──────────────► BRANCHE TABLEAUX
         1. Lecture pandas (toutes les feuilles)
         2. Nettoyage (lignes/colonnes vides, noms de colonnes normalisés)
         3. Écriture d'une table par feuille dans le schéma PostgreSQL "tabular"

Puis : invalidation de l'index BM25 (il sera reconstruit à la prochaine question)
```

Résultat renvoyé (exemple réel) :
```json
{"files": 3, "chunks": 5, "tables": ["incidents_septembre"],
 "index": {"num_added": 5, "num_updated": 0, "num_skipped": 0, "num_deleted": 0}, "errors": []}
```
Un fichier en erreur n'arrête pas l'ingestion : il est listé dans `errors` et les autres sont traités normalement.

## 3.2 Étape 1 — Le chargement structuré (`app/loaders.py`)

**Le problème.** Extraire le texte brut d'un PDF détruit sa structure :
- un tableau devient une suite de mots sans colonnes ;
- l'en-tête « Manuel PV-300 — Confidentiel » est répété sur chaque page et pollue la recherche ;
- les titres ne sont plus distingués du texte.

**La solution : réutiliser les parseurs de la V1**, encapsulés dans un *Loader* LangChain (une classe qui hérite de `BaseLoader` et implémente `lazy_load()`).

| Format | Parseur | Ce qu'il fait |
|---|---|---|
| **PDF** | `parse_pdf` (PyMuPDF + pdfplumber) | Classe les blocs (titre / texte / clé-valeur) selon la taille de police. Détecte les tableaux, **même sans bordures**, et les convertit en **Markdown** (`| col | col |`). Supprime les blocs répétés de page en page. Produit **une page = un Document**. |
| **Word .docx** | `parse_word` (python-docx) | Parcourt paragraphes et tableaux **dans l'ordre**. Convertit les tableaux en Markdown. Découpe en **pages logiques** sur les titres de niveau 1 et 2, car un .docx n'a pas de pagination fiable. |
| **Word .doc** (ancien format) | `parse_doc` | Passe par Microsoft Word si installé (conversion en .docx), sinon par une extraction binaire de secours (texte brut). |
| **.txt / .md** | lecture directe | Un Document par fichier. |

Chaque page devient un **`Document` LangChain**, c'est-à-dire un objet avec deux champs :
- `page_content` : le texte en Markdown ;
- `metadata` : `{"source": "procedure_PV300.docx", "page": 3, "doc_type": "docx"}`.

**Le repli.** Si le parseur maison plante sur un fichier atypique, le loader bascule sur un loader standard LangChain (`PyMuPDFLoader` pour un PDF, `Docx2txtLoader` pour un Word). On perd la structure, mais le fichier est quand même indexé.

**Pourquoi c'est important.** La qualité d'un RAG se joue **d'abord à l'ingestion**. Un tableau mal extrait ne sera jamais bien retrouvé, quel que soit le LLM. La règle du prompt « choisis UNE ligne du tableau » ne fonctionne que parce que le tableau arrive au LLM sous forme de lignes Markdown propres.

## 3.3 Étape 2 — Le découpage en chunks (`app/ingestion.py`)

**Pourquoi découper.**
1. **La précision.** Un embedding résume le sens d'un texte en 384 nombres : plus le texte est long, plus ce résumé est flou.
2. **Le budget de contexte.** On n'envoie au LLM que les 5 meilleurs chunks, pas des documents entiers.

**L'outil.** `RecursiveCharacterTextSplitter` avec :
- `chunk_size = 1500` caractères (environ 350 tokens) ;
- `chunk_overlap = 300` : les 300 derniers caractères d'un chunk sont répétés au début du suivant, pour ne pas couper une idée en deux ;
- **des séparateurs ordonnés** : `"\n## "`, `"\n### "`, `"\n\n"`, `"\n"`, `". "`, `" "`, `""`.

**L'algorithme « récursif ».**
1. Le découpeur essaie d'abord de couper **entre deux sections Markdown** (`## `).
2. Si un morceau reste trop long, il coupe entre paragraphes, puis entre lignes, puis entre phrases…
3. Il ne coupe au milieu d'un mot qu'en dernier recours.

Une ligne de tableau (`| … |`) tient sur une ligne : elle n'est coupée que si une seule ligne dépasse 1 500 caractères.

**`add_start_index=True`** ajoute dans les métadonnées la position du chunk dans sa page. Cela sert à dédoublonner proprement pendant la recherche.

## 3.4 Étape 3 — Le préfixe de fichier

Chaque chunk commence par le nom de son fichier : `[procedure_PV300.docx]\n<texte>`.

**Pourquoi.** Imaginons deux documents très proches, comme deux rapports de vérification ou deux versions d'un manuel. Sans ce préfixe, leurs chunks auraient des embeddings quasi identiques. Le nom du fichier, souvent porteur de sens (référence, produit, date), aide à la fois l'embedding et BM25 à **désambiguïser**.

## 3.5 Étape 4 — L'indexation synchronisée

L'appel central :
```
index(chunks, record_manager, vectorstore, cleanup="incremental", source_id_key="source", key_encoder="sha256")
```

Pour chaque chunk :
1. **Empreinte** : hash SHA-256 du texte et des métadonnées.
2. **Consultation du registre** (`upsertion_record`) : cette empreinte est-elle connue ?
   - **oui** → le chunk est ignoré (`num_skipped`) : pas de nouvel embedding, pas de doublon ;
   - **non** → on calcule l'embedding (préfixe `"passage: "` + modèle e5), on l'écrit dans `langchain_pg_embedding` et on l'enregistre dans le registre (`num_added`).
3. **Nettoyage** : un chunk du registre rattaché à un fichier traité, mais qui n'a pas été revu cette fois, est supprimé (`num_deleted`). C'est le cas quand le fichier a été modifié.

| Situation | Mode à utiliser | Effet |
|---|---|---|
| J'ajoute des fichiers | `incremental` (défaut) | Seuls les nouveaux chunks sont calculés |
| J'ai modifié un fichier | `incremental` | Les anciens chunks de ce fichier sont remplacés |
| J'ai supprimé un fichier | `full` (automatique via le bouton « supprimer ») | Ses chunks disparaissent de l'index |
| Je relance tout sans rien changer | `incremental` | `num_skipped = tout`, rien n'est recalculé (rapide) |

**Ce qui est stocké pour un chunk** (une ligne de `langchain_pg_embedding`) :

| Colonne | Contenu |
|---|---|
| `id` | identifiant unique |
| `collection_id` | lien vers la collection `documents` |
| `embedding` | vecteur de 384 nombres |
| `document` | le texte du chunk (avec son préfixe `[fichier]`) |
| `cmetadata` | `{"source": "...", "page": 3, "doc_type": "docx", "start_index": 0}` |

## 3.6 La branche tableaux (Excel / CSV → PostgreSQL)

**Le principe.** On ne vectorise **pas** les tableaux de données. Une question comme « retard total sur la T2 » demande un **calcul exact** sur toutes les lignes, ce qu'une recherche de passages ne sait pas faire. On importe donc les tableaux comme de vraies tables SQL.

**Les étapes** (`ingest_tabular`) :
1. **Lecture avec pandas** : `read_excel(sheet_name=None)` lit **toutes les feuilles** ; pour un CSV, le séparateur est détecté automatiquement.
2. **Nettoyage** : suppression des lignes et colonnes entièrement vides.
3. **Normalisation des noms**, en minuscules, sans accents, avec des `_` : « Retard (min) » devient `retard_min`, « Type d'incident » devient `type_d_incident`. Le LLM écrira ainsi du SQL sans avoir à gérer les guillemets ni les accents.
4. **Nom de table** : le nom du fichier, plus celui de la feuille s'il y en a plusieurs (`incidents_septembre`, ou `rapport_2026_feuil2`).
5. **Écriture** : `to_sql(schema="tabular", if_exists="replace")`. Ré-importer un fichier **remplace** sa table.

Les types sont déduits par pandas : une date devient `TIMESTAMP`, un entier `BIGINT`, un texte `TEXT`.

**Les droits.** Les tables sont créées par le rôle `rag`. Une règle posée par `init.sql` (`ALTER DEFAULT PRIVILEGES`) donne **automatiquement** le droit de lecture au rôle `rag_reader` sur toute nouvelle table du schéma `tabular`.

## 3.7 Après l'ingestion

L'index BM25 en mémoire est **invalidé** : la prochaine question le reconstruira à partir des chunks à jour. Pas besoin de redémarrer le serveur après une ingestion.

## 3.8 À retenir pour l'expliquer

- « L'ingestion est **idempotente** : on peut la relancer autant de fois qu'on veut, elle ne recalcule que ce qui a changé, grâce au RecordManager de LangChain. »
- « Je **sépare** les documents, qui vont dans le RAG vectoriel, et les données tabulaires, qui vont dans des tables SQL. La recherche sémantique ne sait pas faire de calcul. »
- « La qualité vient du **parsing structuré** : les tableaux arrivent en Markdown, et les en-têtes répétés sont supprimés. »
