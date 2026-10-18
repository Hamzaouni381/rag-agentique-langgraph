"""
1. TABLEAUX : pdfplumber détecte les vrais tableaux (filtrage des cadres/frames)
2. TEXTE STRUCTURÉ : PyMuPDF dict-mode — blocs classifiés par propriétés visuelles
   (titres, paires clé-valeur, texte courant)
3. EXCLUSION : zones de tableau retirées de l'extraction texte
4. ASSEMBLAGE : fusion ordonnée verticalement → Markdown structuré

"""
import pymupdf as fitz  # PyMuPDF
import re
import os
import logging
from typing import List, Dict, Tuple

from app.parsers.table_utils import (
    detect_multiline_header as _detect_multiline_header,
    merge_header_rows as _merge_header_rows,
    fill_down_merged_cells as _fill_down_merged_cells,
    is_header_row as _is_header_row,
)

logger = logging.getLogger(__name__)

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False
    logger.warning("pdfplumber non disponible — extraction de tableaux désactivée")


#  Configuration 

# Détection de tableaux
MIN_TABLE_ROWS = 2
MIN_TABLE_COLS_WITH_DATA = 2
FRAME_TABLE_AREA_RATIO = 0.50      # un tableau couvrant >50% de la page 
TABLE_GIANT_CELL_LEN = 300         # une cellule >300 chars = texte dans un cadre, pas une donnée
TABLE_GIANT_CELL_RATIO = 0.3       # >30% de cellules géantes = faux tableau

# Classification de blocs (titres, KV)
HEADER_FONT_RATIO = 1.15
FOOTER_TOP_PERCENT = 0.95
KV_MAX_KEY_LEN = 60
KV_HEADING_MAX_CHARS = 80          # au-dessus, ce n'est plus un heading même bold
KV_HEADING_LONG_THRESHOLD = 25     # phrase >25 chars = vrai titre, pas une cellule de tableau

# Détection de tableaux sans bordures
BORDERLESS_MIN_X_SEP_RATIO = 0.15  # séparation horizontale minimale (% de la largeur)
BORDERLESS_MIN_ROWS = 3            # minimum 3 lignes pour considérer un tableau
BORDERLESS_GAP_MULT = 2.5          # gap max entre lignes = 2.5× le gap médian
BORDERLESS_ALIGN_MAX_DEV = 0.10    # déviation max des centres X (% de la largeur)
BORDERLESS_OVERLAP_RATIO = 0.30    # chevauchement Y minimal pour considérer 2 blocs sur la même ligne

# Détection d'images
IMAGE_MIN_SIZE_PX = 30             # plus petit = icône, ignoré
IMAGE_FULL_PAGE_RATIO = 0.8        # >80% de la page = fond/watermark, ignoré

# Header au-dessus des colonnes (matching positionnel)
HEADER_ABOVE_MARGIN_PX = 40        # zone de recherche au-dessus du tableau
HEADER_ABOVE_MAX_WIDTH_RATIO = 1.8 # un bloc trop large pour une colonne ne compte pas
HEADER_ABOVE_Y_TOLERANCE_PX = 5    # regroupement des blocs sur la même ligne Y
HEADER_ABOVE_TRUNCATE = 60         # tronquer les headers trop longs

# Heuristique fill-misaligned-columns
MISALIGNED_DATA_FILL_MIN = 0.3     # une colonne data orpheline doit avoir >=30% de remplissage
MISALIGNED_LABEL_FILL_MAX = 0.1    # une colonne label orpheline doit avoir <10% de remplissage

# Texte répété (header/footer de page)
REPEATED_TEXT_MIN_PAGES_RATIO = 0.4  # texte présent sur >=40% des pages = répété
REPEATED_TEXT_MIN_LEN = 5
REPEATED_TEXT_MAX_LEN = 120


#  Point d'entrée 

