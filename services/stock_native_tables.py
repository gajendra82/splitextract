"""Native-lane table builders for the shared stock header resolver (Phase 3b).

Pure builders: turn sheet / text / PDF / DOCX sources into header_cells +
row tokens. No I/O beyond structures passed in. No Gemini.
"""

from __future__ import annotations

import logging
import os
import re
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

from services.stock_header_resolver import (
    ALIASES,
    normalize_header,
    resolve_columns,
)

_CLOSING_ALIASES = frozenset(
    normalize_header(a) for a in (ALIASES.get("closing") or [])
) | {
    normalize_header(a)
    for a in (
        "cls",
        "cl",
        "bal",
        "balance",
        "closing qty",
        "closing stock",
        "closstock",
        "cl stk",
        "cb",
    )
}


def _is_closing_alias(text: Any) -> bool:
    norm = normalize_header(text)
    if not norm:
        return False
    if norm in _CLOSING_ALIASES:
        return True
    compact = re.sub(r"\s+", "", norm)
    if compact in {"cls", "cl", "bal", "cb", "closing", "closstock", "clstk", "clsqty"}:
        return True
    from services.stock_header_resolver import _pick_group

    group, _conf, _reason = _pick_group(norm)
    return group == "closing"


def _alias_hit(text: Any) -> bool:
    """True when the cell text hits any resolver alias group."""
    norm = normalize_header(text)
    if not norm:
        return False
    from services.stock_header_resolver import _pick_group

    group, conf, _reason = _pick_group(norm)
    return group is not None and conf > 0


def _cell_text(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, dict):
        return str(cell.get("text") or cell.get("value") or "").strip()
    return str(cell).strip()


def _header_cells_from_row(
    row: Sequence[Any],
    *,
    subheader: Optional[Sequence[Any]] = None,
    use_index: bool = True,
) -> List[Dict[str, Any]]:
    cells: List[Dict[str, Any]] = []
    width = max(len(row), len(subheader or []))
    for idx in range(width):
        text = _cell_text(row[idx] if idx < len(row) else "")
        sub = ""
        if subheader is not None and idx < len(subheader):
            sub = _cell_text(subheader[idx])
        if not text and not sub:
            continue
        # Expand merged-looking blanks already handled by caller; here one cell/col.
        entry: Dict[str, Any] = {
            "text": text or sub,
            "col_index": idx,
            "x_center": float(idx) if use_index else None,
            "subheader_text": sub or None,
        }
        cells.append(entry)
    return cells


def _score_header_cells(header_cells: List[Dict[str, Any]]) -> Tuple[int, bool]:
    hits = 0
    has_closing = False
    for cell in header_cells:
        text = cell.get("text") or ""
        sub = cell.get("subheader_text") or ""
        combined = f"{text} {sub}".strip()
        if _alias_hit(combined) or _alias_hit(text):
            hits += 1
        if _is_closing_alias(combined) or _is_closing_alias(text) or _is_closing_alias(sub):
            has_closing = True
    return hits, has_closing


def from_sheet(
    rows: List[List[Any]],
) -> List[Dict[str, Any]]:
    """Return candidates of {header_cells, data_rows, header_row_index}.

    Header = row (or two-row group) with the most resolver alias hits, at least
    3 hits including a closing alias. Merged header cells: a non-empty cell
    followed by empty cells that belong to the same span is expanded by the
    caller via subheader pairing; here empty trailing cells inherit the prior
    non-empty parent label as text for alias scoring only when the next row
    has Qty/Value children.
    """
    if not rows:
        return []
    best: Optional[Dict[str, Any]] = None
    best_hits = 0
    scan = min(len(rows), 60)
    for ri in range(scan):
        row = rows[ri] if isinstance(rows[ri], list) else []
        candidates = [(row, None, ri)]
        if ri + 1 < scan and isinstance(rows[ri + 1], list):
            # Two-row: parent on ri, Qty/Value on ri+1.
            candidates.append((row, rows[ri + 1], ri))
        for parent, child, header_idx in candidates:
            # Expand merged parent: fill empty parent cells from last non-empty.
            expanded = list(parent)
            last = ""
            for i, cell in enumerate(expanded):
                text = _cell_text(cell)
                if text:
                    last = text
                elif last and child is not None and i < len(child) and _cell_text(child[i]):
                    expanded[i] = last
            header_cells = _header_cells_from_row(expanded, subheader=child)
            hits, has_closing = _score_header_cells(header_cells)
            if hits < 3 or not has_closing:
                continue
            if hits > best_hits:
                data_start = header_idx + (2 if child is not None else 1)
                data_rows = [
                    r for r in rows[data_start:] if isinstance(r, list)
                ]
                best_hits = hits
                best = {
                    "header_cells": header_cells,
                    "data_rows": data_rows,
                    "header_row_index": header_idx,
                    "two_row_header": child is not None,
                }
    return [best] if best else []


