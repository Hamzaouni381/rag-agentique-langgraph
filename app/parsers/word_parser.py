"""
Extraction structuree de fichiers Word (.docx, .doc) pour RAG

Formats supportes :
  parse_word(path)  → .docx  (python-docx)
  parse_doc(path)   → .doc   (Word 97-2003, stratégie dual)
                               1. win32com  : conversion .doc→.docx temp → parse_word()
                               2. olefile   : extraction binaire OLE2 (fallback sans Word)
"""
import logging
import os
import re
from docx import Document
from app.parsers.table_utils import (
    detect_multiline_header, merge_header_rows, fill_down_merged_cells,
    is_header_row,
)

logger = logging.getLogger(__name__)


WORD_EXTENSIONS = (".doc", ".docx")


def is_word_document(source: str) -> bool:
    """Detecte si un chemin/nom de fichier correspond a un document Word
    """
    if not source or source == "?":
        return False
    return os.path.splitext(str(source))[1].lower() in WORD_EXTENSIONS


def parse_word(file_path: str) -> list[dict]:
    """Point d'entree. Retourne une liste de pages logiques structurees."""
    doc = Document(file_path)
    filename = os.path.basename(file_path)
    metadata = _extract_metadata(doc, filename)

    # Parcourir en ordre sequentiel (paragraphes + tableaux)
    elements = _extract_elements(doc)

    if not elements:
        return []

    # Decouper en pages logiques sur les Heading 1 et Heading 2
    pages = _split_into_logical_pages(elements, file_path, metadata)

    # Ajouter le header document sur la premiere page
    if pages:
        header = f"[Document: {filename}]\n---"
        pages[0]["text"] = f"{header}\n{pages[0]['text']}"

    return pages


def _extract_metadata(doc, filename: str) -> dict:
    """Recupere les metadonnees du document."""
    props = doc.core_properties
    meta = {"fichier": filename, "type_document": "docx"}
    if props.author:
        meta["auteur"] = props.author
    if props.title:
        meta["titre"] = props.title
    if props.created:
        meta["date_creation"] = props.created.isoformat()
    if props.modified:
        meta["date_modification"] = props.modified.isoformat()
    return meta


def _extract_elements(doc) -> list[dict]:
    """Parcourt le body XML en ordre et extrait paragraphes + tableaux."""
    elements = []

    # Construire un lookup rapide element -> paragraph / table
    para_map = {p._element: p for p in doc.paragraphs}
    table_map = {t._element: t for t in doc.tables}

    for child in doc.element.body:
        tag = child.tag.split("}")[-1]

        if tag == "p":
            para = para_map.get(child)
            if para is None:
                continue

            # Detecter les images dans le paragraphe
            drawing_ns = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
            has_image = child.find(f".//{drawing_ns}inline") is not None or \
                        child.find(f".//{drawing_ns}anchor") is not None
            if has_image:
                elements.append({
                    "type": "image",
                    "text": "[Image presente dans le document. Consultez le fichier original pour la visualiser.]",
                })

            text = para.text.strip()
            if not text:
                continue

            style_name = para.style.name if para.style else ""
            heading_level = _get_heading_level(style_name)

            if heading_level:
                elements.append({
                    "type": "heading",
                    "level": heading_level,
                    "text": text,
                })
            elif _is_list_paragraph(para):
                prefix = _get_list_prefix(para)
                elements.append({
                    "type": "list_item",
                    "text": f"{prefix} {text}",
                })
            else:
                # Extraire les runs avec formatting pour enrichir le contexte
                formatted = _extract_formatted_text(para)
                elements.append({
                    "type": "text",
                    "text": formatted if formatted != text else text,
                })

        elif tag == "tbl":
            table = table_map.get(child)
            if table is None:
                continue
            md = _word_table_to_markdown(table)
            if md:
                elements.append({"type": "table", "text": md})

    return elements


def _get_heading_level(style_name: str) -> int | None:
    """Retourne le niveau de heading (1-6) ou None."""
    if not style_name:
        return None
    # Gere "Heading 1", "Heading1", "heading 2", "Titre 1", "Titre1"
    m = re.match(r"(?:heading|titre)\s*(\d)", style_name, re.IGNORECASE)
    if m:
        return min(int(m.group(1)), 6)
    # Styles "Title" ou "Titre" sans numero = H1
    if style_name.lower() in ("title", "titre", "subtitle", "sous-titre"):
        return 1
    return None