def parse_pdf(file_path: str) -> List[Dict]:
    """Point d'entrée principal. Retourne une liste de pages structurées"""
    doc = fitz.open(file_path)
    filename = os.path.basename(file_path)
    metadata = {"fichier": filename, "type_document": "pdf"}

    median_font_size = _compute_median_font_size(doc) #Calcule la taille de police médiane du document (clasification de blocs)
    repeated_texts = _detect_repeated_blocks(doc) #Détecte les textes répétés. Retourne {texte: premiere_page} pour garder la 1ere occurrence

    # Extraire les tableaux pdfplumber 
    all_tables_data = {}
    all_table_bboxes = {}
    if HAS_PDFPLUMBER:
        all_tables_data, all_table_bboxes = _extract_all_tables(file_path, doc)

    pages = []
    for page_num in range(len(doc)):
        fitz_page = doc[page_num]
        page_height = fitz_page.rect.height

        # Couche 1 : Blocs de texte structurés (PyMuPDF )
        raw_blocks = _extract_structured_blocks(
            fitz_page, median_font_size, page_height, repeated_texts, page_num
        )

        # Couche 2 : Exclure les zones de tableau du texte
        table_bboxes = all_table_bboxes.get(page_num, [])
        text_blocks = _exclude_table_zones(raw_blocks, table_bboxes)

        # Couche 3 : Préparer les tableaux en Markdown

        table_blocks = []
        for bbox, table_md in all_tables_data.get(page_num, []):
            if table_md:
                table_md = _clean_polluted_header(table_md, text_blocks, bbox)

                table_blocks.append({
                    "y0": bbox[1],
                    "type": "table",
                    "content": table_md,
                })

        # Couche 4 : Assembler en Markdown ordonné
        text_blocks = _merge_status_label_blocks(text_blocks)

        # Detecter les tableaux sans bordures dans les blocs texte restants
        page_width = fitz_page.rect.width
        borderless_tables, text_blocks = _detect_borderless_tables(text_blocks, page_width)

        # Deduplication : supprimer les blocs texte dont le contenu ,apparait deja dans un tableau (header ou donnees)
        # Evite les doublons quand PyMuPDF et pdfplumber extraient la meme zone
        if table_blocks or borderless_tables:
            table_texts = set()
            for tb in table_blocks + borderless_tables:
                for line in tb["content"].split("\n"):
                    line = line.strip()
                    if line.startswith("|") and line.endswith("|"):
                        cells = [c.strip() for c in line.strip("|").split("|") if c.strip()]
                        table_texts.update(cells)
                    # Garder aussi les en-tetes non-Markdown au-dessus
                    elif line and not line.startswith("---"):
                        table_texts.add(line)

            text_blocks = [
                b for b in text_blocks
                if b["content"].strip() not in table_texts
            ]

        all_elements = text_blocks + table_blocks + borderless_tables
        all_elements.sort(key=lambda e: e["y0"])

        # Detecter les images visibles sur la page et ajouter leurs references
        image_refs = _detect_page_images(fitz_page)
        if image_refs:
            for img_ref in image_refs:
                all_elements.append(img_ref)
            all_elements.sort(key=lambda e: e["y0"])

        page_md = _assemble_markdown(all_elements)

        if page_num == 0:
            page_md = f"[Document: {filename}]\n---\n{page_md}"

        if page_md.strip():
            tables_count = sum(1 for e in all_elements if e.get("type") == "table")
            pages.append({
                "text": page_md.strip(),
                "page": page_num + 1,
                "source": file_path,
                "total_pages": len(doc),
                "tables_count": tables_count,
                "document_type": "pdf",
                "metadata": metadata,
            })

    doc.close()
    return pages


#  Couche 1 : Extraction structurée PyMuPDF 

def _extract_structured_blocks(
    fitz_page, median_font_size: float, page_height: float,
    repeated_texts, page_num: int = 0
) -> List[Dict]:
    """Extrait les blocs texte avec classification visuelle."""
    raw = fitz_page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    blocks = []

    for block in raw.get("blocks", []):
        if block["type"] != 0:
            continue

        bbox = block["bbox"]
        y0, y1 = bbox[1], bbox[3]
        x0, x1 = bbox[0], bbox[2]

        # Ignorer les footers
        if y0 / page_height > FOOTER_TOP_PERCENT:
            continue

        lines_data = []
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue

            line_text = "".join(s.get("text", "") for s in spans).strip()
            if not line_text:
                continue

            if line_text in repeated_texts and page_num > repeated_texts.get(line_text, 0):
                continue

            max_font_size = max(s.get("size", 0) for s in spans)
            is_bold = any(s.get("flags", 0) & 16 for s in spans)
            line_bbox = line.get("bbox", bbox)

            # Stocker aussi les spans individuels pour l'analyse KV
            span_data = []
            for s in spans:
                span_data.append({
                    "text": s.get("text", ""),
                    "bold": bool(s.get("flags", 0) & 16),
                    "size": s.get("size", 0),
                })

            lines_data.append({
                "text": line_text,
                "font_size": max_font_size,
                "is_bold": is_bold,
                "bbox": line_bbox,
                "spans": span_data,
            })

        if not lines_data:
            continue

        block_text = "\n".join(ld["text"] for ld in lines_data)
        avg_font_size = sum(ld["font_size"] for ld in lines_data) / len(lines_data)
        any_bold = any(ld["is_bold"] for ld in lines_data)

        block_type = _classify_block(
            block_text, avg_font_size, median_font_size, any_bold, lines_data
        )

        blocks.append({
            "y0": y0,
            "x0": x0,
            "type": block_type,
            "content": block_text,
            "font_size": avg_font_size,
            "is_bold": any_bold,
            "lines": lines_data,
            "bbox": bbox,
        })

    return blocks


def _classify_block(
    text: str, avg_size: float, median_size: float,
    is_bold: bool, lines: list
) -> str:
    """Classifie un bloc par ses propriétés visuelles."""
    line_count = len(lines)
    text_len = len(text)

    # TITRE : grande police + court + souvent bold
    if (avg_size > median_size * HEADER_FONT_RATIO
            and text_len < 120
            and line_count <= 3):
        return "heading"

    if is_bold and text_len < KV_HEADING_MAX_CHARS and line_count <= 2 and ":" not in text:
        stripped = text.strip()

        is_all_caps = stripped.isupper() and any(c.isalpha() for c in stripped)
        has_numbering = bool(re.match(r'^(\d+(\.\d+)+\s|[IVX]+\.\s)', stripped))
        is_long_phrase = text_len >= KV_HEADING_LONG_THRESHOLD  # cellules de tableau rarement >25 chars

        if is_all_caps or has_numbering or is_long_phrase:
            return "heading"

    kv_count = 0
    for line_data in lines:
        if _line_has_kv_pattern(line_data):
            kv_count += 1

    if kv_count > 0 and kv_count >= line_count * 0.4:
        return "kv_group"

    return "text"


