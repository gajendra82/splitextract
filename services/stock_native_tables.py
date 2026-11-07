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
    decode_pdf_pseudo_digits,
    normalize_header,
    parse_number,
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
        "quant",  # wrapped QUANT(ITY)
        # Second line under Opening/Purchase/Sale/Total/Closing:
        # Stock, Quantity, Return, Receipt, Cost, Value.
        "return",
        "receipt",
        "receive",
        "cost",
        "others",
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
        "quant",
        "value",
        "val",
        "stock",
        "stoc",
        "rate",
        "division",
        "div",
        "description",
        "receive",
        "others",
        "month",
        "last",
        "expiry",
        "purchase",
        "unit",
        "replace",
        "total",
        "return",
    }
) | _QUALIFIER_WORDS

# Mid-word wrap fragments from narrow PDF columns (OPENI/NG, RECEIV/E, QUANT/ITY).
_WRAP_FRAGMENTS = frozenset(
    {
        "ng",
        "g",
        "e",
        "i",
        "ity",
        "quant",
        "stoc",
        "openi",
        "openin",
        "receiv",
        "closi",
        "closin",
    }
)
_HEADER_WRAP_COMPLETIONS = frozenset(
    {
        "opening",
        "closing",
        "receive",
        "received",
        "receipt",
        "quantity",
        "stock",
        "value",
        "issue",
        "sales",
        "purchase",
        "description",
        "product",
        "amount",
    }
)
_QTY_CHILD_WORDS = frozenset({"qty", "quantity", "qnty", "quant"})
_VALUE_CHILD_WORDS = frozenset({"value", "val", "amt", "amount"})


def _cell_is_header_lexicon(text: Any) -> bool:
    tokens = [t for t in normalize_header(text).split() if t]
    return bool(tokens) and all(t in _HEADER_LEXICON for t in tokens)


def _should_glue_header_parts(left: str, right: str) -> bool:
    """True when right continues a wrapped word in left (OPENI+NG, QUANT+ITY)."""
    if not left or not right:
        return False
    if not re.fullmatch(r"[A-Za-z]+", right):
        return False
    last = left.split()[-1]
    if not re.fullmatch(r"[A-Za-z]+", last):
        return False
    compact = (last + right).lower()
    if compact in _HEADER_WRAP_COMPLETIONS:
        return True
    if 1 <= len(right) <= 3 and len(last) >= 4:
        return True
    return False


def _glue_header_text(host: str, word: str, *, prepend: bool) -> str:
    host = str(host or "").strip()
    word = str(word or "").strip()
    if not word:
        return host
    if not host:
        return word
    host_parts = host.lower().split()
    if word.lower() in host_parts or word.lower() == host.lower():
        return host
    left, right = (word, host) if prepend else (host, word)
    if _should_glue_header_parts(left, right):
        return (left + right).strip()
    return (left + " " + right).strip()


def _row_is_alpha_header_child(cells: List[Dict[str, Any]]) -> bool:
    """Second header line of stacked labels (RECEIVE / QUANTITY / OTHERS / STOCK)."""
    if not cells or len(cells) < 2:
        return False
    for cell in cells:
        text = str(cell.get("text") or "").strip()
        if not text or len(text) > 16:
            return False
        if parse_number(text) is not None:
            return False
        if not re.search(r"[A-Za-z]", text):
            return False
    return True


def _row_is_header_wrap_continuation(cells: List[Dict[str, Any]]) -> bool:
    """Continuation of a mid-word wrapped header (NG / STOCK / QUANT / ITY)."""
    if not cells:
        return False
    wrap_hits = 0
    for cell in cells:
        text = normalize_header(cell.get("text"))
        parts = [p for p in text.split() if p]
        if not parts:
            return False
        for part in parts:
            if part in _WRAP_FRAGMENTS:
                wrap_hits += 1
            elif part in _HEADER_LEXICON or part in _QUALIFIER_WORDS:
                continue
            else:
                return False
    return wrap_hits >= 1


_QTY_VALUE_CHILD_EXTRAS = frozenset({"dump", "scrap", "free"})


def _qty_value_child_conflict(prev_text: Any, text: Any) -> bool:
    prev = normalize_header(prev_text)
    nxt = normalize_header(text)
    if (prev in _QTY_CHILD_WORDS and nxt in _VALUE_CHILD_WORDS) or (
        prev in _VALUE_CHILD_WORDS and nxt in _QTY_CHILD_WORDS
    ):
        return True
    # DUMP QTY | SCRAP must stay two columns (gap is often under the cluster threshold).
    if (prev in _QTY_CHILD_WORDS or prev in _VALUE_CHILD_WORDS) and nxt in _QTY_VALUE_CHILD_EXTRAS:
        return True
    if (nxt in _QTY_CHILD_WORDS or nxt in _VALUE_CHILD_WORDS) and prev in _QTY_VALUE_CHILD_EXTRAS:
        return True
    return False


def _place_header_word(
    header: List[Dict[str, Any]], cell: Dict[str, Any], *, prepend: bool
) -> None:
    """Overlap an existing header cell, or insert a column that sits in a gap."""
    x0 = float(cell.get("x0") or 0)
    x1 = float(cell.get("x1") or x0)
    word = str(cell.get("text") or "").strip()
    if not word:
        return
    xc = (x0 + x1) / 2.0
    best: Optional[Dict[str, Any]] = None
    best_dist: Optional[float] = None
    for host in header:
        h0 = float(host.get("x0") or 0)
        h1 = float(host.get("x1") or h0)
        if min(h1, x1) < max(h0, x0) - 2:
            continue
        hc = (h0 + h1) / 2.0
        dist = abs(hc - xc)
        if best_dist is None or dist < best_dist:
            best = host
            best_dist = dist
    if best is not None:
        h0 = float(best.get("x0") or 0)
        h1 = float(best.get("x1") or h0)
        best["text"] = _glue_header_text(
            str(best.get("text") or ""), word, prepend=prepend
        )
        best["x0"] = min(h0, x0)
        best["x1"] = max(h1, x1)
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