def from_fixed_width(
    lines: Sequence[str],
    column_plan: Dict[str, Any],
) -> Dict[str, Any]:
    """Build header_cells / tokens from a fixed-width column plan.

    column_plan values are (start, end) char windows or dicts with start/end.
    """
    header_cells: List[Dict[str, Any]] = []
    windows: List[Tuple[str, int, int]] = []
    for idx, (name, win) in enumerate(column_plan.items()):
        if isinstance(win, dict):
            start = int(win.get("start") or win.get("begin") or 0)
            end = int(win.get("end") or start + 1)
            label = str(win.get("label") or name)
        elif isinstance(win, (list, tuple)) and len(win) >= 2:
            start, end = int(win[0]), int(win[1])
            label = str(name)
        else:
            continue
        center = (start + end) / 2.0
        header_cells.append(
            {
                "text": label,
                "col_index": idx,
                "x_center": center,
                "subheader_text": None,
            }
        )
        windows.append((str(name), start, end))

    data_rows: List[List[Dict[str, Any]]] = []
    for line in lines:
        if not isinstance(line, str):
            continue
        tokens: List[Dict[str, Any]] = []
        for idx, (_name, start, end) in enumerate(windows):
            chunk = line[start:end] if start < len(line) else ""
            text = chunk.strip()
            if not text:
                continue
            tokens.append(
                {
                    "text": text,
                    "col_index": idx,
                    "x": (start + end) / 2.0,
                }
            )
        if tokens:
            data_rows.append(tokens)
    return {"header_cells": header_cells, "data_rows": data_rows}


def from_text_header(lines: Sequence[str]) -> Dict[str, Any]:
    """TXT / legacy-doc text: header by alias hits; x = character centre."""
    best_idx = -1
    best_hits = 0
    best_tokens: List[str] = []
    for i, line in enumerate(lines[:80]):
        if not isinstance(line, str) or not line.strip():
            continue
        # Prefer whitespace-split tokens preserving positions.
        parts = list(re.finditer(r"\S+", line))
        if len(parts) < 3:
            continue
        hits = sum(1 for m in parts if _alias_hit(m.group(0)))
        has_closing = any(_is_closing_alias(m.group(0)) for m in parts)
        if hits >= 3 and has_closing and hits > best_hits:
            best_hits = hits
            best_idx = i
            best_tokens = [m.group(0) for m in parts]

    if best_idx < 0:
        return {"header_cells": [], "data_rows": []}

    header_line = lines[best_idx]
    header_cells: List[Dict[str, Any]] = []
    for idx, m in enumerate(re.finditer(r"\S+", header_line)):
        start, end = m.start(), m.end()
        header_cells.append(
            {
                "text": m.group(0),
                "col_index": idx,
                "x_center": (start + end) / 2.0,
                "subheader_text": None,
            }
        )

    data_rows: List[List[Dict[str, Any]]] = []
    for line in lines[best_idx + 1 :]:
        if not isinstance(line, str) or not line.strip():
            continue
        if re.match(r"^\s*TOTAL\b", line, re.I):
            continue
        tokens: List[Dict[str, Any]] = []
        for idx, m in enumerate(re.finditer(r"\S+", line)):
            start, end = m.start(), m.end()
            tokens.append(
                {
                    "text": m.group(0),
                    "col_index": idx,
                    "x": (start + end) / 2.0,
                }
            )
        if tokens:
            data_rows.append(tokens)
    return {"header_cells": header_cells, "data_rows": data_rows}


_QUALIFIER_WORDS = frozenset(
    {
        "free",
        "stock",
        "rate",
        "amt",
        "amount",
        "qty",
        "value",
        "val",
        "quantity",
    }
)


def _is_qualifier_text(text: Any) -> bool:
    tokens = [t for t in normalize_header(text).split() if t]
    return bool(tokens) and all(t in _QUALIFIER_WORDS for t in tokens)


_HEADER_LEXICON = frozenset(
    {
        "product",
        "pack",
        "packing",
        "opening",
        "open",
        "receipt",
        "issue",
        "free",
        "i",
        "sales",
        "sale",
        "closing",
        "close",
        "amount",
        "amt",
        "qty",
        "quantity",
        "value",
        "val",
        "stock",
        "rate",
        "division",
        "div",
    }
) | _QUALIFIER_WORDS