def _line_has_kv_pattern(line_data: dict) -> bool:
    """Détecte si une ligne contient un pattern clé:valeur."""
    text = line_data["text"]
    if ":" not in text:
        return False

    spans = line_data.get("spans", [])
    has_bold = any(s["bold"] for s in spans)
    has_normal = any(not s["bold"] for s in spans)

    if has_bold and has_normal:
        return True

    # Fallback : vérifier le pattern textuel "Clé : Valeur"
    parts = text.split(":")
    if len(parts) >= 2 and len(parts[0].strip()) <= KV_MAX_KEY_LEN:
        return True

    return False


#  Couche 2 : Extraction de tableaux pdfplumber 

def _extract_all_tables(file_path: str, doc) -> Tuple[Dict, Dict]:
    """
    Extrait les vrais tableaux (pas les cadres de page)
    on extrait les sous-tableaux denses qui s'y trouvent
    """
    tables_data = {}
    table_bboxes = {}

    try:
        with pdfplumber.open(file_path) as pdf:
            for pn, page in enumerate(pdf.pages):
                page_width = page.width
                page_height = page.height
                page_area = page_width * page_height

                page_tables = []
                page_bboxes = []

                try:
                    tables = page.find_tables({
                        "vertical_strategy": "lines",
                        "horizontal_strategy": "lines",
                        "snap_tolerance": 5,
                        "join_tolerance": 5,
                    })

                    for table in tables:
                        data = table.extract()
                        bbox = table.bbox

                        # Filtre 1 : Frame table (couvre trop de la page)
                        table_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                        if table_area / page_area > FRAME_TABLE_AREA_RATIO:
                            # Extraire les sous-tableaux denses depuis le frame en utilisant les objets Row pour avoir la bbox précise
                            sub_tables = _extract_subtables_from_frame(data, table.rows)
                            for sub_bbox, sub_md in sub_tables:
                                page_tables.append((sub_bbox, sub_md))
                                page_bboxes.append(sub_bbox)
                            continue

                        # Filtre 2 : Validité du contenu
                        if not _is_valid_table(data):
                            continue

                        md = _table_to_markdown(data)
                        if md:
                            page_tables.append((bbox, md))
                            page_bboxes.append(bbox)

                except Exception as e:
                    logger.debug(f"Erreur tableaux page {pn}: {e}")

                if page_tables:
                    tables_data[pn] = page_tables
                if page_bboxes:
                    table_bboxes[pn] = page_bboxes

    except Exception as e:
        logger.error(f"Erreur pdfplumber: {e}")

    return tables_data, table_bboxes


def _extract_subtables_from_frame(data: list, rows: list) -> List[Tuple]:
    """
    Extrait les sous-tableaux structurés depuis un frame table.

    """
    if not data or not rows or len(data) != len(rows):
        return []
    
    sub_tables = []
    current_rows_data = []
    current_rows_objs = []
    
    for row_data, row_obj in zip(data, rows):
        filled = sum(1 for c in row_data if c and str(c).strip())
        
        if filled >= 2:
            # Verifier si les cellules contiennent des \n (multi-lignes fusionnees)
            filled_cells = [c for c in row_data if c and str(c).strip()]
            has_multiline = any("\n" in str(c) for c in filled_cells)
            
            if has_multiline and filled <= 3:
                # Decomposer : chaque cellule a N lignes, on cree N lignes de tableau
                expanded = _expand_multiline_cells(row_data)
                if len(expanded) >= 2:
                    sub_md = _table_to_markdown(expanded)
                    if sub_md:
                        sub_tables.append((row_obj.bbox, sub_md))
                else:
                    current_rows_data.append(row_data)
                    current_rows_objs.append(row_obj)
            else:
                current_rows_data.append(row_data)
                current_rows_objs.append(row_obj)
        else:
            if len(current_rows_data) >= 2:
                sub_md = _table_to_markdown(current_rows_data)
                if sub_md:
                    b0 = min(r.bbox[0] for r in current_rows_objs)
                    b1 = min(r.bbox[1] for r in current_rows_objs)
                    b2 = max(r.bbox[2] for r in current_rows_objs)
                    b3 = max(r.bbox[3] for r in current_rows_objs)
                    sub_tables.append(((b0, b1, b2, b3), sub_md))
            current_rows_data = []
            current_rows_objs = []
    
    if len(current_rows_data) >= 2:
        sub_md = _table_to_markdown(current_rows_data)
        if sub_md:
            b0 = min(r.bbox[0] for r in current_rows_objs)
            b1 = min(r.bbox[1] for r in current_rows_objs)
            b2 = max(r.bbox[2] for r in current_rows_objs)
            b3 = max(r.bbox[3] for r in current_rows_objs)
            sub_tables.append(((b0, b1, b2, b3), sub_md))
    
    return sub_tables


def _expand_multiline_cells(row_data: list) -> list:

    n_cols = len(row_data)
    if n_cols == 0:
        return [row_data]

    cell_lines = []
    non_empty_count = 0
    line_counts_non_empty = []
    for c in row_data:
        text = "" if c is None else str(c).strip()
        is_filled = bool(text)
        if "\n" in text:
            ls = [ln.strip() for ln in text.split("\n")]
            cell_lines.append(ls)
            if is_filled:
                non_empty_count += 1
                line_counts_non_empty.append(len(ls))
        else:
            cell_lines.append([text])
            if is_filled:
                non_empty_count += 1
                line_counts_non_empty.append(1)


    if non_empty_count < 2:
        return [row_data]
    if not line_counts_non_empty:
        return [row_data]
    if len(set(line_counts_non_empty)) > 1:
        return [row_data]
    if line_counts_non_empty[0] < 2:
        return [row_data]

    max_lines = line_counts_non_empty[0]
    result = []
    for line_idx in range(max_lines):
        row = []
        for lines in cell_lines:
            row.append(lines[line_idx] if line_idx < len(lines) else "")
        if any(c for c in row):
            result.append(row)

    return result if result else [row_data]


