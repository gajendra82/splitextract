"""Native-lane table builders for the shared stock header resolver (Phase 3b).

Pure builders: turn sheet / text / PDF / DOCX sources into header_cells +
row tokens. No I/O beyond structures passed in. No Gemini.
"""

from __future__ import annotations

import re
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
        "clustered_headers": clustered,
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
        header_idx = int(extracted.get("header_row_index") or -1)
        all_rows = list(extracted.get("all_rows") or [])
        data_start = header_idx + 1 if header_idx >= 0 else 0
        candidate_rows = all_rows[data_start:] if header_idx >= 0 else all_rows

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