def _is_list_paragraph(para) -> bool:
    """Detecte si un paragraphe est un element de liste."""
    # Methode 1 : numPr dans le XML (liste native Word)
    pPr = para._element.find(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr"
    )
    if pPr is not None:
        numPr = pPr.find(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr"
        )
        if numPr is not None:
            return True

    # Methode 2 : style "List Bullet", "List Number", etc.
    if para.style and para.style.name:
        name = para.style.name.lower()
        if any(kw in name for kw in ("list", "bullet", "puce", "numero")):
            return True

    # Methode 3 : detection par le texte (bullet unicode en debut)
    text = para.text.strip()
    if text and text[0] in "\u2022\u2023\u25cf\u25cb\u25aa\u25ab\u2013\u2014":
        return True

    return False


def _get_list_prefix(para) -> str:
    """Determine le prefixe de liste (puce ou numero)."""
    pPr = para._element.find(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr"
    )
    if pPr is not None:
        numPr = pPr.find(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr"
        )
        if numPr is not None:
            ilvl = numPr.find(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}ilvl"
            )
            level = int(ilvl.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val", "0")) if ilvl is not None else 0
            indent = "  " * level
            return f"{indent}-"

    return "-"


def _extract_formatted_text(para) -> str:
    """Extrait le texte avec marquage bold/italic """
    parts = []
    for run in para.runs:
        text = run.text
        if not text:
            continue
        if run.bold and run.italic:
            parts.append(f"**_{text}_**")
        elif run.bold:
            parts.append(f"**{text}**")
        elif run.italic:
            parts.append(f"_{text}_")
        else:
            parts.append(text)
    result = "".join(parts).strip()
    return result if result else para.text.strip()


def _word_table_to_markdown(table) -> str:
    """Convertit un tableau Word en Markdown avec gestion des cellules fusionnees
    """
    rows = []
    for row in table.rows:
        cells = []
        prev_text = None
        for cell in row.cells:
            text = cell.text.strip().replace("\n", " ").replace("|", "/")
            
            if text == prev_text and prev_text:
                cells.append("")  # Colonne fusionnee, laisser vide
            else:
                cells.append(text)
            prev_text = text

        if any(c for c in cells):
            rows.append(cells)

    if len(rows) < 1:
        return ""

    # Normaliser les colonnes
    max_cols = max(len(row) for row in rows)
    for row in rows:
        while len(row) < max_cols:
            row.append("")

    # Supprimer les colonnes entierement vides 
    cols_to_keep = [
        i for i in range(max_cols) if any(row[i].strip() for row in rows)
    ]
    if not cols_to_keep:
        return ""

    rows = [[row[i] for i in cols_to_keep] for row in rows]
    max_cols = len(cols_to_keep)

    has_real_header = is_header_row(rows[0], rows[1:]) if len(rows) > 1 else True

    lines = []
    if has_real_header:
        n_header = detect_multiline_header(rows)
        header = merge_header_rows(rows[:n_header])
        data_rows = fill_down_merged_cells(rows[n_header:])
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * max_cols) + " |")
        for row in data_rows:
            lines.append("| " + " | ".join(row) + " |")
    else:
        # Pas de header reel 
        data_rows = fill_down_merged_cells(rows)
        lines.append("| " + " | ".join([""] * max_cols) + " |")
        lines.append("| " + " | ".join(["---"] * max_cols) + " |")
        for row in data_rows:
            lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)


def _split_into_logical_pages(
    elements: list[dict], source: str, metadata: dict
) -> list[dict]:
    """
    Decoupe les elements en pages logiques.
    Chaque Heading 1 ou Heading 2 demarre une nouvelle page.
    """
    # Verifier s'il y a des headings significatifs
    has_headings = any(
        e["type"] == "heading" and e["level"] <= 2 for e in elements
    )

    if not has_headings:
        # Pas de structure de sections : une seule page
        full_text = _elements_to_text(elements)
        if not full_text.strip():
            return []
        return [{
            "text": full_text.strip(),
            "page": 1,
            "source": source,
            "total_pages": 1,
            "document_type": "docx",
            "metadata": metadata,
        }]

    # Decouper par sections
    pages = []
    current_elements = []
    current_section = ""

    for elem in elements:
        if elem["type"] == "heading" and elem["level"] <= 2:
            # Sauvegarder la section precedente
            if current_elements:
                text = _elements_to_text(current_elements)
                if text.strip():
                    pages.append(text.strip())
            current_elements = []
            current_section = elem["text"]
            # Marquer la section pour le chunker/indexer
            current_elements.append({
                "type": "section_marker",
                "text": f"[SECTION] {elem['text']}",
            })
        else:
            current_elements.append(elem)

    # Derniere section
    if current_elements:
        text = _elements_to_text(current_elements)
        if text.strip():
            pages.append(text.strip())

    total = len(pages)
    return [
        {
            "text": page_text,
            "page": i + 1,
            "source": source,
            "total_pages": total,
            "document_type": "docx",
            "metadata": metadata,
        }
        for i, page_text in enumerate(pages)
    ]