def _expand_data(data: list) -> list:
    """
    Applique _expand_multiline_cells a chaque ligne d'une table : si une
    ligne contient des cellules multi-lignes, elle est decomposee en
    plusieurs lignes. Les autres lignes passent inchangees.
    """
    expanded = []
    for row in data:
        if row is None:
            continue
        rows = _expand_multiline_cells(list(row))
        expanded.extend(rows)
    return expanded


def _is_valid_table(data: list) -> bool:
    """Vérifie qu'un tableau est valide (pas un faux positif)."""
    if not data or len(data) < MIN_TABLE_ROWS:
        return False

    max_filled_cols = 0
    for row in data:
        filled = sum(1 for c in row if c and str(c).strip())
        max_filled_cols = max(max_filled_cols, filled)

    if max_filled_cols < MIN_TABLE_COLS_WITH_DATA:
        return False

    # Ignorer les tableaux avec des cellules géantes (texte dans un cadre)
    giant_cells = 0
    total_cells = 0
    for row in data:
        for cell in (row or []):
            if cell and str(cell).strip():
                total_cells += 1
                if len(str(cell)) > TABLE_GIANT_CELL_LEN:
                    giant_cells += 1

    if total_cells > 0 and (giant_cells / total_cells) > TABLE_GIANT_CELL_RATIO:
        return False

    return True


def _merge_misaligned_columns(cleaned: list, n_header_rows: int) -> list:

    if not cleaned or n_header_rows >= len(cleaned):
        return cleaned

    n_cols = max(len(r) for r in cleaned)
    if n_cols < 3:
        return cleaned

    # Padding uniforme
    cleaned = [list(r) + [""] * (n_cols - len(r)) for r in cleaned]

    header_filled = [
        any(cleaned[r][c].strip() for r in range(n_header_rows))
        for c in range(n_cols)
    ]
    data_rows = cleaned[n_header_rows:]
    if not data_rows:
        return cleaned
    data_fill = [
        sum(1 for r in data_rows if r[c].strip()) / len(data_rows)
        for c in range(n_cols)
    ]

    used = set()
    merge_pairs = []  # (keep_col_for_header, donor_col_for_data)

    for c in range(n_cols):
        if c in used:
            continue
        # Pattern A : (c-1)=donnee orpheline + (c)=label orphelin
        if c > 0 and (c - 1) not in used:
            if (not header_filled[c - 1] and data_fill[c - 1] >= MISALIGNED_DATA_FILL_MIN
                    and header_filled[c] and data_fill[c] < MISALIGNED_LABEL_FILL_MAX):
                merge_pairs.append((c, c - 1))
                used.add(c)
                used.add(c - 1)
                continue
        # Pattern B : (c)=label orphelin + (c+1)=donnee orpheline
        if c + 1 < n_cols and (c + 1) not in used:
            if (header_filled[c] and data_fill[c] < MISALIGNED_LABEL_FILL_MAX
                    and not header_filled[c + 1] and data_fill[c + 1] >= MISALIGNED_DATA_FILL_MIN):
                merge_pairs.append((c, c + 1))
                used.add(c)
                used.add(c + 1)
                continue

    if not merge_pairs:
        return cleaned

    drop_cols = {donor for _, donor in merge_pairs}
    keep_to_donor = {keep: donor for keep, donor in merge_pairs}

    result = []
    for ri, row in enumerate(cleaned):
        new_row = []
        for ci in range(n_cols):
            if ci in drop_cols:
                continue
            if ci in keep_to_donor:
                if ri < n_header_rows:
                    new_row.append(row[ci])  # header de la col gardee
                else:
                    new_row.append(row[keep_to_donor[ci]])  # data de la col donneuse
            else:
                new_row.append(row[ci])
        result.append(new_row)

    return result


