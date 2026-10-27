"""Parse HTML tables emitted by PaddleOCR-VL into cell records.

Expands rowspan/colspan so multi-row stock headers (Purchase Qty/Free,
Sales Qty/Free, …) align with body columns. Wrong column association was the
dominant silent-wrong pattern on GT-20 layouts like 0000700209.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple


class _Cell:
    __slots__ = ("text", "rowspan", "colspan")

    def __init__(self, text: str, rowspan: int = 1, colspan: int = 1) -> None:
        self.text = text
        self.rowspan = max(1, rowspan)
        self.colspan = max(1, colspan)


class _StructuredTableParser(HTMLParser):
    """Parse tables while preserving rowspan/colspan attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: List[List[List[_Cell]]] = []
        self._table: Optional[List[List[_Cell]]] = None
        self._row: Optional[List[_Cell]] = None
        self._cell_parts: Optional[List[str]] = None
        self._rowspan = 1
        self._colspan = 1
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attr = {str(k).lower(): str(v) for k, v in attrs}
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []
            self._in_cell = True
            self._rowspan = _positive_int(attr.get("rowspan"), default=1)
            self._colspan = _positive_int(attr.get("colspan"), default=1)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"td", "th"} and self._row is not None and self._cell_parts is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell_parts)).strip()
            self._row.append(_Cell(text, rowspan=self._rowspan, colspan=self._colspan))
            self._cell_parts = None
            self._in_cell = False
            self._rowspan = 1
            self._colspan = 1
        elif tag == "tr" and self._table is not None and self._row is not None:
            if any(cell.text.strip() for cell in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._in_cell and self._cell_parts is not None:
            self._cell_parts.append(data)


def parse_html_tables(text: str) -> List[Dict[str, Any]]:
    """Return flat cell dicts with row/col/header association."""
    if not text or "<table" not in text.lower():
        return []
    parser = _StructuredTableParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return []
    cells: List[Dict[str, Any]] = []
    for table_index, raw_table in enumerate(parser.tables):
        grid = _expand_span_grid(raw_table)
        if not grid:
            continue
        header_rows, data_rows = _split_header_and_body(grid)
        headers = _compose_headers(header_rows, width=len(grid[0]) if grid else 0)
        if not headers:
            continue
        headers, data_rows = _repair_missing_uom_column(headers, data_rows)
        data_rows = _repair_total_rows_without_uom(headers, data_rows)
        data_rows = _repair_pack_column_holds_opening(headers, data_rows)
        data_rows = _repair_qty_shift_when_days_in_closing(headers, data_rows)

        leading_index = _detect_leading_index(headers, data_rows)
        use_headers = headers[1:] if leading_index else headers
        pack_idx = next(
            (
                i
                for i, h in enumerate(use_headers)
                if re.search(r"(?i)^pack(ing|age)?$|^pack$", (h or "").strip())
            ),
            None,
        )
        for c_index, header in enumerate(use_headers):
            cells.append(
                {
                    "table_index": table_index,
                    "row_index": 0,
                    "col_index": c_index,
                    "header": header,
                    "value": header,
                    "product_key": None,
                    "packing": None,
                    "is_header": True,
                }
            )
        for r_index, row in enumerate(data_rows, start=len(header_rows)):
            values = row[1:] if leading_index and row else list(row)
            if _is_section_banner(values):
                continue
            product_key = values[0] if values else None
            packing = (
                values[pack_idx]
                if pack_idx is not None and pack_idx < len(values)
                else None
            )
            for c_index, value in enumerate(values):
                header = use_headers[c_index] if c_index < len(use_headers) else None
                cells.append(
                    {
                        "table_index": table_index,
                        "row_index": r_index,
                        "col_index": c_index,
                        "header": header,
                        "value": value,
                        "product_key": product_key,
                        "packing": packing,
                        "is_header": False,
                    }
                )
    return cells


def iter_expanded_tables(text: str) -> List[Dict[str, Any]]:
    """Yield repaired tables as {headers, rows} for parser reuse."""
    if not text or "<table" not in text.lower():
        return []
    parser = _StructuredTableParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for raw_table in parser.tables:
        grid = _expand_span_grid(raw_table)
        if not grid:
            continue
        header_rows, data_rows = _split_header_and_body(grid)
        headers = _compose_headers(header_rows, width=len(grid[0]) if grid else 0)
        if not headers:
            continue
        headers, data_rows = _repair_missing_uom_column(headers, data_rows)
        data_rows = _repair_total_rows_without_uom(headers, data_rows)
        data_rows = _repair_pack_column_holds_opening(headers, data_rows)
        data_rows = _repair_qty_shift_when_days_in_closing(headers, data_rows)
        leading_index = _detect_leading_index(headers, data_rows)
        use_headers = headers[1:] if leading_index else headers
        rows = []
        for row in data_rows:
            values = row[1:] if leading_index and row else list(row)
            if _is_section_banner(values):
                continue
            rows.append(values)
        out.append({"headers": use_headers, "rows": rows})
    return out


def _positive_int(raw: Optional[str], *, default: int = 1) -> int:
    try:
        value = int(str(raw or default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _expand_span_grid(rows: List[List[_Cell]]) -> List[List[str]]:
    """Expand HTML rows into a rectangular text grid."""
    occupancy: Dict[Tuple[int, int], str] = {}
    max_col = 0
    row_idx = 0
    for html_row in rows:
        col = 0
        for cell in html_row:
            while (row_idx, col) in occupancy:
                col += 1
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    r = row_idx + dr
                    c = col + dc
                    # Origin keeps text; horizontal colspan copies parent label
                    # into the same header row so "Purchase" covers Qty/Free slots.
                    if dr == 0:
                        occupancy[(r, c)] = cell.text
                    elif (r, c) not in occupancy:
                        occupancy[(r, c)] = cell.text
                    max_col = max(max_col, c)
            col += cell.colspan
        row_idx += 1
    if not occupancy:
        return []
    max_row = max(r for r, _ in occupancy)
    width = max_col + 1
    grid: List[List[str]] = []
    for r in range(max_row + 1):
        grid.append([occupancy.get((r, c), "") for c in range(width)])
    return grid


def _split_header_and_body(
    grid: List[List[str]],
) -> Tuple[List[List[str]], List[List[str]]]:
    if not grid:
        return [], []
    header_count = 1
    # Second row is a sub-header when it is short labels (Qty/Free) under
    # colspan parents, not a product data row.
    if len(grid) >= 2 and _looks_like_subheader_row(grid[1], parent=grid[0]):
        header_count = 2
    # Rare 3-row banners: keep conservative.
    if len(grid) >= 3 and _looks_like_subheader_row(grid[2], parent=grid[0]):
        header_count = 3
    return grid[:header_count], grid[header_count:]


def _looks_like_subheader_row(row: List[str], *, parent: List[str]) -> bool:
    """True only for Qty/Free-style child header rows, never product data."""
    texts = [re.sub(r"\s+", " ", (c or "").strip()) for c in row]
    non_empty = [t for t in texts if t]
    if not non_empty:
        return False
    known = {
        "qty",
        "free",
        "value",
        "pack",
        "nos",
        "unit",
        "units",
        "rate",
        "amount",
        "qty.",
        "free qty",
    }
    numeric = sum(1 for t in non_empty if re.fullmatch(r"-?\d+(?:\.\d+)?", t))
    if numeric >= max(1, len(non_empty) // 2):
        return False
    first = texts[0] if texts else ""
    parent0 = (parent[0] if parent else "") or ""
    # Distinct product-like first cell ⇒ body row.
    if (
        first
        and first.casefold() != parent0.casefold()
        and re.search(r"[A-Za-z]{3,}", first)
        and first.casefold() not in known
        and not _UOM_TOKEN_RE.match(first)
    ):
        return False
    labelish = 0
    for i, t in enumerate(texts):
        if not t:
            continue
        parent_t = (parent[i] if i < len(parent) else "") or ""
        if t.casefold() == parent_t.casefold():
            labelish += 1
        elif t.casefold() in known or re.fullmatch(r"qty|free|value|pack|nos", t, re.I):
            labelish += 1
    return labelish >= max(1, int(0.6 * len(non_empty)))


def _compose_headers(header_rows: List[List[str]], *, width: int) -> List[str]:
    if not header_rows:
        return [f"col_{i}" for i in range(width)]
    headers: List[str] = []
    for c in range(width):
        parts: List[str] = []
        for row in header_rows:
            text = re.sub(r"\s+", " ", (row[c] if c < len(row) else "") or "").strip()
            if not text:
                continue
            if not parts or parts[-1].casefold() != text.casefold():
                parts.append(text)
        if not parts:
            headers.append(f"col_{c}")
            continue
        if len(parts) == 1:
            headers.append(parts[0])
            continue
        # "Purchase" + "Qty" → "Purchase Qty"; avoid "Qty Qty".
        parent, child = parts[0], parts[-1]
        if child.casefold() in parent.casefold():
            headers.append(parent)
        elif parent.casefold() in child.casefold():
            headers.append(child)
        else:
            headers.append(f"{parent} {child}")
    # If first row looks numeric-heavy, treat as headerless.
    numericish = sum(
        1 for h in headers if re.fullmatch(r"-?\d+(?:\.\d+)?", h or "")
    )
    if numericish >= max(1, len(headers) // 2):
        return [f"col_{i}" for i in range(width)]
    return headers


def _detect_leading_index(headers: List[str], data_rows: List[List[str]]) -> bool:
    if not headers:
        return False
    first = headers[0]
    second = headers[1] if len(headers) > 1 else ""
    # Explicit serial-number columns (Sr. No. / S.No / #).
    if re.search(
        r"(?i)^(sr\.?\s*no\.?|s\.?\s*no\.?|sl\.?\s*no\.?|sno|#|no\.?)$",
        (first or "").strip(),
    ):
        if re.search(r"product|item|particular|description", second or "", re.I):
            return True
        body = data_rows[:8]
        if body:
            idxish = sum(
                1
                for row in body
                if row and re.fullmatch(r"\d{1,4}", str(row[0] or "").strip())
            )
            if idxish >= max(1, len(body) // 2):
                return True
    if re.fullmatch(r"\d{1,4}", first or "") and re.search(
        r"product|item|particular", second or "", re.I
    ):
        return True
    if re.fullmatch(r"\d{1,4}", first or "") and not re.search(
        r"qty|stock|value|pack|opening|issue|sales", first or "", re.I
    ):
        body = data_rows[:8]
        if body:
            idxish = sum(
                1
                for row in body
                if row and re.fullmatch(r"\d{1,4}", row[0] or "")
            )
            return idxish >= max(1, len(body) // 2)
    return False


def _is_section_banner(values: List[str]) -> bool:
    non_empty = [v for v in values if str(v or "").strip()]
    if len(non_empty) == 1 and len(values) >= 3:
        return True
    return False


_UOM_TOKEN_RE = re.compile(
    r"(?i)^(pkt|bot|bol|tub|tube|pcs?|pot|str|gel|nos|strip|box|cap|tab|syr|amp|vial|jar|bag|rol|roll|unit|units)$"
)
_QTY_HEADER_RE = re.compile(
    r"(?i)^(op\.?\s*stock|opening|op\.?|ob|op\s*qty|opstk|receipt|receive|pur|issue|issues|"
    r"sales?|closing|cl\.?\s*stock|clstk|cb|m\.?exp|early\s*exp)"
)
_CLOSING_EXP_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s+(\d{1,2}[/-]\d{2,4})\s*$"
)


def _repair_missing_uom_column(
    headers: List[str],
    data_rows: List[List[str]],
) -> Tuple[List[str], List[List[str]]]:
    """Fix VL tables that drop a UOM column and shift qty headers left.

    Example body: Product | Pack | PKT | 72 | - | - | 72 12/27
    with headers: Item | Item | OPENING | RECEIPT | ISSUE | CLOSING | M.EXP
    → OPENING was reading 'PKT'. Insert UOM and realign qty headers; split
    glued closing+expiry in the last column when present.
    """
    if not headers or not data_rows:
        return headers, data_rows
    qty_indexes = [
        i for i, h in enumerate(headers) if _QTY_HEADER_RE.search((h or "").strip())
    ]
    if not qty_indexes:
        return headers, data_rows
    first_qty = qty_indexes[0]
    samples = []
    for row in data_rows[:12]:
        if first_qty >= len(row):
            continue
        if _is_section_banner(row):
            continue
        val = re.sub(r"\s+", " ", str(row[first_qty] or "")).strip()
        if not val or val in {"-", "—", "--"}:
            continue
        samples.append(val)
    if len(samples) < 2:
        return headers, data_rows
    uomish = sum(1 for v in samples if _UOM_TOKEN_RE.match(v))
    if uomish < max(2, int(0.5 * len(samples))):
        return headers, data_rows

    # Rename first qty column to UOM and shift subsequent qty header labels right
    # within the existing width (last label dropped / absorbed into split values).
    new_headers = list(headers)
    new_headers[first_qty] = "UOM"
    for src, dst in zip(qty_indexes, qty_indexes[1:] + [None]):
        if dst is None:
            break
        new_headers[dst] = headers[src]

    # Prefer CLOSING on the final shifted slot when M.EXP absorbed closing digits.
    last_qty = qty_indexes[-1]
    if last_qty < len(new_headers):
        prev_last = headers[qty_indexes[-2]] if len(qty_indexes) >= 2 else ""
        if re.search(r"m\.?exp|expir", headers[last_qty] or "", re.I) and re.search(
            r"clos|cl\.|cb", prev_last or "", re.I
        ):
            new_headers[last_qty] = prev_last or "CLOSING"

    new_rows: List[List[str]] = []
    for row in data_rows:
        values = list(row)
        if last_qty < len(values):
            m = _CLOSING_EXP_RE.match(str(values[last_qty] or ""))
            if m:
                # Keep closing qty in the CLOSING column; expose expiry via
                # synthetic trailing cell only when width allows, else encode
                # as "qty|date" and let matchers use the left number for CLOSING.
                values[last_qty] = m.group(1)
                # Attach expiry on a side-channel header if we still have M.EXP alias
                # by appending a virtual column used only for lookup.
                values.append(m.group(2))
        new_rows.append(values)

    if any(len(r) > len(new_headers) for r in new_rows):
        new_headers = list(new_headers) + ["M.EXP"]
    return new_headers, new_rows


_PACK_DESC_RE = re.compile(
    r"(?i)\b\d+\s*(ml|mg|gm|g|kg|s|tab|caps?|nos|pc|pcs)\b|^(pc|pcs|60s|100s)$"
)
_DAYS_IN_CELL_RE = re.compile(r"(?i)^\s*\d+(?:\.\d+)?\s*days?\s*$")


def _repair_qty_shift_when_days_in_closing(
    headers: List[str],
    data_rows: List[List[str]],
) -> List[List[str]]:
    """Realign qty cells when liquidation Days landed under Closing.

    Evidence (doc 0308): Opening empty on source, VL shifts Receipt/Issues/
    Closing one column left, and ``142 Days`` appears under Closing while
    Liquidation is blank. Move Days to Liquidation and shift qty values right
    without inventing numbers from the stock equation.
    """
    if not headers or not data_rows:
        return data_rows

    def _find(pred) -> Optional[int]:
        for i, h in enumerate(headers):
            if pred((h or "").strip().casefold()):
                return i
        return None

    open_i = _find(
        lambda h: bool(re.search(r"\bop(?:ening)?\b|\bop\.?\b|\bopstk\b|\bob\b", h))
        and "exp" not in h
        and "amount" not in h
        and "value" not in h
    )
    receipt_i = _find(
        lambda h: bool(re.search(r"receipt|receive|\bpur\b|purchase", h))
        and "rtn" not in h
        and "ret" not in h
        and "free" not in h
        and "amount" not in h
    )
    issues_i = _find(
        lambda h: bool(re.search(r"issue", h))
        and "free" not in h
        and "rtn" not in h
        and "ret" not in h
        and "amount" not in h
        and "value" not in h
        and "closing" not in h
    )
    if issues_i is None:
        issues_i = _find(
            lambda h: bool(re.search(r"\bsales?\b", h))
            and "free" not in h
            and "amount" not in h
            and "value" not in h
            and "closing" not in h
        )
    close_i = _find(
        lambda h: bool(re.search(r"closing|cl\.?\s*stock|\bclstk\b|\bcb\b", h))
        and "amount" not in h
        and "value" not in h
        and "days" not in h
    )
    liq_i = _find(lambda h: bool(re.search(r"liquid|days", h)))

    if open_i is None or receipt_i is None or close_i is None:
        return data_rows
    if issues_i is None:
        return data_rows

    touched = False
    out: List[List[str]] = []
    for row in data_rows:
        values = list(row)
        if close_i >= len(values):
            out.append(values)
            continue
        close_raw = str(values[close_i] or "").strip()
        if not _DAYS_IN_CELL_RE.match(close_raw):
            out.append(values)
            continue
        # Require a numeric opening occupying the shifted receipt slot.
        open_raw = str(values[open_i] or "").strip() if open_i < len(values) else ""
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", open_raw or ""):
            out.append(values)
            continue
        # Shift right: Opening←empty, Receipt←Opening, Issues←Receipt, Closing←Issues.
        chain = [open_i, receipt_i, issues_i, close_i]
        # Ensure width for liquidation.
        need = max(chain + ([liq_i] if liq_i is not None else [])) + 1
        while len(values) < need:
            values.append("")
        days_val = values[close_i]
        for dst, src in zip(reversed(chain[1:]), reversed(chain[:-1])):
            values[dst] = values[src]
        values[open_i] = ""
        if liq_i is not None:
            if not str(values[liq_i] or "").strip():
                values[liq_i] = days_val
        else:
            values.append(days_val)
        touched = True
        out.append(values)

    if touched and liq_i is None and any(len(r) > len(headers) for r in out):
        # Caller only uses data_rows here; headers gain Liquidation via parse path
        # only when we also return headers. Keep values; trailing col is ignored
        # unless headers are extended by the caller. Extend in-place via side
        # effect is not available — append a virtual header by mutating list.
        headers.append("Liquidation")
    return out if touched else data_rows


def _repair_pack_column_holds_opening(
    headers: List[str],
    data_rows: List[List[str]],
) -> List[List[str]]:
    """Fix VL rows where opening landed under PACK and SAL/CB under FR.

    Seen on SNO/ITEM/DESCRIPTION/PACK/OB/PUR/FR/SAL/FR/CB layouts when
    DESCRIPTION already carries the pack size (200ML, 60S, PC, …).
    """
    if not headers or not data_rows:
        return data_rows
    norm = [(h or "").strip().casefold() for h in headers]

    def _idx(*names: str) -> Optional[int]:
        for i, h in enumerate(norm):
            if h in names:
                return i
        return None

    pack_i = _idx("pack", "packing")
    ob_i = _idx("ob", "op", "opening", "opstk", "op.bal. qty.")
    desc_i = _idx("description", "packing")
    if desc_i == pack_i:
        desc_i = _idx("description")
    pur_i = _idx("pur", "purchase", "receipt")
    sal_i = _idx("sal", "sale", "sales", "sales qty")
    cb_i = _idx("cb", "closing", "clstk", "closing balance")
    if pack_i is None or ob_i is None or pack_i >= ob_i:
        return data_rows
    # FR columns between PUR and SAL / SAL and CB.
    fr_indexes = [i for i, h in enumerate(norm) if h in {"fr", "free"}]
    fr1 = next((i for i in fr_indexes if pur_i is not None and pur_i < i < (sal_i or 10**9)), None)
    fr2 = next((i for i in fr_indexes if sal_i is not None and sal_i < i < (cb_i or 10**9)), None)

    nonzero_pack_samples = 0
    pack_as_ob = 0
    for row in data_rows[:40]:
        if max(pack_i, ob_i) >= len(row):
            continue
        desc = str(row[desc_i] if desc_i is not None and desc_i < len(row) else "")
        if not _PACK_DESC_RE.search(desc.strip()):
            continue
        pack_raw = re.sub(r"[\s,]", "", str(row[pack_i] or ""))
        ob_raw = re.sub(r"[\s,]", "", str(row[ob_i] or ""))
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", pack_raw or ""):
            continue
        if float(pack_raw) == 0:
            continue
        nonzero_pack_samples += 1
        if ob_raw in {"", "0", "0.0", "0.00"}:
            pack_as_ob += 1
    if nonzero_pack_samples < 2 or pack_as_ob < max(
        2, int(0.5 * nonzero_pack_samples)
    ):
        return data_rows

    repaired: List[List[str]] = []
    for row in data_rows:
        values = list(row)
        if max(pack_i, ob_i) >= len(values):
            repaired.append(values)
            continue
        desc = str(values[desc_i] if desc_i is not None and desc_i < len(values) else "")
        pack_raw = re.sub(r"[\s,]", "", str(values[pack_i] or ""))
        ob_raw = re.sub(r"[\s,]", "", str(values[ob_i] or ""))
        if _PACK_DESC_RE.search(desc.strip()) and re.fullmatch(
            r"-?\d+(?:\.\d+)?", pack_raw or ""
        ) and ob_raw in {"", "0", "0.0", "0.00"} and float(pack_raw) != 0:
            values[ob_i] = values[pack_i]
            values[pack_i] = ""
            # SAL blank but first FR holds sales; second FR holds CB while CB is money.
            if sal_i is not None and fr1 is not None and sal_i < len(values) and fr1 < len(values):
                sal_raw = re.sub(r"[\s,]", "", str(values[sal_i] or ""))
                fr1_raw = re.sub(r"[\s,]", "", str(values[fr1] or ""))
                if sal_raw in {"", "0", "0.0"} and re.fullmatch(r"-?\d+", fr1_raw or ""):
                    if float(fr1_raw) != 0:
                        values[sal_i] = values[fr1]
                        values[fr1] = "0"
            if (
                cb_i is not None
                and fr2 is not None
                and cb_i < len(values)
                and fr2 < len(values)
            ):
                cb_raw = str(values[cb_i] or "").strip()
                fr2_raw = re.sub(r"[\s,]", "", str(values[fr2] or ""))
                if re.fullmatch(r"-?\d+", fr2_raw or "") and re.search(
                    r"\d+\.\d{2}$", cb_raw.replace(",", "")
                ):
                    # CB holds RATE; second FR holds true closing qty.
                    values[cb_i] = values[fr2]
                    values[fr2] = "0"
        repaired.append(values)
    return repaired


def _repair_total_rows_without_uom(
    headers: List[str],
    data_rows: List[List[str]],
) -> List[List[str]]:
    """TOTAL lines usually have no UOM token — undo UOM shift for those rows only."""
    if not headers or not data_rows:
        return data_rows
    try:
        uom_i = next(i for i, h in enumerate(headers) if (h or "").strip().upper() == "UOM")
    except StopIteration:
        return data_rows
    repaired: List[List[str]] = []
    for row in data_rows:
        values = list(row)
        product = str(values[0] if values else "").strip()
        if not re.search(r"(?i)^totals?\b", product):
            repaired.append(values)
            continue
        if uom_i >= len(values):
            repaired.append(values)
            continue
        uom_val = re.sub(r"[\s,]", "", str(values[uom_i] or ""))
        if re.fullmatch(r"-?\d+(?:\.\d+)?", uom_val or ""):
            # Insert empty UOM slot and keep numeric run under OPENING…CLOSING.
            values = values[:uom_i] + [""] + values[uom_i:]
            if len(values) > len(headers):
                values = values[: len(headers)]
            elif len(values) < len(headers):
                values = values + [""] * (len(headers) - len(values))
        repaired.append(values)
    return repaired


def _normalize_product_key(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip()).casefold()
    text = re.sub(r"[.\-]+$", "", text)
    return text


def packing_hint_from_field_id(field_id: Optional[str]) -> Optional[str]:
    """Infer pack size hint from GT/parser field ids like liv200_op / liv100_cl."""
    if not field_id:
        return None
    fid = str(field_id).casefold()
    m = re.search(r"(?:^|_)(?:liv|pack|sku)?(\d{2,4})(?:ml)?(?:_|$)", fid)
    if not m:
        m = re.search(r"(\d{2,4})ml", fid)
    if not m:
        return None
    return m.group(1)


def _header_aliases(header: str) -> List[str]:
    h = re.sub(r"\s+", " ", (header or "").strip().casefold())
    aliases = {h}
    compact = re.sub(r"[^a-z0-9]", "", h)
    aliases.add(compact)
    mapping = {
        "openingstock": [
            "opening stock",
            "op.bal. qty.",
            "op.bal qty",
            "op.bal. qty",
            "op.",
            "opening",
        ],
        "openingvalue": ["opening value"],
        "receivequantity": [
            "receive quantity",
            "receipt qty.",
            "receipt qty",
            "pur",
            "receipt",
        ],
        "receipt": ["receipt", "receipt qty.", "receipt qty", "receive quantity"],
        "receivevalue": ["receive value"],
        "issuequantity": [
            "issue quantity",
            "issue qty.",
            "issue qty",
            "sale",
            "sales",
            "issues",
        ],
        "issuevalue": ["issue value"],
        "closingstock": [
            "closing stock",
            "closing balance",
            "bal.",
            "balance",
            "closing",
        ],
        "closingvalue": ["closing value", "bval"],
        "productdescription": ["product description", "product name", "item", "product"],
        "productname": ["product name", "product description", "item", "product"],
        "expirystock": ["expiry stock"],
        "expirybreakage": ["expiry breakage"],
        "totalqty": ["total qty.", "total qty"],
        "packing": ["packing", "pack"],
        "sval": ["sval", "sales value"],
        "salesamount": ["sales amount", "sval", "sales value"],
        # Keep amount/value aliases separate from bare qty "closing".
        "closingamount": [
            "closing amount",
            "closing value",
            "bval",
            "cl. rs.",
            "cl.rs",
        ],
        "closingbalance": [
            "closing balance",
            "cl. stock as on",
            "clstk",
            "clstock",
            "cb",
        ],
        "opbalqty": ["op.bal. qty.", "op.bal qty", "op.bal. qty"],
        "receiptqty": ["receipt qty.", "receipt qty"],
        "issueqty": ["issue qty.", "issue qty"],
        "opstk": [
            "opstk",
            "op. stock",
            "op.stock",
            "opening stock",
            "opening",
            "ob",
            "op qty",
        ],
        "clstk": [
            "clstk",
            "cl. stock as on",
            "cl.stock as on",
            "clstock as on",
            "closing stock",
            "closing",
            "cb",
            "closing balance",
            "clstock",
        ],
        "clstockason": [
            "cl. stock as on",
            "cl.stock as on",
            "cl stock as on",
            "clstk",
            "closing stock",
            "closing balance",
        ],
        "ob": ["ob", "opening", "opstk", "op.bal. qty.", "op qty"],
        "cb": ["cb", "closing", "clstk", "closing balance", "clstock"],
        "sal": ["sal", "sale", "sales", "sale qty", "sales qty", "issue qty."],
        "salesqty": ["sales qty", "sales", "sale qty", "issue qty.", "sales quantity"],
        "purchaseqty": ["purchase qty", "purchase", "pur", "pur qty", "receipt qty."],
        "clstock": ["clstock", "closing balance", "clstk", "cb"],
        "clvalue": ["clvalue", "closing value", "closing amount"],
        "salesvalue": ["salesvalue", "sales value", "sales amount", "sval"],
        "opqty": ["op qty", "op.bal. qty.", "opening", "ob"],
        "purqty": ["pur qty", "pur", "purchase", "receipt qty."],
        "earlyexp": ["early exp.", "expiry stock", "m.exp"],
        "mexp": ["m.exp", "early exp.", "expiry stock"],
        "qty": ["qty.", "qty", "quantity"],
    }
    for key, values in mapping.items():
        if compact == key or h in values:
            aliases.update(values)
            aliases.add(key)
    # Prefer qty child under Sales/Purchase when compact ends with qty.
    if compact.endswith("qty") and "sales" in compact:
        aliases.update(mapping["salesqty"])
        aliases.add("salesqty")
        aliases.add("sales")
    if compact.endswith("qty") and "purchase" in compact:
        aliases.update(mapping["purchaseqty"])
        aliases.add("purchaseqty")
        aliases.add("purchase")
    # VL often prefixes Goods/Stock onto Closing/Issues child headers.
    if "closing" in compact and "amount" not in compact and "value" not in compact:
        aliases.update(mapping["closingstock"])
        aliases.add("closing")
        aliases.add("closingstock")
    if "issue" in compact and "free" not in compact and "value" not in compact:
        aliases.update(mapping["issuequantity"])
        aliases.add("issues")
        aliases.add("issuequantity")
    # Garbled OCR headers like "Sales\\n0tv" still expose a Sales token.
    for token in re.findall(r"[a-z]{3,}", h):
        if token in {"sales", "sale", "opening", "receipt", "closing", "issues", "issue"}:
            aliases.add(token)
            if token in {"sales", "sale"}:
                aliases.update(mapping["sal"])
            if token == "opening":
                aliases.update(mapping["opstk"])
            if token == "receipt":
                aliases.update(mapping["receipt"])
            if token == "closing":
                aliases.update(mapping["closingstock"])
            if token in {"issues", "issue"}:
                aliases.update(mapping["issuequantity"])
    return list(aliases)


def row_lookup(
    cells: List[Dict[str, Any]],
    *,
    product_key: str,
    header: str,
    packing_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Find cell value for product row + printed header.

    Optional packing_hint (e.g. \"200\") prefers the matching Packing cell when
    the same product appears on multiple strength rows.
    """
    target_raw = re.sub(r"\s+", " ", (product_key or "").strip().casefold())
    target_product = _normalize_product_key(product_key or "")
    header_aliases = set(_header_aliases(header))
    target_is_total = bool(re.search(r"(?i)^totals?\b", product_key or ""))
    header_l = (header or "").casefold()
    want_closing_total = target_is_total and bool(
        re.search(r"clos", header_l) and not re.search(r"sales?", header_l)
    )
    want_sales_total = target_is_total and bool(
        re.search(r"sales?", header_l) and not re.search(r"clos", header_l)
    )
    pack_hint = re.sub(r"\D", "", str(packing_hint or ""))
    candidates: List[tuple[int, Dict[str, Any]]] = []
    for cell in cells:
        if cell.get("is_header"):
            continue
        product_raw = re.sub(
            r"\s+", " ", str(cell.get("product_key") or "").strip().casefold()
        )
        product = _normalize_product_key(str(cell.get("product_key") or ""))
        if not product:
            continue
        score = 0
        if target_is_total and re.search(r"(?i)total", product_raw):
            score = 80
            if want_closing_total and re.search(r"(?i)clos", product_raw):
                score += 40
            elif want_sales_total and re.search(r"(?i)sales?", product_raw):
                score += 40
            elif want_closing_total and re.search(r"(?i)sales?", product_raw):
                score -= 20
            elif want_sales_total and re.search(r"(?i)clos", product_raw):
                score -= 20
        elif product == target_product or product_raw == target_raw:
            score = 100
        elif product.startswith(target_product + " ") or product.startswith(
            target_product + "-"
        ):
            score = 40
        elif target_product.startswith(product + " ") or target_product.startswith(
            product + "-"
        ):
            score = 20
        elif target_product[:16] and product.startswith(target_product[:16]):
            score = 30
        else:
            continue
        cell_header = str(cell.get("header") or "")
        aliases = set(_header_aliases(cell_header))
        if not (header_aliases & aliases):
            continue
        header_bonus = 0
        ch = cell_header.casefold()
        th = (header or "").casefold()
        if ch == th:
            header_bonus = 5
        elif "free" in ch and "free" not in th:
            header_bonus = -5
        elif "rtn" in ch or "ret" in ch:
            if "rtn" not in th and "ret" not in th:
                header_bonus = -5
        # Never treat amount/value/Rs columns as qty Closing/Opening.
        amountish = bool(re.search(r"amount|value|\brs\b", ch))
        want_amount = bool(re.search(r"amount|value|\brs\b", th))
        if amountish and not want_amount:
            continue
        if want_amount and not amountish and re.search(
            r"(?i)^(opening|closing|op\.?|cl\.?|ob|cb)$", ch.strip()
        ):
            # Allow TOTAL banner rows that repeat the amount under every col.
            if not target_is_total:
                continue
        pack_bonus = 0
        packing = str(cell.get("packing") or "")
        pack_digits = re.sub(r"\D", "", packing)
        if pack_hint:
            if pack_digits and (
                pack_digits == pack_hint
                or pack_digits.startswith(pack_hint)
                or pack_hint.startswith(pack_digits)
            ):
                pack_bonus = 25
            elif pack_digits and pack_digits != pack_hint:
                pack_bonus = -15
        candidates.append((score + header_bonus + pack_bonus, cell))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score = candidates[0][0]
    top = [c for s, c in candidates if s == best_score]
    if len(top) == 1:
        return top[0]
    # Tie-break: prefer a non-blank/non-zero qty over an all-empty pack line.
    def _value_rank(cell: Dict[str, Any]) -> tuple:
        raw = str(cell.get("value") or "").strip()
        if raw in {"", "-", "—", "--", "-Nil-", "Nil"}:
            return (0, 0)
        num = re.sub(r"[^\d.\-]", "", raw)
        try:
            return (1, 1 if abs(float(num)) > 0 else 0)
        except ValueError:
            return (1, 0)

    top.sort(key=_value_rank, reverse=True)
    return top[0]
