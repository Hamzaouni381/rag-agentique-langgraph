"""
Helpers de normalisation de tableaux
(cellules fusionnées rendues vides par
python-docx, headers wrappés sur plusieurs lignes, etc)
"""
from collections import Counter


def cell_is_numeric(cell: str) -> bool:
    """ contient un nombre (int/float, virgule ou espace)"""
    c = cell.strip().replace(",", ".").replace(" ", "")
    if not c:
        return False
    try:
        float(c)
        return True
    except ValueError:
        return False


def row_is_mostly_text(row: list) -> bool:
    """ si <30% des cellules non-vides sont numériques."""
    non_empty = [c for c in row if c.strip()]
    if not non_empty:
        return False
    numeric = sum(1 for c in non_empty if cell_is_numeric(c))
    return numeric / len(non_empty) < 0.3


def detect_multiline_header(cleaned: list, max_header_rows: int = 6) -> int:
    """
    Détecte combien de lignes consécutives en tête forment un header
      1 Row 0 doit être majoritairement textuelle.
      2 Une ligne suivante est continuation du header SI sa colonne 0 est
         vide ET row 0 a aussi la colonne 0 vide 
    """
    n = len(cleaned)
    if n <= 1:
        return 1
    if not row_is_mostly_text(cleaned[0]):
        return 1

    row0_col0_empty = (len(cleaned[0]) > 0 and not cleaned[0][0].strip())
    if not row0_col0_empty:
        return 1

    header_count = 1
    limit = min(max_header_rows, n - 1)
    for i in range(1, limit + 1):
        if i >= n:
            break
        row_i = cleaned[i]
        col0_empty = (len(row_i) > 0 and not row_i[0].strip())
        if col0_empty and row_is_mostly_text(row_i):
            header_count += 1
        else:
            break

    return header_count


def merge_header_rows(header_rows: list) -> list:
    """Fusionne plusieurs lignes de header en une seule par colonne, en dédupliquant"""
    if not header_rows:
        return []
    if len(header_rows) == 1:
        return header_rows[0]

    n_cols = max(len(r) for r in header_rows)
    merged = []
    for col_idx in range(n_cols):
        parts = []
        seen = set()
        for row in header_rows:
            if col_idx < len(row):
                cell = row[col_idx].strip()
                if cell and cell not in seen:
                    parts.append(cell)
                    seen.add(cell)
        merged.append(" ".join(parts))
    return merged


def fill_down_merged_cells(rows: list) -> list:
    """
    Recopie verticalement les cellules vides qui suivent une cellule pleine
    dans la même colonne 

    - cellule à remplir vide
    - une valeur existe au-dessus dans la même colonne
    - la ligne courante a au moins UNE autre cellule pleine 
    """
    if len(rows) < 2:
        return rows

    result = [list(r) for r in rows]
    n_cols = max(len(r) for r in result) if result else 0
    last_value = [None] * n_cols

    for row in result:
        non_empty_in_row = sum(1 for c in row if c.strip())
        is_data_row = non_empty_in_row > 0

        for col in range(min(len(row), n_cols)):
            if row[col].strip():
                last_value[col] = row[col]
            elif is_data_row and last_value[col] is not None:
                row[col] = last_value[col]

    return result


def is_header_row(first_row: list, data_rows: list) -> bool:
    """
    Compare le profil de la 1ère ligne avec les data : si profil identique
    (ex: tout numérique), pas un header.
    """
    if not data_rows:
        return True

    first_non_empty = [c for c in first_row if c.strip()]
    if not first_non_empty:
        return False

    first_numeric = sum(1 for c in first_non_empty if cell_is_numeric(c))
    first_ratio = first_numeric / len(first_non_empty)

    sample = data_rows[:min(10, len(data_rows))]
    data_numeric_ratios = []
    for row in sample:
        non_empty = [c for c in row if c.strip()]
        if non_empty:
            ratio = sum(1 for c in non_empty if cell_is_numeric(c)) / len(non_empty)
            data_numeric_ratios.append(ratio)

    if not data_numeric_ratios:
        return True

    avg_data_ratio = sum(data_numeric_ratios) / len(data_numeric_ratios)

    if first_ratio > 0.5 and avg_data_ratio > 0.5:
        return False
    if first_ratio < 0.3 and avg_data_ratio > 0.5:
        return True
    return True