def _table_to_markdown(data: list, text_blocks_above: list = None,
                       table_bbox: tuple = None) -> str:
    """Convertit les données d'un tableau en Markdown.

    Si la premiere ligne n'est pas un header et que text_blocks_above est fourni,
    cherche les textes positionnes au-dessus de chaque colonne pour les utiliser
    comme noms de colonnes.
    """
    if not data:
        return ""

    data = _expand_data(data)

    cleaned = []
    for row in data:
        cleaned_row = []
        for cell in row:
            if cell is None:
                cleaned_row.append("")
            else:
                c = str(cell).strip().replace("\n", " ").replace("|", "/")
                cleaned_row.append(c)
        if any(c.strip() for c in cleaned_row):
            cleaned.append(cleaned_row)

    if len(cleaned) < 1:
        return ""

    max_cols = max(len(row) for row in cleaned)
    for row in cleaned:
        while len(row) < max_cols:
            row.append("")

    # Supprimer les colonnes entièrement vides
    cols_to_keep = []
    for col_idx in range(max_cols):
        if any(row[col_idx].strip() for row in cleaned):
            cols_to_keep.append(col_idx)

    if not cols_to_keep:
        return ""

    cleaned = [[row[ci] for ci in cols_to_keep] for row in cleaned]
    max_cols = len(cols_to_keep)

    has_real_header = _is_header_row(cleaned[0], cleaned[1:])

    lines = []
    if has_real_header:
        n_header = _detect_multiline_header(cleaned)
        cleaned = _merge_misaligned_columns(cleaned, n_header)
        max_cols = len(cleaned[0]) if cleaned else max_cols
        header = _merge_header_rows(cleaned[:n_header])
        data_rows = _fill_down_merged_cells(cleaned[n_header:])
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * max_cols) + " |")
        for row in data_rows:
            lines.append("| " + " | ".join(row) + " |")
    else:
        # Pas de header reel : chercher les textes au-dessus des colonnes
        found_headers = _find_headers_above_columns(
            text_blocks_above, table_bbox, max_cols
        ) if text_blocks_above and table_bbox else None

        if found_headers and any(h.strip() for h in found_headers):
            gen_header = found_headers
        else:
            gen_header = [""] * max_cols
        data_rows = _fill_down_merged_cells(cleaned)
        lines.append("| " + " | ".join(gen_header) + " |")
        lines.append("| " + " | ".join(["---"] * max_cols) + " |")
        for row in data_rows:
            lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)


def _clean_polluted_header(table_md: str, text_blocks: list, table_bbox: tuple) -> str:

    md_lines = table_md.split("\n")
    if len(md_lines) < 3:
        return table_md

    header_line = md_lines[0]
    cells = [c.strip() for c in header_line.strip("|").split("|")]
    n_cols = len(cells)

    if n_cols < 2:
        return table_md

    needs_replacement = False

    if header_line.strip().startswith("| Col"):
        needs_replacement = True


    if all(not c.strip() for c in cells):
        needs_replacement = True

    if not needs_replacement:
        return table_md

    real_headers = _find_headers_above_columns(
        text_blocks, table_bbox, n_cols
    )

    if real_headers and any(h.strip() for h in real_headers):
        md_lines[0] = "| " + " | ".join(real_headers) + " |"
        # Toutes les donnees originales deviennent le body (y compris l'ancien header)
        old_was_generic = (
            header_line.strip().startswith("| Col")
            or all(not c.strip() for c in cells)
        )
        if not old_was_generic:
            # L'ancien header etait des donnees, le remettre dans le body
            md_lines.insert(2, header_line)
    else:
        # Pas de headers trouves au-dessus : header vide (au lieu de Col1/Col2/...)
        old_was_generic = (
            header_line.strip().startswith("| Col")
            or all(not c.strip() for c in cells)
        )
        if not old_was_generic:
            empty_header = "| " + " | ".join([""] * n_cols) + " |"
            sep = md_lines[1]
            md_lines = [empty_header, sep, header_line] + md_lines[2:]

    return "\n".join(md_lines)


def _find_headers_above_columns(text_blocks: list, table_bbox: tuple,
                                max_cols: int) -> list:

    if not text_blocks or not table_bbox:
        return None

    table_top = table_bbox[1]
    table_left = table_bbox[0]
    table_right = table_bbox[2]
    table_width = table_right - table_left

    # Collecter les blocs texte juste au-dessus du tableau
    above_blocks = []
    for b in text_blocks:
        if not b.get("lines"):
            continue
        b_bottom = max(ld["bbox"][3] for ld in b["lines"])
        b_top = b["y0"]
        # Le bloc doit etre au-dessus du tableau, dans la marge
        if b_bottom <= table_top and b_bottom >= table_top - HEADER_ABOVE_MARGIN_PX:
            x0 = b.get("x0", 0)
            x1 = b.get("bbox", (x0, 0, x0 + 100, 0))[2]
            xc = (x0 + x1) / 2
            content = b["content"].strip().replace("\n", " ")
            if content:
                above_blocks.append({"xc": xc, "x0": x0, "x1": x1, "y0": b_top, "text": content})

    if not above_blocks:
        return None

    # Filtrer les blocs trop larges pour etre un header de colonne
    col_width = table_width / max_cols if max_cols > 0 else table_width
    above_blocks = [
        b for b in above_blocks
        if (b["x1"] - b["x0"]) < col_width * HEADER_ABOVE_MAX_WIDTH_RATIO
    ]

    if not above_blocks:
        return None

    # Garder seulement les blocs sur la meme ligne Y que la majorite
    # (separe les titres des vrais headers)
    if len(above_blocks) >= 2:
        y_groups = {}
        for b in above_blocks:
            matched = False
            for y_key in y_groups:
                if abs(b["y0"] - y_key) < HEADER_ABOVE_Y_TOLERANCE_PX:
                    y_groups[y_key].append(b)
                    matched = True
                    break
            if not matched:
                y_groups[b["y0"]] = [b]

        # Garder le groupe le plus nombreux (ce sont les headers)
        best_group = max(y_groups.values(), key=len)
        if len(best_group) >= max(max_cols - 1, 1):
            above_blocks = best_group
        else:
            return None
    elif len(above_blocks) < max(max_cols - 1, 1):
        return None

    # Estimer les centres X de chaque colonne du tableau
    col_width = table_width / max_cols if max_cols > 0 else table_width
    col_centers = [table_left + col_width * (i + 0.5) for i in range(max_cols)]

    # Matcher chaque bloc texte a la colonne la plus proche
    headers = [""] * max_cols
    for block in above_blocks:
        best_col = None
        best_dist = float("inf")
        for ci, cc in enumerate(col_centers):
            dist = abs(block["xc"] - cc)
            if dist < best_dist and dist < col_width:
                best_dist = dist
                best_col = ci
        if best_col is not None:
            text = block["text"][:HEADER_ABOVE_TRUNCATE]
            if headers[best_col]:
                headers[best_col] += " " + text
            else:
                headers[best_col] = text

    return headers