def _elements_to_text(elements: list[dict]) -> str:
    """Assemble les elements en texte structure."""
    parts = []
    for elem in elements:
        etype = elem["type"]
        text = elem["text"]

        if etype == "heading":
            level = elem.get("level", 3)
            prefix = "#" * min(level, 4)
            parts.append(f"\n{prefix} {text}")
        elif etype == "section_marker":
            parts.append(f"\n{text}")
        elif etype == "table":
            parts.append(f"\n[TABLEAU]\n{text}")
        elif etype == "image":
            parts.append(text)
        elif etype == "list_item":
            parts.append(text)
        else:
            parts.append(text)

    return "\n\n".join(parts)


#  PARSE .DOC  (Word 97-2003)

def parse_doc(file_path: str) -> list[dict]:
    """
    Parse un fichier .doc (Word 97-2003).

    Stratégie en deux étapes :
      1. win32com  — ouvre le .doc via Microsoft Word (COM), le convertit en
                     .docx temporaire, puis appelle parse_word() dessus.
                     Qualité identique à .docx 
      2. olefile   — lit directement le flux binaire OLE2 WordDocument et
                     extrait le texte brut (fallback sans Word installé)
    """
    logger.info("[DOC] Début extraction : %s", os.path.basename(file_path))

    # Tentative 1 : via Microsoft Word (COM automation)
    try:
        pages = _parse_doc_via_word(file_path)
        logger.info("[DOC] %d page(s) extraite(s) via Word COM", len(pages))
        return pages
    except Exception as e:
        logger.warning("[DOC] Word COM indisponible (%s) — fallback olefile", type(e).__name__)

    # Tentative 2 : extraction binaire OLE2 (olefile)
    try:
        pages = _parse_doc_via_olefile(file_path)
        logger.info("[DOC] %d page(s) extraite(s) via olefile", len(pages))
        return pages
    except Exception as e:
        logger.error("[DOC] Échec total : %s — %s", os.path.basename(file_path), e)
        return []


def _parse_doc_via_word(file_path: str) -> list[dict]:
    """
    Conversion .doc → .docx temporaire via COM Word, puis parse_word().
    Nécessite Microsoft Word installé
    """
    import tempfile
    import win32com.client

    abs_path = os.path.abspath(file_path)
    tmp_dir  = tempfile.mkdtemp()
    basename = os.path.splitext(os.path.basename(file_path))[0]
    tmp_docx = os.path.join(tmp_dir, basename + ".docx")

    word = None
    try:
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        word.DisplayAlerts = False

        doc = word.Documents.Open(abs_path, ReadOnly=True)
        # wdFormatXMLDocument = 12  (.docx, Office Open XML)
        doc.SaveAs2(tmp_docx, FileFormat=12)
        doc.Close(False)

        logger.info("[DOC] Conversion .docx réussie via Word COM")
        pages = parse_word(tmp_docx)

        # Marquer le type original comme "doc" dans toutes les pages
        for page in pages:
            page["document_type"] = "doc"
            if "metadata" in page:
                page["metadata"]["type_document"] = "doc"
        return pages

    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        try:
            if os.path.exists(tmp_docx):
                os.unlink(tmp_docx)
            if os.path.exists(tmp_dir):
                os.rmdir(tmp_dir)
        except Exception:
            pass