_SAME_ROW_QUALIFIERS = frozenset(
    {
        "return",
        "others",
        "receive",
        "receipt",
        "quantity",
        "quant",
        "qty",
        "stock",
        "name",
    }
)
_HEADER_QUALS = frozenset(
    {
        "qty",
        "qnty",
        "quantity",
        "quant",
        "free",
        "val",
        "value",
        "amt",
        "amount",
        "name",
    }
)


def _is_same_row_qualifier(text: Any) -> bool:
    tokens = [t for t in normalize_header(text).split() if t]
    return bool(tokens) and all(t in _SAME_ROW_QUALIFIERS for t in tokens)


def _unglue_header_enabled() -> bool:
    """STOCK_PDF_UNGLUE_HEADER_TOKENS — split QtyLM / FreeSales / QtyCl.Stock.

    Default ON. Custom-encoded PDFs omit the space between adjacent headers.
    """
    return os.getenv("STOCK_PDF_UNGLUE_HEADER_TOKENS", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _right_align_qty_enabled() -> bool:
    """STOCK_PDF_RIGHT_ALIGN_QTY — right-aligned qtys under left-aligned headers.

    Default ON. Kill switch STOCK_PDF_RIGHT_ALIGN_QTY=false.
    A qty printed at the right edge of its column sits closer to the next
    header centre. Snap it back when it is still left of that next header.
    Also keeps a following expiry date, and a pack size, as their own cells.
    """
    return os.getenv("STOCK_PDF_RIGHT_ALIGN_QTY", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _looks_like_unit_pack(text: str) -> bool:
    from services.stock_header_resolver import _unit_pack_token

    token = str(text or "").replace("`", "'").replace("’", "'").replace("´", "'")
    return _unit_pack_token(token)


_EXPIRY_DATE_TOKEN_RE = re.compile(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$")


def _snap_right_aligned_qty_tokens(
    header_cells: List[Dict[str, Any]],
    rows: List[List[Dict[str, Any]]],
) -> int:
    """Move a right-aligned qty onto its header centre.

    Owner is the header whose text starts at or before the number's right
    edge and before the next header. Snap only when that number sits past
    the header glyph and nearest-centre would pick a different column.
    """
    if not _right_align_qty_enabled():
        return 0
    spans = [
        h
        for h in header_cells or []
        if isinstance(h, dict)
        and h.get("x0") is not None
        and h.get("x_center") is not None
    ]
    spans.sort(key=lambda h: float(h["x0"]))
    if len(spans) < 3:
        return 0
    snapped = 0
    for row in rows or []:
        for tok in row:
            if not isinstance(tok, dict):
                continue
            raw = str(tok.get("text") or "").strip()
            # Plain qtys only. Parenthetical codes such as (078) are not amounts.
            if not re.fullmatch(r"-?\d+(?:\.\d+)?", raw.replace(",", "")):
                continue
            if parse_number(raw) is None:
                continue
            if tok.get("x0") is None or tok.get("x1") is None:
                continue
            right = float(tok["x1"])
            center = (float(tok["x0"]) + right) / 2.0
            owner = None
            for i, header in enumerate(spans):
                nxt = (
                    float(spans[i + 1]["x0"]) if i + 1 < len(spans) else 1e12
                )
                if float(header["x0"]) - 1.0 <= right < nxt - 0.5:
                    owner = header
                    break
            if owner is None:
                continue
            owner_right = float(owner.get("x1") or owner["x0"])
            if center <= owner_right + 2.0:
                continue
            nearest = min(
                spans,
                key=lambda h: abs(
                    (
                        (float(h["x0"]) + float(h.get("x1") or h["x0"]))
                        / 2.0
                    )
                    - center
                ),
            )
            if int(nearest.get("col_index") or -1) == int(
                owner.get("col_index") or -2
            ):
                continue
            tok["x"] = float(owner["x_center"])
            snapped += 1
            logger.info(
                "STOCK_PDF_RIGHT_ALIGN_QTY token=%s header=%s",
                raw,
                owner.get("text"),
            )
    if snapped:
        logger.info("STOCK_PDF_RIGHT_ALIGN_QTY snapped=%s", snapped)
    return snapped


def _split_qty_pack_cluster_enabled() -> bool:
    """STOCK_PDF_SPLIT_QTY_PACK_CLUSTER — keep Cl.Stock qty off Box Pack.

    Default ON. Kill switch STOCK_PDF_SPLIT_QTY_PACK_CLUSTER=false.
    Right-aligned closing qty sits a few px left of Box Pack (100x60'S /
    1x30 ML); clustering them as one token parks the qty in the pack band.
    """
    return os.getenv("STOCK_PDF_SPLIT_QTY_PACK_CLUSTER", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# Box-pack cells start with a digit and contain a letter (100x60'S / 1x30 / 56x100ml).
_BOX_PACK_TOKEN_RE = re.compile(r"(?i)^\d[\w.'/-]*[A-Za-z][\w.'/-]*$")


def _pure_qty_token(text: str) -> bool:
    return parse_number(text) is not None and not re.search(r"[A-Za-z]", text or "")


def _is_box_pack_token(text: str) -> bool:
    token = str(text or "").strip()
    if not token:
        return False
    if _BOX_PACK_TOKEN_RE.match(token):
        return True
    from services.stock_header_resolver import _unit_pack_token

    return bool(_unit_pack_token(token))


def _qty_and_pack_cluster_split(prev_text: str, text: str) -> bool:
    """True when a qty cell and a box-pack cell must not merge."""
    if not _split_qty_pack_cluster_enabled():
        return False
    if _pure_qty_token(prev_text) and _is_box_pack_token(text):
        return True
    if _pure_qty_token(text) and _is_box_pack_token(prev_text):
        return True
    return False


def _header_fragment_kind(text: str) -> Optional[str]:
    """'qual' for Qty/Free/Val, 'primary' for a movement/name alias, else None."""
    norm = normalize_header(text)
    compact = re.sub(r"\s+", "", norm).rstrip(".")
    if not compact:
        return None
    if compact in _HEADER_QUALS:
        return "qual"
    if not _alias_hit(text):
        return None
    from services.stock_header_resolver import _pick_group

    group, conf, _reason = _pick_group(norm)
    if conf <= 0 or group is None:
        return None
    if group in {"free", "value_marker"}:
        return "qual"
    return "primary"


def _exact_header_alias(text: str) -> bool:
    """True when the whole token is already one printed header alias."""
    if _header_fragment_kind(text) != "primary":
        return False
    norm = normalize_header(text)
    compact = re.sub(r"\s+", "", norm)
    from services.stock_header_resolver import _direct_value_field

    if _direct_value_field(norm):
        return True
    for aliases in ALIASES.values():
        for alias in aliases:
            a_norm = normalize_header(alias)
            if not a_norm:
                continue
            if a_norm == norm or re.sub(r"\s+", "", a_norm) == compact:
                return True
    return False


def _split_glued_header_text(text: str) -> Optional[Tuple[str, str]]:
    """Split QtyLM / FreeSales / QtyCl.Stock into two header fragments."""
    if not text or " " in text or "|" in text:
        return None
    if parse_number(text) is not None:
        return None
    if _exact_header_alias(text):
        return None
    best: Optional[Tuple[int, str, str]] = None
    for i in range(1, len(text)):
        left, right = text[:i], text[i:]
        lk = _header_fragment_kind(left)
        rk = _header_fragment_kind(right)
        if not lk or not rk:
            continue
        score = len(right)
        if lk == "qual" and rk == "primary":
            score += 100
        elif lk == "primary" and rk == "primary":
            score += 60
        elif lk == "primary" and rk == "qual":
            score += 40
        else:
            score += 10
        if _exact_header_alias(right):
            score += 50
        if best is None or score > best[0]:
            best = (score, left, right)
    if not best:
        return None
    return best[1], best[2]


_UNGLUE_SEQ = 0


def _unglue_header_parts(text: str, x0: float, x1: float) -> List[Dict[str, Any]]:
    """Yield one or two word boxes after splitting a glued header token."""
    base = {"text": text, "x0": x0, "x1": x1, "pipe_split": False}
    if not _unglue_header_enabled():
        return [base]
    split = _split_glued_header_text(text)
    if not split:
        return [base]
    left, right = split
    span = max(x1 - x0, 1.0)
    frac = max(0.15, min(0.85, len(left) / max(len(text), 1)))
    mid = x0 + span * frac
    logger.info(
        "STOCK_PDF_HEADER_UNGLUE token=%r left=%r right=%r", text, left, right
    )
    global _UNGLUE_SEQ
    _UNGLUE_SEQ += 1
    uid = _UNGLUE_SEQ
    out: List[Dict[str, Any]] = []
    for part, px0, px1 in ((left, x0, mid), (right, mid, x1)):
        nested = _split_glued_header_text(part)
        if nested:
            out.extend(_unglue_header_parts(part, px0, px1))
        else:
            out.append(
                {
                    "text": part,
                    "x0": px0,
                    "x1": px1,
                    "pipe_split": False,
                    "unglue_id": uid,
                }
            )
    return out


def _glue_adjacent_qualifier_cells(cells: List[Dict[str, Any]]) -> None:
    """Join SALE+RETURN / REPLACE++OTHERS that sat just apart in the header row.

    Does not glue RATE/AMT/VALUE — those are their own columns.
    """
    i = 1
    while i < len(cells):
        right = cells[i]
        if not _is_same_row_qualifier(right.get("text")):
            i += 1
            continue
        left = cells[i - 1]
        gap = float(right.get("x0") or 0) - float(left.get("x1") or 0)
        left_w = max(1.0, float(left.get("x1") or 0) - float(left.get("x0") or 0))
        if gap > max(14.0, 0.55 * left_w):
            i += 1
            continue
        width = None
        xc = left.get("x_center")
        mid = (float(left.get("x0") or 0) + float(left.get("x1") or 0)) / 2.0
        if xc and mid:
            width = mid / float(xc)
        left["text"] = _glue_header_text(
            str(left.get("text") or ""),
            str(right.get("text") or ""),
            prepend=False,
        )
        left["x1"] = max(float(left.get("x1") or 0), float(right.get("x1") or 0))
        new_mid = (float(left["x0"]) + float(left["x1"])) / 2.0
        if width:
            left["x_center"] = new_mid / width
        cells.pop(i)
        for idx, cell in enumerate(cells):
            cell["col_index"] = idx


def _wrap_header_enabled() -> bool:
    """STOCK_PDF_WORDS_WRAP_HEADER — absorb OPENI/NG/STOCK stacked labels.

    Default ON. Set false to keep only the original ±1 lexicon absorb.
    """
    return os.getenv("STOCK_PDF_WORDS_WRAP_HEADER", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _split_column_pages_enabled() -> bool:
    """STOCK_PDF_SPLIT_COLUMN_PAGES — stitch left-name + right-qty page halves.

    Default ON. Wide STOCK & SALES sheets print product/opening on one page
    and sales/closing on the next; concatenating those pages duplicates rows.
    """
    return os.getenv("STOCK_PDF_SPLIT_COLUMN_PAGES", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _footer_totals_enabled() -> bool:
    """STOCK_PDF_WORDS_FOOTER_TOTALS — keep printed TOTAL QUANTITY/VALUE rows.

    Default ON. Sheet footers are skipped as products; the numbers are copied
    into statement totals. Never invented from row arithmetic.
    """
    return os.getenv("STOCK_PDF_WORDS_FOOTER_TOTALS", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _sheet_total_row_kind(tokens: List[Dict[str, Any]]) -> Optional[str]:
    """'quantity' or 'value' when the row is a printed sheet TOTAL footer."""
    texts = [str(t.get("text") or "").strip() for t in tokens if isinstance(t, dict)]
    texts = [t for t in texts if t]
    if not texts:
        return None
    joined = " ".join(texts)
    if is_software_price_ad_name(joined):
        return None
    numeric = sum(1 for t in texts if parse_number(t) is not None)
    if numeric < 3:
        return None
    if re.search(
        r"(?i)(?:^|\b)(?:grand\s+|page\s+|sub\s+|company\s+|division\s+)?"
        r"total\s+(?:quantity|qty|qnt)\b",
        joined,
    ):
        return "quantity"
    # "Total For: All Items" — qty under the movement columns, money under value.
    if re.search(r"(?i)total\s+for\b", joined):
        return "quantity"
    if re.search(
        r"(?i)(?:^|\b)(?:grand\s+|page\s+|sub\s+|company\s+|division\s+)?"
        r"total\s+value\b",
        joined,
    ):
        return "value"
    if re.match(r"(?i)^(?:grand\s+|page\s+|sub\s+)?total$", texts[0]):
        second = texts[1] if len(texts) > 1 else ""
        if re.match(r"(?i)^(quantity|qty|qnt)$", second):
            return "quantity"
        if re.match(r"(?i)^value$", second):
            return "value"
    return None


def _header_has_group(header_cells: List[Dict[str, Any]], group_name: str) -> bool:
    from services.stock_header_resolver import _pick_group

    for cell in header_cells or []:
        text = str(cell.get("text") or "")
        sub = str(cell.get("subheader_text") or "")
        combined = f"{text} {sub}".strip()
        group, _conf, _reason = _pick_group(normalize_header(combined or text))
        if group == group_name:
            return True
    return False


def _page_header_canons(header_cells: List[Dict[str, Any]]) -> set:
    resolved = resolve_columns(header_cells or [])
    return {
        str(c.get("canonical") or "")
        for c in (resolved.get("columns") or [])
        if isinstance(c, dict) and c.get("canonical") not in (None, "ignore")
    }


def _is_left_split_header(canons: set) -> bool:
    return (
        "product_name" in canons
        and "opening_qty" in canons
        and "closing_qty" not in canons
    )


def _is_right_split_header(canons: set) -> bool:
    return (
        "closing_qty" in canons
        and "product_name" not in canons
        and ("sales_qty" in canons or "total_qty" in canons)
    )


def _split_page_product_rows(
    rows: List[List[Dict[str, Any]]], *, named: bool
) -> List[List[Dict[str, Any]]]:
    out: List[List[Dict[str, Any]]] = []
    keep_totals = _footer_totals_enabled()
    for row in rows or []:
        if not row:
            continue
        kind = _sheet_total_row_kind(row) if keep_totals else None
        if _is_footer_or_total_tokens(row) and not kind:
            continue
        texts = [str(t.get("text") or "") for t in row if isinstance(t, dict)]
        texts = [t for t in texts if t]
        has_num = any(
            parse_number(t) is not None
            or re.match(r"^-?\d[\d,]*(?:\.\d+)?\b", t)
            for t in texts
        )
        blob = " ".join(texts)
        has_name = bool(re.search(r"[A-Za-z]{3,}", blob))
        if named:
            if has_num and has_name:
                out.append(row)
        elif has_num and not has_name:
            out.append(row)
        elif has_num and has_name:
            # Right halves often print manufacturer/division after qty/value.
            first = texts[0]
            if parse_number(first) is not None or re.match(
                r"^-?\d[\d,]*(?:\.\d+)?\b", first
            ):
                out.append(row)
    return out


def _merge_split_headers(
    left: List[Dict[str, Any]], right: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for cell in left or []:
        entry = dict(cell)
        entry["col_index"] = len(merged)
        merged.append(entry)
    for cell in right or []:
        entry = dict(cell)
        entry["col_index"] = len(merged)
        if entry.get("x_center") is not None:
            entry["x_center"] = float(entry["x_center"]) + 1.0
        merged.append(entry)
    return merged


def _peel_glued_amount_manufacturer(
    token: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Split '1087.52 THE HIMALAYA…' into amount + trailing manufacturer text."""
    text = str(token.get("text") or "").strip()
    match = re.match(r"^(-?\d[\d,]*(?:\.\d+)?)\s+([A-Za-z].+)$", text)
    if not match:
        return [token]
    amount = dict(token)
    amount["text"] = match.group(1)
    trail = dict(token)
    trail["text"] = match.group(2)
    if token.get("x") is not None:
        # Clustered amount+manufacturer x sits near Manufacturer; pull the
        # amount left toward STOCKV and keep manufacturer on the right.
        x = float(token["x"])
        amount["x"] = x - 0.15
        trail["x"] = x + 0.02
    logger.info(
        "STOCK_PDF_SPLIT_COLUMN_PAGES peel_glued_amount text=%r",
        text[:60],
    )
    return [amount, trail]


def _merge_split_row(
    left: List[Dict[str, Any]], right: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    out = [dict(t) for t in left if isinstance(t, dict)]
    for token in right or []:
        if not isinstance(token, dict):
            continue
        tok = dict(token)
        if tok.get("x") is not None:
            tok["x"] = float(tok["x"]) + 1.0
        for piece in _peel_glued_amount_manufacturer(tok):
            out.append(piece)
    return out


def _try_merge_split_page_pair(
    left_page: Dict[str, Any], right_page: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Merge one product/opening page with one sales/closing-only page."""
    left_header = (left_page or {}).get("header_cells") or []
    right_header = (right_page or {}).get("header_cells") or []
    if not (
        left_header
        and right_header
        and _is_left_split_header(_page_header_canons(left_header))
        and _is_right_split_header(_page_header_canons(right_header))
    ):
        return None
    left_rows = _split_page_product_rows(
        left_page.get("data_rows") or [], named=True
    )
    right_rows = _split_page_product_rows(
        right_page.get("data_rows") or [], named=False
    )
    n_left = len(left_rows)
    n_right = len(right_rows)
    slack = max(2, int(0.15 * max(n_left, n_right, 1)))
    if not (n_left >= 3 and n_right >= 3 and abs(n_left - n_right) <= slack):
        return None
    n = min(n_left, n_right)
    merged_rows = [
        _merge_split_row(left_rows[i], right_rows[i]) for i in range(n)
    ]
    return {
        "page_index": left_page.get("page_index"),
        "header_cells": _merge_split_headers(left_header, right_header),
        "data_rows": merged_rows,
        "resolved": True,
        "structural": False,
        "group_qty_value_header": bool(
            left_page.get("group_qty_value_header")
            or right_page.get("group_qty_value_header")
        ),
        "split_column_pages": [
            left_page.get("page_index"),
            right_page.get("page_index"),
        ],
    }


def _stitch_split_column_blocks(
    page_results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Zip left-half pages with right-half pages when printed as L+ then R+.

    Wide STOCK & SALES sheets sometimes emit every product/opening page first,
    then every purchase/sales/closing page. Adjacent L/R pairing misses that.
    """
    if not _split_column_pages_enabled() or len(page_results) < 4:
        return page_results
    if any(
        isinstance(p, dict) and p.get("split_column_pages") for p in page_results
    ):
        return page_results

    kinds: List[str] = []
    for page in page_results:
        canons = _page_header_canons((page or {}).get("header_cells") or [])
        if _is_left_split_header(canons):
            kinds.append("L")
        elif _is_right_split_header(canons):
            kinds.append("R")
        else:
            kinds.append("O")

    try:
        first_l = kinds.index("L")
        first_r = kinds.index("R")
    except ValueError:
        return page_results
    if first_r <= first_l:
        return page_results
    if any(k != "L" for k in kinds[first_l:first_r]):
        return page_results
    last_r = first_r
    while last_r + 1 < len(kinds) and kinds[last_r + 1] == "R":
        last_r += 1
    left_pages = page_results[first_l:first_r]
    right_pages = page_results[first_r : last_r + 1]
    if len(left_pages) < 2 or len(left_pages) != len(right_pages):
        return page_results

    merged_pages: List[Dict[str, Any]] = []
    for left_page, right_page in zip(left_pages, right_pages):
        merged = _try_merge_split_page_pair(left_page, right_page)
        if merged is None:
            return page_results
        merged_pages.append(merged)
        logger.info(
            "STOCK_PDF_SPLIT_COLUMN_PAGES block left=%s right=%s rows=%s",
            left_page.get("page_index"),
            right_page.get("page_index"),
            len(merged.get("data_rows") or []),
        )
    return (
        list(page_results[:first_l])
        + merged_pages
        + list(page_results[last_r + 1 :])
    )


def _stitch_split_column_pages(
    page_results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Zip a product/opening page with the following sales/closing-only page."""
    if not _split_column_pages_enabled() or len(page_results) < 2:
        return page_results
    out: List[Dict[str, Any]] = []
    index = 0
    while index < len(page_results):
        current = page_results[index]
        nxt = page_results[index + 1] if index + 1 < len(page_results) else None
        merged = (
            _try_merge_split_page_pair(current, nxt) if nxt is not None else None
        )
        if merged is not None:
            out.append(merged)
            logger.info(
                "STOCK_PDF_SPLIT_COLUMN_PAGES left=%s right=%s rows=%s",
                current.get("page_index"),
                nxt.get("page_index"),
                len(merged.get("data_rows") or []),
            )
            index += 2
            continue
        out.append(current)
        index += 1
    return _stitch_split_column_blocks(out)


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
    Wrapped OPENI/NG/STOCK stacks are absorbed when STOCK_PDF_WORDS_WRAP_HEADER
    is on (default).
    """
    if best_i < 0 or not header or best_i >= len(rows):
        return []
    header_top = float((rows[best_i][0] or {}).get("top") or 0)
    limit = max(8.0, 1.25 * float(med_h or 8.0))
    wrap_limit = max(limit, 2.75 * float(med_h or 8.0))
    wrap_on = _wrap_header_enabled()
    consumed: List[int] = []
    candidates: List[Tuple[int, int, List[Dict[str, Any]]]] = []
    scan_lo = best_i - (2 if wrap_on else 1)
    scan_hi = best_i + (2 if wrap_on else 1)
    lex_limit = max(limit, 2.0 * float(med_h or 8.0))
    for idx in range(scan_lo, scan_hi + 1):
        if idx == best_i:
            continue
        if idx < 0 or idx >= len(clustered) or idx >= len(rows) or not rows[idx]:
            continue
        top = float((rows[idx][0] or {}).get("top") or 0)
        dist = abs(top - header_top)
        cells = clustered[idx]
        if not cells:
            continue
        # Qty|Value under OPENING/RECEIPT/ISSUE must stay separate columns.
        # Absorbing them glues "OPENING QTY. VALUE" into one cell so pairing
        # never runs and qty numbers land in value fields.
        if (
            _group_qty_value_header_enabled()
            and _row_is_group_labels(header)
            and _row_is_qty_value_children(cells)
        ):
            logger.info(
                "STOCK_PDF_GROUP_QTY_VALUE skip_absorb row=%s cols=%s",
                idx,
                [c.get("text") for c in cells],
            )
            continue
        is_lex = all(_cell_is_header_lexicon(c.get("text")) for c in cells)
        is_wrap = wrap_on and _row_is_header_wrap_continuation(cells)
        is_alpha_child = abs(idx - best_i) == 1 and _row_is_alpha_header_child(cells)
        if is_lex and dist <= lex_limit and abs(idx - best_i) == 1:
            candidates.append((abs(idx - best_i), idx, cells))
        elif is_wrap and dist <= wrap_limit:
            candidates.append((abs(idx - best_i), idx, cells))
        elif is_alpha_child and dist <= lex_limit:
            candidates.append((abs(idx - best_i), idx, cells))
    candidates.sort(key=lambda item: (item[0], item[1]))
    for _rank, idx, cells in candidates:
        for cell in cells:
            _place_header_word(header, cell, prepend=idx < best_i)
        consumed.append(idx)
        logger.info(
            "STOCK_PDF_WORDS_WRAP_HEADER row=%s cols=%s",
            idx,
            [c.get("text") for c in cells],
        )
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


# Billing-software ads printed under the grid (ERP package + price / chemist slogan).
# Never a SKU. Generic: does not key off a stockist or filename.
_SOFTWARE_PRICE_AD_NAME = re.compile(
    r"(?ix)"
    r"\bERP\b.{0,80}(?:Rs\.?\s*\d{3,}|\bfor\s+Chemist\b)"
    r"|(?:Rs\.?\s*\d{3,}|\bfor\s+Chemist\b).{0,80}\bERP\b"
)


def is_software_price_ad_name(name: Any) -> bool:
    """True for ERP/software price-tag rows that must not become line items."""
    text = str(name or "").strip()
    if not text:
        return False
    return bool(_SOFTWARE_PRICE_AD_NAME.search(text))


def _looks_like_legend_or_footer_header(cells: List[Dict[str, Any]]) -> bool:
    """True for DSTK legends / PURC: n SALE: n summaries that must not be headers."""
    texts = [
        str(c.get("text") or "").strip()
        for c in (cells or [])
        if isinstance(c, dict)
    ]
    texts = [t for t in texts if t]
    if not texts:
        return False
    blob = " ".join(texts)
    if re.search(
        r"Prepared\s+by|Computer\s+Generated|Does\s+Not\s+Require\s+Stamp|"
        r"Days\s+Stock\s+on|No\s+Sale\s+No\s+Stock|\bDSTK\b",
        blob,
        re.I,
    ):
        return True
    # Sheet summary strip: "PURC : 331488 SALE : 316204 STOCK : 486430".
    if len(re.findall(r"[A-Za-z]{2,}\s*:\s*-?\d", blob)) >= 2:
        return True
    return False


def _is_footer_or_total_tokens(tokens: List[Dict[str, Any]]) -> bool:
    texts = [str(t.get("text") or "").strip() for t in tokens if isinstance(t, dict)]
    if not texts:
        return True
    joined = " ".join(texts)
    if is_software_price_ad_name(joined):
        logger.info("STOCK_SOFTWARE_AD_SKIP blob=%s", joined[:80])
        return True
    if _looks_like_legend_or_footer_header(
        [{"text": t} for t in texts]
    ):
        return True
    # Company / page / grand footers only. Product "Total :" rows with movement
    # numbers must stay so multi-lot sheets can fold printed totals.
    if re.search(
        r"GRAND\s+TOTAL|PAGE\s*TOTAL|COMPANY\s*Total|Firm\s*Total|CONTINUED|Page\s*No",
        joined,
        re.I,
    ):
        return True
    from services.stock_header_resolver import parse_number

    numeric = sum(1 for t in texts if parse_number(t) is not None)
    first = texts[0]
    # Bare TOTAL / GRAND TOTAL is a sheet footer even when qty columns are filled.
    # Product lot totals print "Total :" (colon) and must remain.
    if re.match(r"^(?:GRAND\s+)?TOTAL\b", first, re.I) and ":" not in first:
        logger.info("STOCK_PDF_WORDS_TOTAL_SKIP footer blob=%s", joined[:60])
        return True
    if re.match(r"^(?:GRAND\s+)?TOTAL\b", first, re.I) and numeric < 3:
        return True
    alpha = next((t for t in texts if re.search(r"[A-Za-z]", t)), "")
    if (
        re.match(r"^(?:GRAND\s+|PAGE\s+|SUB\s+)?TOTAL\b", alpha, re.I)
        and ":" not in alpha
    ):
        logger.info("STOCK_PDF_WORDS_TOTAL_SKIP footer blob=%s", joined[:60])
        return True
    if (
        re.match(r"^(?:GRAND\s+|PAGE\s+|SUB\s+)?TOTAL\b", alpha, re.I)
        and numeric < 3
    ):
        return True
    return False


def _is_repeated_header_tokens(
    tokens: List[Dict[str, Any]], header_cells: List[Dict[str, Any]]
) -> bool:
    """True when a data row looks like a reprinted column header."""
    if not tokens or not header_cells:
        return False
    # A printed amount means this is a value footer, not a reprinted header.
    if any(parse_number(t.get("text")) is not None for t in tokens):
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
    {"qty", "quantity", "qnty", "quant", "value", "val", "amt", "amount"}
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

    norm = normalize_header(text)
    # DUMP/SCRAP sit above a QTY child and must cover that column, even when
    # the non_stock_qty alias table is flagged off.
    if norm in {"dump", "scrap", "dump qty", "scrap qty"}:
        return "non_stock_qty"
    group, _conf, _reason = _pick_group(norm)
    if group in _GROUP_LABELS or group == "non_stock_qty":
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
    """Product/Rate plus repeated Qty/value, with no data numbers.

    Trailing DUMP/SCRAP labels sit on the child row of OPENING/RECEIPT/ISSUE
    grids and are not movement groups. A glued "QTY. SCRAP" cell still counts.
    """
    if not cells or len(cells) < 6:
        return False
    qty_value = 0
    for cell in cells:
        text = cell.get("text")
        if parse_number(text) is not None:
            return False
        tokens = [t for t in normalize_header(text).split() if t]
        if not tokens:
            return False
        if any(t in _QTY_VALUE_CHILD_WORDS for t in tokens):
            qty_value += 1
            leftover = [t for t in tokens if t not in _QTY_VALUE_CHILD_WORDS]
            if leftover and not all(
                t in _QTY_VALUE_CHILD_EXTRAS or t in _HEADER_LEXICON for t in leftover
            ):
                return False
            continue
        if all(t in _HEADER_LEXICON or t in _QTY_VALUE_CHILD_EXTRAS for t in tokens):
            continue
        return False
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
            "item description",
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
    first_group_x0 = min(float(p.get("x0") or 0) for p in group_parents)
    paired: List[Dict[str, Any]] = []
    # ITEM DESCRIPTION / RATE / PACK sit on the parent row, not the QTY/VALUE row.
    for parent in sorted(parents, key=lambda c: float(c.get("x0") or 0)):
        if _header_group_name(parent.get("text")):
            continue
        if float(parent.get("x0") or 0) >= first_group_x0 - 2:
            continue
        pc = (float(parent.get("x0") or 0) + float(parent.get("x1") or 0)) / 2.0
        if any(
            abs(
                (
                    (float(c.get("x0") or 0) + float(c.get("x1") or 0)) / 2.0
                )
                - pc
            )
            < 12
            for c in children
        ):
            continue
        entry = dict(parent)
        entry["subheader_text"] = None
        entry["_group_qty_value"] = True
        paired.append(entry)
    for cell in sorted(children, key=lambda c: float(c.get("x0") or 0)):
        entry = dict(cell)
        tokens = [t for t in normalize_header(cell.get("text")).split() if t]
        qty_value_tok = next((t for t in tokens if t in _QTY_VALUE_CHILD_WORDS), None)
        if qty_value_tok:
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
        # Expand pipe-ruled fragments so "1884.14|05/2029|" becomes two cells.
        _num_or_date = re.compile(r"^[\d.,/\-]+$")
        expanded: List[Dict[str, Any]] = []
        for w in row_words:
            text = decode_pdf_pseudo_digits(str(w.get("text") or "")).strip()
            if not text:
                continue
            x0 = float(w.get("x0") or 0)
            x1 = float(w.get("x1") or x0)
            if "|" not in text:
                expanded.extend(_unglue_header_parts(text, x0, x1))
                continue
            parts = [p.strip() for p in text.split("|")]
            parts = [p for p in parts if p]
            if not parts:
                continue
            span = max(x1 - x0, 1.0)
            for i, part in enumerate(parts):
                left = x0 + span * i / len(parts)
                right = x0 + span * (i + 1) / len(parts)
                expanded.append(
                    {
                        "text": part,
                        "x0": left,
                        "x1": right,
                        "pipe_split": True,
                    }
                )
        row_words = expanded
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
            pipe_split = bool(w.get("pipe_split"))
            if not text or text == "|":
                continue
            uid = w.get("unglue_id")
            if cells and x0 - float(cells[-1]["x1"]) <= gap:
                prev_text = cells[-1]["text"]
                # Split QtyLM/FreeSales pieces share a boundary; do not rejoin them.
                if uid and cells[-1].get("unglue_id") == uid:
                    cells.append(
                        {
                            "text": text,
                            "x0": x0,
                            "x1": x1,
                            "pipe_split": pipe_split,
                            "unglue_id": uid,
                        }
                    )
                    continue
                # Keep distinct header aliases apart (Op.Value vs Expiry).
                # SALE+RETURN stays one column: RETURN is a qualifier of SALE.
                if _alias_hit(prev_text) and _alias_hit(text):
                    from services.stock_header_resolver import (
                        _direct_value_field,
                        _pick_group,
                    )

                    g_prev, _, _ = _pick_group(normalize_header(prev_text))
                    g_new, _, _ = _pick_group(normalize_header(text))
                    combined_value = _direct_value_field(
                        normalize_header(f"{prev_text} {text}")
                    )
                    if combined_value:
                        logger.info(
                            "STOCK_PDF_HEADER_VALUE_LABEL glued %r + %r -> %s",
                            prev_text,
                            text,
                            combined_value,
                        )
                    if (
                        g_prev
                        and g_new
                        and g_prev != g_new
                        and not _is_same_row_qualifier(text)
                        and not combined_value
                    ):
                        cells.append(
                            {
                                "text": text,
                                "x0": x0,
                                "x1": x1,
                                "pipe_split": pipe_split,
                            }
                        )
                        continue
                # Keep QUANT vs VALUE (and qty vs amount) as separate columns.
                if _qty_value_child_conflict(prev_text, text):
                    cells.append(
                        {
                            "text": text,
                            "x0": x0,
                            "x1": x1,
                            "pipe_split": pipe_split,
                        }
                    )
                    continue
                # Qty and its neighbouring value are two cells, not "51 10689.52".
                if (
                    parse_number(prev_text) is not None
                    and parse_number(text) is not None
                ):
                    cells.append(
                        {
                            "text": text,
                            "x0": x0,
                            "x1": x1,
                            "pipe_split": pipe_split,
                        }
                    )
                    continue
                # Closing qty and the expiry date beside it are two cells.
                if (
                    _right_align_qty_enabled()
                    and parse_number(prev_text) is not None
                    and _EXPIRY_DATE_TOKEN_RE.fullmatch(text)
                ):
                    logger.info(
                        "STOCK_PDF_RIGHT_ALIGN_QTY split_date left=%r right=%r",
                        prev_text,
                        text,
                    )
                    cells.append(
                        {
                            "text": text,
                            "x0": x0,
                            "x1": x1,
                            "pipe_split": pipe_split,
                        }
                    )
                    continue
                # Pack size after the product name is the packing column.
                if (
                    _right_align_qty_enabled()
                    and _looks_like_unit_pack(text)
                    and re.search(r"[A-Za-z]", prev_text)
                    and not _looks_like_unit_pack(prev_text)
                ):
                    logger.info(
                        "STOCK_PDF_RIGHT_ALIGN_QTY split_pack left=%r right=%r",
                        prev_text,
                        text,
                    )
                    cells.append(
                        {
                            "text": text,
                            "x0": x0,
                            "x1": x1,
                            "pipe_split": pipe_split,
                        }
                    )
                    continue
                # Qty and neighbouring box-pack are two cells, not "89 100x60'S".
                if _qty_and_pack_cluster_split(prev_text, text):
                    logger.info(
                        "STOCK_PDF_SPLIT_QTY_PACK_CLUSTER left=%r right=%r",
                        prev_text,
                        text,
                    )
                    cells.append(
                        {
                            "text": text,
                            "x0": x0,
                            "x1": x1,
                            "pipe_split": pipe_split,
                        }
                    )
                    continue
                # Do not re-glue value|expiry fragments from a pipe-ruled cell.
                if cells[-1].get("pipe_split") or pipe_split:
                    if _num_or_date.match(prev_text) and _num_or_date.match(text):
                        cells.append(
                            {
                                "text": text,
                                "x0": x0,
                                "x1": x1,
                                "pipe_split": pipe_split,
                            }
                        )
                        continue
                cells[-1]["text"] = (prev_text + " " + text).strip()
                cells[-1]["x1"] = max(float(cells[-1]["x1"]), x1)
                cells[-1]["pipe_split"] = cells[-1].get("pipe_split") or pipe_split
            else:
                cells.append(
                    {
                        "text": text,
                        "x0": x0,
                        "x1": x1,
                        "pipe_split": pipe_split,
                        "unglue_id": uid,
                    }
                )
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

    def _looks_like_value_summary_header(cells: List[Dict[str, Any]]) -> bool:
        """Footer 'Value-> OB :… Purchase :…' must not beat the real column header."""
        blob = " ".join(str(c.get("text") or "") for c in cells)
        if re.search(r"Value\s*->|Qnt\s*->", blob, re.I):
            return True
        colon_amts = sum(
            1
            for c in cells
            if re.match(r"^:\s*\d", str(c.get("text") or "").strip())
        )
        return colon_amts >= 2

    clustered = [_cluster_row(rw) for rw in rows]
    best_i = -1
    best_hits = 0
    best_header: List[Dict[str, Any]] = []
    # Scan the whole page — continuation pages put the only false "header"
    # (Value-> OB/Purchase/Sale) after the product rows.
    for i, cells in enumerate(clustered):
        if _looks_like_value_summary_header(cells):
            continue
        if _looks_like_legend_or_footer_header(cells):
            continue
        hits, has_closing = _score_header_cells(cells)
        left_half = (
            _split_column_pages_enabled()
            and hits >= 3
            and _header_has_group(cells, "product_name")
            and _header_has_group(cells, "opening")
        )
        if hits >= 3 and (has_closing or left_half) and hits > best_hits:
            best_hits = hits
            best_i = i
            best_header = cells

    if best_header:
        _glue_adjacent_qualifier_cells(best_header)

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
                "x0": c.get("x0"),
                "x1": c.get("x1"),
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

        if local_header and carried_header and _looks_like_legend_or_footer_header(
            local_header
        ):
            # DSTK / PURC:n SALE:n legends must not replace a carried stock header.
            local_header = []
            logger.info(
                "STOCK_PDF_WORDS_SKIP_LEGEND_HEADER page=%s",
                page_index,
            )

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
                if not (
                    _footer_totals_enabled() and _sheet_total_row_kind(tokens)
                ):
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

        if page_header and page_data:
            _snap_right_aligned_qty_tokens(page_header, page_data)
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

    if _split_column_pages_enabled():
        page_results = _stitch_split_column_pages(page_results)
        combined_rows = []
        pages_resolved = 0
        primary_header = []
        for page in page_results:
            combined_rows.extend(page.get("data_rows") or [])
            if page.get("resolved"):
                pages_resolved += 1
            if not primary_header and page.get("header_cells"):
                primary_header = list(page.get("header_cells") or [])

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


class _FitzWordPage:
    """pdfplumber-shaped page so from_pdf_words can consume PyMuPDF words."""

    def __init__(self, words: List[Dict[str, Any]], width: float, height: float):
        self._words = words
        self.width = width
        self.height = height

    def extract_words(self, **_kwargs: Any) -> List[Dict[str, Any]]:
        return list(self._words)


def pdf_built_has_nul_digits(built: Dict[str, Any]) -> bool:
    """True when plumber dropped custom-encoded digits to NUL."""
    for page in (built or {}).get("page_results") or []:
        for row in page.get("data_rows") or []:
            for token in row or []:
                if not isinstance(token, dict):
                    continue
                if "\x00" in str(token.get("text") or ""):
                    return True
    return False


_MASHED_HEADER = re.compile(r"[a-z][A-Z]")


def pdf_built_has_mashed_headers(built: Dict[str, Any]) -> bool:
    """True when adjacent header glyphs collapsed (StocPkur / FreSealesQty)."""
    for page in (built or {}).get("page_results") or []:
        for cell in page.get("header_cells") or []:
            if not isinstance(cell, dict):
                continue
            text = str(cell.get("text") or "")
            if _MASHED_HEADER.search(text) and not _alias_hit(text):
                return True
    return False


def from_fitz_pdf_bytes(file_bytes: bytes) -> Dict[str, Any]:
    """Rebuild native word tables from PyMuPDF, decoding pseudo-digit fonts."""
    try:
        import fitz
    except Exception:
        return {
            "header_cells": [],
            "data_rows": [],
            "pages_total": 0,
            "pages_resolved": 0,
            "page_results": [],
        }
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception:
        return {
            "header_cells": [],
            "data_rows": [],
            "pages_total": 0,
            "pages_resolved": 0,
            "page_results": [],
        }
    try:
        shims: List[_FitzWordPage] = []
        for page in doc:
            rect = page.rect
            words: List[Dict[str, Any]] = []
            for raw in page.get_text("words") or []:
                if not isinstance(raw, (list, tuple)) or len(raw) < 5:
                    continue
                words.append(
                    {
                        "x0": float(raw[0]),
                        "top": float(raw[1]),
                        "x1": float(raw[2]),
                        "bottom": float(raw[3]),
                        "text": decode_pdf_pseudo_digits(raw[4]),
                    }
                )
            shims.append(
                _FitzWordPage(words, float(rect.width), float(rect.height))
            )
        built = from_pdf_words(shims)
        logger.info(
            "STOCK_PDF_PSEUDO_DIGITS source=fitz pages=%s resolved=%s",
            built.get("pages_total"),
            built.get("pages_resolved"),
        )
        return built
    finally:
        doc.close()