#  Couche 3 : Exclusion et association 

def _exclude_table_zones(blocks: List[Dict], table_bboxes: list) -> List[Dict]:
    """Retire les blocs de texte qui chevauchent des zones de tableau."""
    if not table_bboxes:
        return blocks

    filtered = []
    for block in blocks:
        y0 = block["y0"]
        if block.get("lines"):
            y1 = max(ld["bbox"][3] for ld in block["lines"])
        else:
            y1 = y0 + 10

        block_height = y1 - y0
        in_table = False

        for (tx0, ttop, tx1, tbottom) in table_bboxes:
            overlap_top = max(y0, ttop)
            overlap_bottom = min(y1, tbottom)
            if block_height > 0 and (overlap_bottom - overlap_top) > block_height * 0.5:
                # Vérifier aussi le chevauchement horizontal
                x0 = block.get("x0", 0)
                x1 = block.get("bbox", [0, 0, 999, 0])[2]
                h_overlap_left = max(x0, tx0)
                h_overlap_right = min(x1, tx1)
                if h_overlap_right > h_overlap_left:
                    in_table = True
                    break

        if not in_table:
            filtered.append(block)

    return filtered


def _merge_status_label_blocks(text_blocks: list) -> list:

    if len(text_blocks) < 2:
        return text_blocks

    status_values = {"C", "SO", "A", "NA", "N/A", "OUI", "NON"}

    # Identifier les blocs statut (courts, contenu = un code)
    status_blocks = {}
    status_indices = set()
    for i, b in enumerate(text_blocks):
        content = b["content"].strip()
        if content.upper() in status_values and len(content) <= 3:
            y_center = b["y0"]
            if b.get("lines"):
                y_center = (b["y0"] + max(ld["bbox"][3] for ld in b["lines"])) / 2
            status_blocks[i] = {"y": y_center, "text": content}
            status_indices.add(i)

    if not status_blocks:
        return text_blocks

    # Pour chaque bloc non-statut, chercher un statut sur la meme ligne Y
    merged = []
    used_status = set()

    for i, b in enumerate(text_blocks):
        if i in status_indices:
            continue

        content = b["content"].strip()
        y_center = b["y0"]
        if b.get("lines"):
            y_center = (b["y0"] + max(ld["bbox"][3] for ld in b["lines"])) / 2

        # Chercher un statut sur la meme ligne (tolerance 5px)
        matched_status = None
        for si, sdata in status_blocks.items():
            if si in used_status:
                continue
            if abs(sdata["y"] - y_center) < 5:
                matched_status = si
                break

        if matched_status is not None:
            used_status.add(matched_status)
            status_text = status_blocks[matched_status]["text"]
            new_block = dict(b)
            new_block["content"] = f"{content} : {status_text}"
            merged.append(new_block)
        else:
            merged.append(b)

    # Ajouter les statuts non utilises (orphelins)
    for i in status_indices:
        if i not in used_status:
            merged.append(text_blocks[i])

    # Retrier par Y
    merged.sort(key=lambda b: b["y0"])
    return merged


#  Couche 3b : Tableaux sans bordures 