def _cell_is_header_lexicon(text: Any) -> bool:
    tokens = [t for t in normalize_header(text).split() if t]
    return bool(tokens) and all(t in _HEADER_LEXICON for t in tokens)


def _place_header_word(
    header: List[Dict[str, Any]], cell: Dict[str, Any], *, prepend: bool
) -> None:
    """Overlap an existing header cell, or insert a column that sits in a gap."""
    x0 = float(cell.get("x0") or 0)
    x1 = float(cell.get("x1") or x0)
    word = str(cell.get("text") or "").strip()
    if not word:
        return
    for host in header:
        h0 = float(host.get("x0") or 0)
        h1 = float(host.get("x1") or h0)
        if min(h1, x1) >= max(h0, x0) - 2:
            if word.lower() not in str(host.get("text") or "").lower():
                host["text"] = (
                    (word + " " + str(host.get("text") or "")).strip()
                    if prepend
                    else (str(host.get("text") or "") + " " + word).strip()
                )
            host["x0"] = min(h0, x0)
            host["x1"] = max(h1, x1)
            return
    header.append(
        {
            "text": word,
            "col_index": len(header),
            "x_center": None,
            "subheader_text": None,
            "x0": x0,
            "x1": x1,
        }
    )


def _absorb_nearby_header_rows(
    header: List[Dict[str, Any]],
    clustered: List[List[Dict[str, Any]]],
    rows: List[List[Dict[str, Any]]],
    best_i: int,
    med_h: float,
) -> List[int]:
    """Pull Sales/Amount/Closing words on the lines just above and below the header.

    A word that overlaps a header cell is glued onto it. A word that sits in
    the gap between cells becomes its own column (Sales Amount, Closing Amount).
    """
    if best_i < 0 or not header or best_i >= len(rows):
        return []
    header_top = float((rows[best_i][0] or {}).get("top") or 0)
    limit = max(8.0, 1.25 * float(med_h or 8.0))
    consumed: List[int] = []
    for idx in (best_i - 1, best_i + 1):
        if idx < 0 or idx >= len(clustered) or idx >= len(rows) or not rows[idx]:
            continue
        top = float((rows[idx][0] or {}).get("top") or 0)
        if abs(top - header_top) > limit:
            continue
        cells = clustered[idx]
        if not cells or not all(_cell_is_header_lexicon(c.get("text")) for c in cells):
            continue
        for cell in cells:
            _place_header_word(header, cell, prepend=idx < best_i)
        consumed.append(idx)
    if not consumed:
        return []
    width = None
    for host in header:
        xc = host.get("x_center")
        if xc:
            mid = (float(host.get("x0") or 0) + float(host.get("x1") or 0)) / 2.0
            if mid:
                width = mid / float(xc)
                break
    header.sort(key=lambda c: float(c.get("x0") or 0))
    for idx, host in enumerate(header):
        host["col_index"] = idx
        mid = (float(host.get("x0") or 0) + float(host.get("x1") or 0)) / 2.0
        if width:
            host["x_center"] = mid / width
    logger.info(
        "STOCK_PDF_WORDS_HEADER_ABSORB cols=%s",
        [c.get("text") for c in header],
    )
    return consumed


def _merge_qualifier_subheader(
    header: List[Dict[str, Any]], next_cells: List[Dict[str, Any]]
) -> bool:
    """Glue a second header line (Free / Stock / Rate / Amt) onto the parent column.

    Only when every word on that line is a qualifier. Does not invent columns.
    """
    if not header or not next_cells or len(next_cells) > len(header):
        return False
    if not all(_is_qualifier_text(c.get("text")) for c in next_cells):
        return False
    assignments: List[Tuple[Dict[str, Any], str]] = []
    for cell in next_cells:
        x0 = float(cell.get("x0") or 0)
        x1 = float(cell.get("x1") or x0)
        xc = (x0 + x1) / 2.0
        chosen: Optional[Dict[str, Any]] = None
        for h in header:
            h0 = float(h.get("x0") or 0)
            h1 = float(h.get("x1") or h0)
            if h0 - 2 <= xc <= h1 + 2:
                chosen = h
                break
        if chosen is None:
            best_d: Optional[float] = None
            for h in header:
                h0 = float(h.get("x0") or 0)
                h1 = float(h.get("x1") or h0)
                dist = abs(xc - ((h0 + h1) / 2.0))
                if best_d is None or dist < best_d:
                    best_d = dist
                    chosen = h
            if chosen is None or best_d is None or best_d > 36:
                return False
        assignments.append((chosen, str(cell.get("text") or "").strip()))
    for host, word in assignments:
        if word and word.lower() not in str(host.get("text") or "").lower():
            host["text"] = (str(host.get("text") or "") + " " + word).strip()
    logger.info(
        "STOCK_PDF_WORDS_SUBHEADER merged=%s",
        [c.get("text") for c in header],
    )
    return True