def _parse_doc_via_olefile(file_path: str) -> list[dict]:
    """
    Extraction de texte depuis un fichier .doc Word 97-2003 via lecture OLE2 binaire.
    """
    import olefile
    import struct

    filename = os.path.basename(file_path)

    if not olefile.isOleFile(file_path):
        raise ValueError(f"Fichier non reconnu comme OLE2 : {filename}")

    ole = olefile.OleFileIO(file_path)
    try:
        if not ole.exists("WordDocument"):
            raise ValueError(f"Flux WordDocument absent dans : {filename}")

        wd_data = ole.openstream("WordDocument").read()

        # Vérification magic number Word (FIB.wIdent = 0xA5EC)
        if len(wd_data) < 32:
            raise ValueError("Fichier .doc trop court ou corrompu")

        w_ident = struct.unpack_from("<H", wd_data, 0)[0]
        if w_ident != 0xA5EC:
            raise ValueError(f"Magic number invalide : 0x{w_ident:04X} (attendu 0xA5EC)")

        # Vérification chiffrement (FibBase.flags bit 8 = fEncrypted)
        flags = struct.unpack_from("<H", wd_data, 0x0A)[0]
        if flags & 0x0100:
            return [{
                "text": (
                    f"[Document: {filename}]\n---\n"
                    "[Ce document est chiffré. Déchiffrez-le avant l'ingestion.]"
                ),
                "page": 1,
                "source": file_path,
                "total_pages": 1,
                "document_type": "doc",
                "metadata": {"fichier": filename, "type_document": "doc"},
            }]

        text = _decode_word_stream(wd_data)
    finally:
        ole.close()

    if not text.strip():
        return []

    header = f"[Document: {filename}]\n---"
    full_text = f"{header}\n{text}"

    return [{
        "text": full_text,
        "page": 1,
        "source": file_path,
        "total_pages": 1,
        "document_type": "doc",
        "metadata": {"fichier": filename, "type_document": "doc"},
    }]


def _decode_word_stream(data: bytes) -> str:
    """
    Extrait le texte lisible depuis le flux binaire WordDocument.
    """
    # La partie texte commence après le FIB de base + extensions
    text_portion = data[0x80:]

    # Tentative UTF-16LE (encodage natif Word 97+)
    try:
        decoded = text_portion.decode("utf-16-le", errors="ignore")
        clean = _clean_binary_text(decoded)
        if len(clean.strip()) > 50:
            return clean
    except Exception:
        pass

    # Tentative CP1252 sur tout le flux
    try:
        decoded_full = data.decode("cp1252", errors="ignore")
        clean = _clean_binary_text(decoded_full)
        if len(clean.strip()) > 50:
            return clean
    except Exception:
        pass

    # Dernier recours : Latin-1 avec filtre printable
    decoded_latin = data.decode("latin-1", errors="ignore")
    printable = "".join(
        c for c in decoded_latin
        if c in " \t\n\r" or (0x20 <= ord(c) <= 0x7E) or (0xC0 <= ord(c) <= 0xFF)
    )
    return _clean_binary_text(printable)


def _clean_binary_text(text: str) -> str:
    """Nettoie le texte extrait du binaire Word : supprime les contrôles, normalise les espaces."""
    # Supprimer les caractères de contrôle (sauf \n, \t)
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F\uFFFD\uFFFE\uFFFF]", " ", text)
    # Word utilise \r comme saut de paragraphe
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Compresser les espaces multiples sur une ligne
    text = re.sub(r"[ \t]{4,}", " ", text)
    # Supprimer les lignes vides répétées (max 2 sauts consécutifs)
    text = re.sub(r"\n{4,}", "\n\n", text)
    # Garder uniquement les lignes contenant au moins un caractère alphanumérique
    lines = [
        ln for ln in text.splitlines()
        if re.search(r"[a-zA-Z0-9\u00C0-\u024F\u0400-\u04FF]", ln)
    ]
    return "\n".join(lines).strip()


#  POINT D'ENTRÉE CLI

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python word_parser.py <fichier.docx|.doc>")
        sys.exit(1)

    path = sys.argv[1]
    ext  = os.path.splitext(path)[1].lower()

    if ext == ".doc":
        print(f"Extraction DOC : {path}")
        pages = parse_doc(path)
    else:
        print(f"Extraction DOCX : {path}")
        pages = parse_word(path)

    print("=" * 60)
    total_chars = sum(len(p["text"]) for p in pages)
    print(f"Pages logiques : {len(pages)}")
    print(f"Total caracteres : {total_chars}")

    for page in pages:
        print(f"\n{'_' * 60}")
        print(f"PAGE {page['page']} ({len(page['text'])} chars) [type={page.get('document_type', '?')}]")
        print(f"{'_' * 60}")
        print(page["text"])