def _detect_borderless_tables(text_blocks: list, page_width: float) -> tuple:
    """
    Detecte les tableaux sans bordures par analyse des positions des blocs.
    Cherche des blocs alignes horizontalement (meme Y) mais a des X differents.
    """
    if len(text_blocks) < 2:
        return [], text_blocks

    min_x_sep = page_width * BORDERLESS_MIN_X_SEP_RATIO

    # Extraire les infos geometriques de chaque bloc
    infos = []
    for i, b in enumerate(text_blocks):
        y0 = b["y0"]
        y1 = max((ld["bbox"][3] for ld in b["lines"]), default=y0 + 15) if b.get("lines") else y0 + 15
        x0 = b.get("x0", 0)
        x1 = b.get("bbox", (x0, 0, x0 + 100, 0))[2]
        infos.append({"i": i, "y0": y0, "y1": y1, "x0": x0, "x1": x1, "xc": (x0 + x1) / 2, "h": y1 - y0})

    # Calculer l'interligne moyen de la page (distance Y entre blocs consecutifs)
    sorted_by_y = sorted(infos, key=lambda r: r["y0"])
    gaps = []
    for k in range(1, len(sorted_by_y)):
        gap = sorted_by_y[k]["y0"] - sorted_by_y[k - 1]["y1"]
        if 0 < gap < 100:  # Ignorer les gaps negatifs (chevauchements) et les tres grands
            gaps.append(gap)

    if gaps:
        median_gap = sorted(gaps)[len(gaps) // 2]
    else:
        median_gap = 15  # Fallback raisonnable

    # Gap max pour rester dans le meme tableau : 2.5x le gap median
    max_table_gap = max(median_gap * BORDERLESS_GAP_MULT, 10)

    rows = []
    used = set()

    for a in infos:
        if a["i"] in used:
            continue
        row = [a]
        for b in infos:
            if b["i"] == a["i"] or b["i"] in used:
                continue
            # Chevauchement vertical
            overlap = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
            min_h = min(a["h"], b["h"])
            if min_h > 0 and overlap > min_h * BORDERLESS_OVERLAP_RATIO:
                # Separation horizontale suffisante
                if abs(a["xc"] - b["xc"]) > min_x_sep:
                    row.append(b)
                    used.add(b["i"])

        if len(row) >= 2:
            used.add(a["i"])
            row.sort(key=lambda r: r["x0"])
            rows.append(row)

    if len(rows) < BORDERLESS_MIN_ROWS:
        return [], text_blocks

    # Trier les lignes par Y
    rows.sort(key=lambda r: min(item["y0"] for item in r))

    # Grouper les lignes consecutives avec gap dynamique
    table_regions = []
    current = [rows[0]]

    for k in range(1, len(rows)):
        prev_bottom = max(r["y1"] for r in rows[k - 1])
        curr_top = min(r["y0"] for r in rows[k])
        if curr_top - prev_bottom < max_table_gap:
            current.append(rows[k])
        else:
            if len(current) >= BORDERLESS_MIN_ROWS:
                table_regions.append(current)
            current = [rows[k]]

    if len(current) >= BORDERLESS_MIN_ROWS:
        table_regions.append(current)

    if not table_regions:
        return [], text_blocks

    verified_regions = []
    for region in table_regions:
        n_cols = max(len(r) for r in region)
        # Pour chaque colonne, collecter les centres X
        col_centers = [[] for _ in range(n_cols)]
        for row in region:
            for ci, item in enumerate(row):
                if ci < n_cols:
                    col_centers[ci].append(item["xc"])


        aligned = True
        for centers in col_centers:
            if len(centers) >= 2:
                avg = sum(centers) / len(centers)
                max_dev = max(abs(c - avg) for c in centers)
                if max_dev > page_width * BORDERLESS_ALIGN_MAX_DEV:
                    aligned = False
                    break

        if aligned:
            verified_regions.append(region)

    if not verified_regions:
        return [], text_blocks

    # Assembler en Markdown
    used_indices = set()
    elements = []

    for region in verified_regions:
        max_cols = max(len(r) for r in region)
        md_rows = []
        y_min = float("inf")

        for row in region:
            cells = []
            for item in row:
                used_indices.add(item["i"])
                content = text_blocks[item["i"]]["content"].strip()
                content = content.replace("\n", " ").replace("|", "/")
                cells.append(content)
            while len(cells) < max_cols:
                cells.append("")
            md_rows.append(cells)
            y_min = min(y_min, min(r["y0"] for r in row))

        if not md_rows:
            continue

        has_header = _is_header_row(md_rows[0], md_rows[1:]) if len(md_rows) > 1 else True
        lines = []
        if has_header:
            lines.append("| " + " | ".join(md_rows[0]) + " |")
            lines.append("| " + " | ".join(["---"] * max_cols) + " |")
            start = 1
        else:
            # Header vide plutot que "Col1/Col2/..." factice : le LLM
            # n'est pas trompe par des noms de colonnes inventes.
            lines.append("| " + " | ".join([""] * max_cols) + " |")
            lines.append("| " + " | ".join(["---"] * max_cols) + " |")
            start = 0

        for row in md_rows[start:]:
            lines.append("| " + " | ".join(row) + " |")

        elements.append({"y0": y_min, "type": "table", "content": "\n".join(lines)})

    remaining = [b for i, b in enumerate(text_blocks) if i not in used_indices]
    return elements, remaining


def _detect_page_images(fitz_page) -> list:

    page_width = fitz_page.rect.width
    page_height = fitz_page.rect.height
    page_area = page_width * page_height

    refs = []
    for info in fitz_page.get_image_info():
        bbox = info["bbox"]
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        img_area = w * h

        # Ignorer les images trop petites (icones, puces)
        if w < IMAGE_MIN_SIZE_PX or h < IMAGE_MIN_SIZE_PX:
            continue

        # Ignorer les images pleine page (fonds, watermarks)
        if img_area > page_area * IMAGE_FULL_PAGE_RATIO:
            continue

        # Ignorer les images dans le footer
        if bbox[1] / page_height > FOOTER_TOP_PERCENT:
            continue

        # Determiner le type d'image par ses proportions
        if w > h * 3:
            img_type = "bandeau"
        elif h > w * 3:
            img_type = "barre verticale"
        elif w > 200 and h > 200:
            img_type = "illustration/schema"
        elif w > 100 and h > 50:
            img_type = "image"
        else:
            img_type = "vignette"

        refs.append({
            "y0": bbox[1],
            "type": "image",
            "content": f"[Image presente : {img_type}, {w:.0f}x{h:.0f}px, "
                       f"position ({bbox[0]:.0f},{bbox[1]:.0f}). "
                       f"Consultez le document original pour visualiser cette image.]",
        })

    return refs


#  Couche 4 : Assemblage Markdown 

def _assemble_markdown(elements: List[Dict]) -> str:
    """Assemble les éléments en Markdown structuré."""
    parts = []

    for elem in elements:
        etype = elem["type"]
        content = elem["content"].strip()

        if not content:
            continue

        if etype == "heading":
            font_size = elem.get("font_size", 10)
            if font_size >= 13:
                parts.append(f"\n## {content}")
            elif font_size >= 10:
                parts.append(f"\n### {content}")
            else:
                parts.append(f"\n**{content}**")

        elif etype == "kv_group":
            formatted = _format_kv_block(elem)
            parts.append(formatted)

        elif etype == "table":
            parts.append(f"\n{content}")

        elif etype == "image":
            parts.append(f"\n{content}")

        else:  # text
            parts.append(content)

    return "\n\n".join(parts)


def _format_kv_block(block: Dict) -> str:

    lines = block.get("lines", [])
    formatted = []

    for line_data in lines:
        text = line_data["text"]
        if not text.strip():
            continue

        spans = line_data.get("spans", [])

        # Stratégie 1 : Si on a des spans bold+normal, extraire les paires
        pairs = _extract_kv_from_spans(spans)
        if pairs:
            for key, value in pairs:
                if value.strip():
                    formatted.append(f"**{key.strip()}** : {value.strip()}")
                else:
                    formatted.append(f"**{key.strip()}**")
            continue

        # Stratégie 2 : Fallback sur split par ":"
        if ":" in text:
            pairs = _extract_kv_pairs_text(text)
            if pairs:
                for key, value in pairs:
                    if value.strip():
                        formatted.append(f"**{key.strip()}** : {value.strip()}")
                    else:
                        formatted.append(f"**{key.strip()}**")
                continue

        formatted.append(text)

    return "\n".join(formatted)


def _extract_kv_from_spans(spans: list) -> List[Tuple[str, str]]:
    """
    Extrait les paires clé-valeur en utilisant les spans bold/normal.
    Un span bold suivi d'un span normal = une paire clé:valeur.
    """
    if not spans or len(spans) < 2:
        return []

    pairs = []
    current_key = ""
    current_value = ""
    collecting_value = False

    for span in spans:
        text = span["text"].strip()
        if not text:
            continue

        if span["bold"]:
            # Si on collectait une valeur, sauvegarder la paire précédente
            if collecting_value and current_key:
                # Nettoyer la clé (retirer le ":" final)
                clean_key = current_key.rstrip(": ").strip()
                if clean_key:
                    pairs.append((clean_key, current_value.strip()))
            current_key = text
            current_value = ""
            collecting_value = False
        else:
            if current_key:
                collecting_value = True
                current_value += " " + text if current_value else text

    # Dernière paire
    if current_key:
        clean_key = current_key.rstrip(": ").strip()
        if clean_key:
            pairs.append((clean_key, current_value.strip()))

    return pairs


def _extract_kv_pairs_text(text: str) -> List[Tuple[str, str]]:
    """Extrait les paires clé:valeur par analyse textuelle."""
    parts = text.split(":")
    if len(parts) < 2:
        return []

    pairs = []

    if len(parts) == 2:
        key = parts[0].strip()
        value = parts[1].strip()
        if len(key) <= KV_MAX_KEY_LEN and key:
            pairs.append((key, value))
        return pairs

    # Plusieurs paires — chercher les doubles espaces comme séparateurs
    current_key = parts[0].strip()
    for i in range(1, len(parts)):
        segment = parts[i]
        if i < len(parts) - 1:
            match = re.search(r'\s{2,}', segment)
            if match:
                value = segment[:match.start()].strip()
                next_key = segment[match.start():].strip()
            else:
                value = segment.strip()
                next_key = ""
            if current_key and len(current_key) <= KV_MAX_KEY_LEN:
                pairs.append((current_key, value))
            current_key = next_key
        else:
            value = segment.strip()
            if current_key and len(current_key) <= KV_MAX_KEY_LEN:
                pairs.append((current_key, value))

    return pairs


#  Utilitaires 

def _compute_median_font_size(doc) -> float:
    """Calcule la taille de police médiane du document."""
    sizes = []
    for page in doc:
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in blocks.get("blocks", []):
            if block["type"] != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    size = span.get("size", 0)
                    text = span.get("text", "").strip()
                    if size > 0 and text:
                        sizes.append(size)

    if not sizes:
        return 10.0
    sizes.sort()
    return sizes[len(sizes) // 2]


def _detect_repeated_blocks(doc) -> dict:
    """Détecte les textes répétés. Retourne {texte: premiere_page} pour garder la 1ere occurrence."""
    total = len(doc)
    if total < 3:
        return {}

    text_pages = {}
    for page_idx, page in enumerate(doc):
        page_texts = set()
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in blocks.get("blocks", []):
            if block["type"] != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
                if REPEATED_TEXT_MIN_LEN < len(text) < REPEATED_TEXT_MAX_LEN:
                    page_texts.add(text)
        for t in page_texts:
            if t not in text_pages:
                text_pages[t] = []
            text_pages[t].append(page_idx)

    threshold = total * REPEATED_TEXT_MIN_PAGES_RATIO
    return {text: pages[0] for text, pages in text_pages.items() if len(pages) >= threshold}


#  CLI 

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python pdf_parser.py <fichier.pdf>")
        sys.exit(1)

    file_path = sys.argv[1]
    print(f"Extraction du PDF : {file_path}")
    print("=" * 60)

    pages = parse_pdf(file_path)
    total_chars = sum(len(p["text"]) for p in pages)

    print(f"Pages extraites : {len(pages)}")
    print(f"Total caractères : {total_chars}")

    for page in pages:
        print(f"\n{'─' * 60}")
        print(f"PAGE {page['page']} ({len(page['text'])} chars, "
              f"{page.get('tables_count', 0)} tableaux)")
        print(f"{'─' * 60}")
        print(page["text"])