def _is_footer_or_total_tokens(tokens: List[Dict[str, Any]]) -> bool:
    texts = [str(t.get("text") or "").strip() for t in tokens if isinstance(t, dict)]
    if not texts:
        return True
    joined = " ".join(texts)
    if re.search(r"^\s*(?:GRAND\s+)?TOTAL\b|PAGE\s*TOTAL|CONTINUED|Page\s*No", joined, re.I):
        return True
    first = texts[0]
    if re.match(r"^(?:GRAND\s+)?TOTAL\b", first, re.I):
        return True
    alpha = next((t for t in texts if re.search(r"[A-Za-z]", t)), "")
    if re.match(r"^(?:GRAND\s+|PAGE\s+|SUB\s+)?TOTAL\b", alpha, re.I):
        return True
    return False


def _is_repeated_header_tokens(
    tokens: List[Dict[str, Any]], header_cells: List[Dict[str, Any]]
) -> bool:
    """True when a data row looks like a reprinted column header."""
    if not tokens or not header_cells:
        return False
    hits = sum(1 for t in tokens if _alias_hit(t.get("text")))
    has_closing = any(_is_closing_alias(t.get("text")) for t in tokens)
    return hits >= 3 and has_closing


def _header_x_centres(header_cells: List[Dict[str, Any]]) -> List[float]:
    return [
        float(c["x_center"])
        for c in header_cells
        if isinstance(c, dict) and c.get("x_center") is not None
    ]


def _headers_align(
    header_a: List[Dict[str, Any]], header_b: List[Dict[str, Any]]
) -> bool:
    """True when header B's x-centres line up with A within 0.65 × median pitch."""
    xs_a = _header_x_centres(header_a)
    xs_b = _header_x_centres(header_b)
    if len(xs_a) < 2 or len(xs_b) < 2:
        return False
    gaps = [xs_a[i + 1] - xs_a[i] for i in range(len(xs_a) - 1)]
    pitch = float(median(gaps)) if gaps else 0.1
    if pitch <= 0:
        pitch = 0.1
    tol = 0.65 * pitch
    # Compare overlapping prefix of centres (same column count preferred).
    n = min(len(xs_a), len(xs_b))
    matched = 0
    for i in range(n):
        if abs(xs_a[i] - xs_b[i]) <= tol:
            matched += 1
    return matched >= max(3, int(0.7 * n))


def _tokens_align_to_header(
    tokens: List[Dict[str, Any]], header_cells: List[Dict[str, Any]]
) -> bool:
    """True when token x's line up with header centres within 0.65 × median pitch."""
    xs_h = _header_x_centres(header_cells)
    xs_t = [
        float(t["x"])
        for t in tokens
        if isinstance(t, dict) and t.get("x") is not None
    ]
    if len(xs_h) < 2 or len(xs_t) < 2:
        return False
    gaps = [xs_h[i + 1] - xs_h[i] for i in range(len(xs_h) - 1)]
    pitch = float(median(gaps)) if gaps else 0.1
    if pitch <= 0:
        pitch = 0.1
    tol = 0.65 * pitch
    matched = 0
    for tx in xs_t:
        if any(abs(tx - hx) <= tol for hx in xs_h):
            matched += 1
    return matched >= max(2, int(0.6 * len(xs_t)))


_QTY_VALUE_CHILD_WORDS = frozenset(
    {"qty", "quantity", "qnty", "value", "val", "amt", "amount"}
)
_GROUP_LABELS = frozenset(
    {
        "opening",
        "purchase",
        "sales",
        "closing",
        "free",
        "sales_return",
        "purchase_return",
    }
)


def _group_qty_value_header_enabled() -> bool:
    """STOCK_PDF_GROUP_QTY_VALUE — pair Opening/Purchase/… with Qty/value children.

    Default ON. Set false to keep the group-label row as the only header.
    """
    return os.getenv("STOCK_PDF_GROUP_QTY_VALUE", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _header_group_name(text: Any) -> Optional[str]:
    from services.stock_header_resolver import _pick_group

    group, _conf, _reason = _pick_group(normalize_header(text))
    if group in _GROUP_LABELS:
        return group
    if isinstance(group, str) and group.startswith("direct:"):
        field = group.split(":", 1)[1]
        if field.startswith("opening"):
            return "opening"
        if field.startswith("purchase"):
            return "purchase"
        if field.startswith("sales"):
            return "sales"
        if field.startswith("closing"):
            return "closing"
    return None


def _row_is_qty_value_children(cells: List[Dict[str, Any]]) -> bool:
    """Product/Rate plus repeated Qty/value, with no data numbers."""
    if not cells or len(cells) < 6:
        return False
    qty_value = 0
    for cell in cells:
        text = cell.get("text")
        if not _cell_is_header_lexicon(text):
            return False
        if normalize_header(text) in _QTY_VALUE_CHILD_WORDS:
            qty_value += 1
    return qty_value >= 4


def _row_is_group_labels(cells: List[Dict[str, Any]]) -> bool:
    if not cells:
        return False
    groups = 0
    for cell in cells:
        if _header_group_name(cell.get("text")):
            groups += 1
    return groups >= 2


def _parent_covering_x(
    x_center: float, parents: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Parent whose midpoint band contains this child column."""
    if not parents:
        return None
    centers = [
        (float(p.get("x0") or 0) + float(p.get("x1") or 0)) / 2.0 for p in parents
    ]
    for index, parent in enumerate(parents):
        left = float("-inf") if index == 0 else (centers[index - 1] + centers[index]) / 2.0
        right = (
            float("inf")
            if index == len(parents) - 1
            else (centers[index] + centers[index + 1]) / 2.0
        )
        if left <= x_center < right or (
            index == len(parents) - 1 and x_center >= left
        ):
            return parent
    return parents[-1]


def _left_align_text_column_centers(cells: List[Dict[str, Any]]) -> None:
    """Product is printed at the left of a wide column, not on the word centre.

    Qty/value headers sit on their numbers. A short left-aligned Product label
    would otherwise steal only the first word and drop the rest into Rate.
    """
    for index, cell in enumerate(cells[:-1]):
        if cell.get("subheader_text"):
            continue
        if normalize_header(cell.get("text")) not in {
            "product",
            "product name",
            "item",
            "item name",
            "description",
            "particular",
            "particulars",
        }:
            continue
        nxt = cells[index + 1]
        x0 = float(cell.get("x0") or 0)
        word_mid = (x0 + float(cell.get("x1") or x0)) / 2.0
        nxt_x0 = float(nxt.get("x0") or 0)
        if nxt_x0 <= x0 or word_mid <= 0:
            continue
        gap_mid = (x0 + nxt_x0) / 2.0
        if word_mid + 1 >= gap_mid:
            continue
        old = float(cell.get("x_center") or 0)
        if old <= 0:
            continue
        cell["x_center"] = old * (gap_mid / word_mid)


def _pair_group_labels_with_children(
    parents: List[Dict[str, Any]], children: List[Dict[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    """One header cell per child column, labelled by the group above it."""
    group_parents = [
        p for p in parents if _header_group_name(p.get("text"))
    ]
    group_parents.sort(
        key=lambda p: (float(p.get("x0") or 0) + float(p.get("x1") or 0)) / 2.0
    )
    if len(group_parents) < 2 or not _row_is_qty_value_children(children):
        return None
    paired: List[Dict[str, Any]] = []
    for cell in sorted(children, key=lambda c: float(c.get("x0") or 0)):
        entry = dict(cell)
        norm = normalize_header(cell.get("text"))
        if norm in _QTY_VALUE_CHILD_WORDS:
            xc = (float(cell.get("x0") or 0) + float(cell.get("x1") or 0)) / 2.0
            parent = _parent_covering_x(xc, group_parents)
            if parent is None:
                continue
            entry["text"] = str(parent.get("text") or "")
            entry["subheader_text"] = str(cell.get("text") or "")
        else:
            entry["subheader_text"] = None
        entry["_group_qty_value"] = True
        paired.append(entry)
    if len(paired) < 6:
        return None
    for index, cell in enumerate(paired):
        cell["col_index"] = index
    _left_align_text_column_centers(paired)
    logger.info(
        "STOCK_PDF_GROUP_QTY_VALUE cols=%s",
        [
            (
                (str(c.get("text") or "") + " " + str(c.get("subheader_text") or "")).strip()
            )
            for c in paired
        ],
    )
    return paired


def _expand_group_qty_value_header(
    header: List[Dict[str, Any]],
    clustered: List[List[Dict[str, Any]]],
    best_i: int,
    already_skipped: Sequence[int],
) -> Optional[Tuple[List[Dict[str, Any]], List[int]]]:
    """Opening | Purchase | Sales above Qty | value is not one column per group."""
    if not _group_qty_value_header_enabled():
        return None
    if best_i < 0 or not header or best_i >= len(clustered):
        return None
    skipped = {int(i) for i in already_skipped}
    # Group words on this row, Qty/value on the next row.
    if best_i + 1 < len(clustered) and (best_i + 1) not in skipped:
        paired = _pair_group_labels_with_children(header, clustered[best_i + 1])
        if paired:
            return paired, [best_i + 1]
    # Qty/value won header scoring; group words are the row above.
    if best_i - 1 >= 0 and (best_i - 1) not in skipped:
        paired = _pair_group_labels_with_children(clustered[best_i - 1], header)
        if paired:
            return paired, [best_i - 1]
    return None


def _extract_pdf_page_rows(
    page: Any,
    *,
    page_width: Optional[float] = None,
) -> Dict[str, Any]:
    """Single-page words → optional header_cells + raw row token lists."""
    try:
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    except Exception:
        words = []
    if not words:
        return {"header_cells": [], "all_rows": [], "header_row_index": -1}

    width = float(page_width or getattr(page, "width", None) or 1.0)
    if width <= 0:
        width = 1.0

    heights = [float(w.get("bottom", 0) - w.get("top", 0)) for w in words]
    med_h = float(median(heights)) if heights else 8.0
    y_tol = 0.5 * med_h

    ordered = sorted(
        words, key=lambda w: (float(w.get("top") or 0), float(w.get("x0") or 0))
    )
    rows: List[List[Dict[str, Any]]] = []
    for w in ordered:
        top = float(w.get("top") or 0)
        if rows:
            prev_top = float(rows[-1][0].get("top") or 0)
            if abs(top - prev_top) <= y_tol:
                rows[-1].append(w)
                continue
        rows.append([w])

    def _cluster_row(row_words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        row_words = sorted(row_words, key=lambda w: float(w.get("x0") or 0))
        inter: List[float] = []
        for i in range(1, len(row_words)):
            g = float(row_words[i].get("x0") or 0) - float(
                row_words[i - 1].get("x1") or 0
            )
            if g >= 0:
                inter.append(g)
        med_gap = float(median(inter)) if inter else med_h
        gap = max(1.0, 0.35 * med_gap)
        cells: List[Dict[str, Any]] = []
        for w in row_words:
            x0 = float(w.get("x0") or 0)
            x1 = float(w.get("x1") or x0)
            text = str(w.get("text") or "").strip()
            if not text:
                continue
            if cells and x0 - float(cells[-1]["x1"]) <= gap:
                cells[-1]["text"] = (cells[-1]["text"] + " " + text).strip()
                cells[-1]["x1"] = max(float(cells[-1]["x1"]), x1)
            else:
                cells.append({"text": text, "x0": x0, "x1": x1})
        out: List[Dict[str, Any]] = []
        for idx, c in enumerate(cells):
            xc = (float(c["x0"]) + float(c["x1"])) / 2.0
            out.append(
                {
                    "text": c["text"],
                    "col_index": idx,
                    "x_center": xc / width,
                    "subheader_text": None,
                    "x0": c["x0"],
                    "x1": c["x1"],
                }
            )
        return out

    clustered = [_cluster_row(rw) for rw in rows]
    best_i = -1
    best_hits = 0
    best_header: List[Dict[str, Any]] = []
    for i, cells in enumerate(clustered[:40]):
        hits, has_closing = _score_header_cells(cells)
        if hits >= 3 and has_closing and hits > best_hits:
            best_hits = hits
            best_i = i
            best_header = cells

    skip_row_indexes: List[int] = []
    absorbed = _absorb_nearby_header_rows(
        best_header, clustered, rows, best_i, med_h
    )
    skip_row_indexes.extend(absorbed)
    if (
        not absorbed
        and best_i >= 0
        and best_i + 1 < len(clustered)
        and _merge_qualifier_subheader(best_header, clustered[best_i + 1])
    ):
        skip_row_indexes.append(best_i + 1)

    group_qty_value = False
    expanded = _expand_group_qty_value_header(
        best_header, clustered, best_i, skip_row_indexes
    )
    if expanded:
        best_header, extra_skips = expanded
        skip_row_indexes.extend(extra_skips)
        group_qty_value = True

    all_token_rows: List[List[Dict[str, Any]]] = []
    for cells in clustered:
        tokens = [
            {
                "text": c["text"],
                "col_index": c["col_index"],
                "x": c["x_center"],
            }
            for c in cells
        ]
        if tokens:
            all_token_rows.append(tokens)

    return {
        "header_cells": best_header,
        "header_row_index": best_i,
        "all_rows": all_token_rows,
        "skip_row_indexes": skip_row_indexes,
        "clustered_headers": clustered,
        "group_qty_value_header": group_qty_value,
    }


def from_pdf_words(
    page_or_pdf: Any,
    *,
    page_width: Optional[float] = None,
) -> Dict[str, Any]:
    """pdfplumber words → header_cells + data row tokens.

    Accepts a single page or a pdf-like object with ``.pages``. Multi-page:
    resolve header on the first page that has one; later pages without a
    header reuse it when column x-centres line up within 0.65 × median pitch.
    A page whose header differs is resolved on its own. A page with no header
    and no match yields STRUCTURAL_ERROR-tagged rows.
    """
    pages: List[Any]
    if hasattr(page_or_pdf, "pages"):
        pages = list(page_or_pdf.pages or [])
    elif isinstance(page_or_pdf, (list, tuple)):
        pages = list(page_or_pdf)
    else:
        pages = [page_or_pdf]

    if not pages:
        return {
            "header_cells": [],
            "data_rows": [],
            "pages_total": 0,
            "pages_resolved": 0,
            "page_results": [],
        }

    carried_header: Optional[List[Dict[str, Any]]] = None
    primary_header: List[Dict[str, Any]] = []
    combined_rows: List[List[Dict[str, Any]]] = []
    page_results: List[Dict[str, Any]] = []
    pages_resolved = 0

    for page_index, page in enumerate(pages):
        extracted = _extract_pdf_page_rows(page, page_width=page_width)
        local_header = extracted.get("header_cells") or []
        local_group_header = bool(extracted.get("group_qty_value_header"))
        header_idx = int(extracted.get("header_row_index") or -1)
        all_rows = list(extracted.get("all_rows") or [])
        data_start = header_idx + 1 if header_idx >= 0 else 0
        skip_rows = {
            int(i) for i in (extracted.get("skip_row_indexes") or [])
        }
        candidate_rows = [
            row
            for i, row in enumerate(all_rows)
            if i not in skip_rows and (header_idx < 0 or i >= data_start)
        ]

        page_header: List[Dict[str, Any]] = []
        resolved = False
        structural = False

        if local_header:
            if carried_header and _headers_align(carried_header, local_header):
                page_header = carried_header
                resolved = True
            elif carried_header and not _headers_align(carried_header, local_header):
                # Different header → resolve on its own.
                page_header = local_header
                carried_header = local_header
                resolved = True
            else:
                page_header = local_header
                carried_header = local_header
                resolved = True
            if not primary_header:
                primary_header = page_header
        elif carried_header:
            # No header on this page — carry over when columns line up.
            probe = next(
                (
                    r
                    for r in candidate_rows
                    if r
                    and not _is_footer_or_total_tokens(r)
                    and not _is_repeated_header_tokens(r, carried_header)
                ),
                None,
            )
            if probe is not None and _tokens_align_to_header(probe, carried_header):
                page_header = carried_header
                resolved = True
            else:
                structural = True
        else:
            structural = True

        page_data: List[List[Dict[str, Any]]] = []
        for tokens in candidate_rows:
            if not tokens:
                continue
            if page_header and _is_repeated_header_tokens(tokens, page_header):
                continue
            if _is_footer_or_total_tokens(tokens):
                continue
            if structural:
                # Tag so the resolver path can mark STRUCTURAL_ERROR.
                tagged = [dict(t) for t in tokens]
                if tagged:
                    tagged[0] = dict(tagged[0])
                    tagged[0]["_structural"] = True
                page_data.append(tagged)
            else:
                page_data.append(tokens)

        if resolved:
            pages_resolved += 1
        combined_rows.extend(page_data)
        page_results.append(
            {
                "page_index": page_index,
                "header_cells": page_header,
                "data_rows": page_data,
                "resolved": resolved,
                "structural": structural,
                "group_qty_value_header": bool(
                    local_group_header
                    or any(
                        isinstance(c, dict) and c.get("_group_qty_value")
                        for c in page_header
                    )
                ),
            }
        )

    return {
        "header_cells": primary_header,
        "data_rows": combined_rows,
        "pages_total": len(pages),
        "pages_resolved": pages_resolved,
        "page_results": page_results,
    }


def from_docx_table(table: Any) -> Dict[str, Any]:
    """DOCX table → header_cells + data rows, honouring gridSpan / vMerge."""
    rows_raw: List[List[Any]] = []
    try:
        for row in table.rows:
            cells_out: List[Any] = []
            seen_tc = set()
            for cell in row.cells:
                # python-docx: merged cells repeat the same _tc for each grid slot.
                # Prefer gridSpan expansion; skip repeats so columns do not double.
                tc_id = id(getattr(cell, "_tc", cell))
                if tc_id in seen_tc:
                    continue
                seen_tc.add(tc_id)
                text = (cell.text or "").replace("\n", " ").strip()
                span = 1
                try:
                    tc = cell._tc
                    tcPr = getattr(tc, "tcPr", None)
                    gridSpan = getattr(tcPr, "gridSpan", None) if tcPr is not None else None
                    if gridSpan is not None and getattr(gridSpan, "val", None):
                        span = int(gridSpan.val)
                except Exception:
                    span = 1
                cells_out.append(text)
                for _ in range(max(span, 1) - 1):
                    cells_out.append("")  # expand span so columns don't shift
            rows_raw.append(cells_out)
    except Exception:
        return {"header_cells": [], "data_rows": []}

    blocks = from_sheet(rows_raw)
    if not blocks:
        return {"header_cells": [], "data_rows": []}
    block = blocks[0]
    # Convert data_rows (list of cell values) to token lists by col_index.
    token_rows: List[List[Dict[str, Any]]] = []
    for row in block.get("data_rows") or []:
        tokens: List[Dict[str, Any]] = []
        for idx, cell in enumerate(row):
            text = _cell_text(cell)
            if text == "":
                continue
            tokens.append({"text": text, "col_index": idx, "x": float(idx)})
        if tokens:
            token_rows.append(tokens)
    return {
        "header_cells": block.get("header_cells") or [],
        "data_rows": token_rows,
        "header_row_index": block.get("header_row_index"),
    }


def sheet_data_rows_to_tokens(
    data_rows: List[List[Any]],
) -> List[List[Dict[str, Any]]]:
    """Convert raw sheet value rows into assign_cells token lists."""
    out: List[List[Dict[str, Any]]] = []
    for row in data_rows:
        tokens: List[Dict[str, Any]] = []
        for idx, cell in enumerate(row if isinstance(row, list) else []):
            # Preserve formula strings (openpyxl cell objects may arrive as dict).
            if isinstance(cell, dict) and "formula" in cell:
                text = cell.get("value")
                tok: Dict[str, Any] = {
                    "text": "" if text is None else str(text),
                    "col_index": idx,
                    "x": float(idx),
                }
                if text is None and cell.get("formula"):
                    tok["formula"] = cell["formula"]
                    tok["text"] = None
                tokens.append(tok)
                continue
            if cell is None or cell == "":
                continue
            tokens.append({"text": str(cell), "col_index": idx, "x": float(idx)})
        if tokens:
            out.append(tokens)
    return out


def resolver_ready(header_cells: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run resolve_columns; return {resolved, columns, errors}."""
    if not header_cells:
        return {"resolved": False, "columns": [], "errors": ["NO_HEADER"]}
    resolved = resolve_columns(header_cells)
    columns = list(resolved.get("columns") or [])
    # Preserve geometry from the builder so assign_cells can use x centres.
    by_idx = {
        int(h.get("col_index") or 0): h
        for h in header_cells
        if isinstance(h, dict)
    }
    for col in columns:
        if not isinstance(col, dict):
            continue
        src = by_idx.get(int(col.get("col_index") or 0))
        if src is not None and src.get("x_center") is not None and col.get("x_center") is None:
            col["x_center"] = src["x_center"]
    errors = resolved.get("errors") or []
    usable = [
        c
        for c in columns
        if c.get("canonical") not in (None, "ignore") and float(c.get("confidence") or 0) > 0
    ]
    has_closing = any(
        str(c.get("canonical") or "").startswith("closing") for c in usable
    )
    ok = len(usable) >= 3 and has_closing and not any(
        str(e.get("code") if isinstance(e, dict) else e) == "STRUCTURAL_ERROR"
        for e in errors
    )
    return {
        "resolved": bool(ok),
        "columns": columns,
        "errors": errors,
        "raw": resolved,
    }
