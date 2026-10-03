"""Coordinate / HTML-aware stock table reconstruction from OCR providers.

Benchmark-only. Preserves blank cells so Balance never shifts into Purc Ret.
Never invents printed quantities; reuse header resolver + closing/recon rules.
"""

from __future__ import annotations

import logging
import re
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bs4 import BeautifulSoup

from services.stock_header_resolver import resolve_columns
from services.stock_ocr_providers.base import OCRBlock, OCRDocumentResult, OCRPage
from services.stock_reconciliation import apply_stock_reconciliation
from services.stock_row_classifier import apply_closing_derived
from services.stock_vision_table import parse_number

logger = logging.getLogger(__name__)

_TOP_LEVEL_QTY = {
    "opening_qty": "opening_qty",
    "purchase_qty": "receipts_qty",
    "sales_qty": "sales_qty",
    "closing_qty": "closing_qty",
    "opening_value": "opening_value",
    "sales_value": "sales_value",
    "closing_value": "closing_value",
}
_EXTRA_FIELDS = {
    "sales_return_qty": "sale_return",
    "purchase_return_qty": "purchase_return",
    "expiry_damage_qty": "exp_damage",
    "total_qty": "total_stock",
    "free_in_qty": "free_in_qty",
    "free_out_qty": "free_out_qty",
    "pack": "packing",  # handled specially
}

_HEADER_HINT = re.compile(
    r"opening|purchase|goods\s*ret|total\s*in|sale|purc\.?\s*ret|balance|closing|qty",
    re.I,
)
_PRODUCT_HINT = re.compile(
    r"^[A-Z0-9][A-Z0-9 \/\-\.\(\)%]{2,}$"
)


def _bbox_center(bbox: Tuple[float, float, float, float]) -> Tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def parse_html_table(html: str) -> List[List[Optional[str]]]:
    """Parse HTML table into a grid. Empty cells stay None (never left-shifted)."""
    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table")
    if table is None:
        return []
    grid: List[List[Optional[str]]] = []
    for tr in table.find_all("tr"):
        cells: List[Optional[str]] = []
        for td in tr.find_all(["td", "th"]):
            text = td.get_text(" ", strip=True)
            cells.append(None if text == "" else text)
        if cells:
            grid.append(cells)
    return grid


def parse_markdown_table(md: str) -> List[List[Optional[str]]]:
    """Parse a GitHub-style markdown table; empty cells preserved."""
    lines = [ln.strip() for ln in (md or "").splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return []
    grid: List[List[Optional[str]]] = []
    for i, line in enumerate(lines):
        parts = [p.strip() for p in line.strip("|").split("|")]
        # skip separator row
        if i == 1 and all(re.fullmatch(r":?-+:?", p or "") for p in parts):
            continue
        grid.append([None if p == "" else p for p in parts])
    return grid


def _is_header_row(cells: Sequence[Optional[str]]) -> bool:
    texts = [str(c or "") for c in cells]
    hits = sum(1 for t in texts if _HEADER_HINT.search(t))
    return hits >= 3


def _cluster_rows(
    blocks: Sequence[OCRBlock],
    *,
    y_tol: Optional[float] = None,
) -> List[List[OCRBlock]]:
    usable = [b for b in blocks if b.bbox is not None and str(b.text or "").strip()]
    if not usable:
        return []
    heights = [abs(b.bbox[3] - b.bbox[1]) for b in usable if b.bbox]
    med_h = float(median(heights)) if heights else 10.0
    tol = y_tol if y_tol is not None else max(4.0, med_h * 0.55)
    ordered = sorted(usable, key=lambda b: (_bbox_center(b.bbox)[1], _bbox_center(b.bbox)[0]))
    rows: List[List[OCRBlock]] = []
    current: List[OCRBlock] = []
    current_y: Optional[float] = None
    for block in ordered:
        cy = _bbox_center(block.bbox)[1]
        if current_y is None or abs(cy - current_y) <= tol:
            current.append(block)
            current_y = cy if current_y is None else (current_y * 0.7 + cy * 0.3)
        else:
            rows.append(sorted(current, key=lambda b: _bbox_center(b.bbox)[0]))
            current = [block]
            current_y = cy
    if current:
        rows.append(sorted(current, key=lambda b: _bbox_center(b.bbox)[0]))
    return rows


def _detect_header_columns_from_blocks(
    rows: List[List[OCRBlock]],
) -> Tuple[Optional[List[Dict[str, Any]]], int]:
    """Return (columns from resolve_columns, header_row_index)."""
    for idx, row in enumerate(rows[:12]):
        cells = []
        for j, block in enumerate(row):
            cx, _cy = _bbox_center(block.bbox)  # type: ignore[arg-type]
            cells.append(
                {
                    "text": block.text,
                    "col_index": j,
                    "x_center": cx,
                }
            )
        if not _is_header_row([c["text"] for c in cells]):
            # Also try joining adjacent header fragments if too fragmented
            joined = " ".join(str(c["text"]) for c in cells)
            if not _HEADER_HINT.search(joined):
                continue
        resolved = resolve_columns(cells)
        columns = resolved.get("columns") or []
        # resolve_columns may omit geometry — re-attach x_center by col_index.
        x_by_idx = {
            int(c["col_index"]): c.get("x_center")
            for c in cells
            if c.get("col_index") is not None
        }
        for col in columns:
            col_idx = int(col.get("col_index") or 0)
            if col.get("x_center") is None and x_by_idx.get(col_idx) is not None:
                col["x_center"] = float(x_by_idx[col_idx])
        qty_like = sum(
            1
            for c in columns
            if str(c.get("canonical") or "")
            in {
                "opening_qty",
                "purchase_qty",
                "sales_qty",
                "closing_qty",
                "total_qty",
                "sales_return_qty",
                "purchase_return_qty",
            }
        )
        if qty_like >= 3:
            return columns, idx
    return None, -1


def _grid_to_header_and_body(
    grid: List[List[Optional[str]]],
) -> Tuple[List[Dict[str, Any]], List[List[Optional[str]]], List[Dict[str, Any]]]:
    if not grid:
        return [], [], [{"code": "EMPTY_GRID"}]
    header_idx = 0
    for i, row in enumerate(grid[:5]):
        if _is_header_row(row):
            header_idx = i
            break
    header_cells = []
    header_row = grid[header_idx]
    # optional second header line (Qty / In. Qty)
    sub = grid[header_idx + 1] if header_idx + 1 < len(grid) else []
    if sub and all(
        (c is None or re.fullmatch(r"(?i)qty|in\.?\s*qty|val(ue)?", str(c).strip() or ""))
        for c in sub
    ):
        body_start = header_idx + 2
        for j, text in enumerate(header_row):
            sub_t = sub[j] if j < len(sub) else None
            merged = " ".join(x for x in [text or "", sub_t or ""] if x).strip()
            header_cells.append(
                {
                    "text": merged or (text or ""),
                    "col_index": j,
                    "x_center": float(j),
                    "subheader_text": sub_t,
                }
            )
    else:
        body_start = header_idx + 1
        for j, text in enumerate(header_row):
            header_cells.append(
                {
                    "text": text or "",
                    "col_index": j,
                    "x_center": float(j),
                }
            )
    resolved = resolve_columns(header_cells)
    columns = resolved.get("columns") or []
    x_by_idx = {
        int(c["col_index"]): c.get("x_center")
        for c in header_cells
        if c.get("col_index") is not None
    }
    for col in columns:
        idx = int(col.get("col_index") or 0)
        if col.get("x_center") is None and x_by_idx.get(idx) is not None:
            col["x_center"] = float(x_by_idx[idx])
    errors = list(resolved.get("errors") or [])
    body = grid[body_start:]
    # Pad each body row to header width without left-shifting
    width = len(header_cells)
    padded: List[List[Optional[str]]] = []
    for row in body:
        r = list(row) + [None] * max(0, width - len(row))
        padded.append(r[:width])
    return columns, padded, errors


def fields_to_line_item(
    fields: Dict[str, Optional[str]],
    *,
    printed_fields: Sequence[str],
    row_index: int,
    source_cells: Optional[List[Optional[str]]] = None,
    cell_bboxes: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Map canonical field texts to a stock line_item without inventing values."""
    item: Dict[str, Any] = {
        "product_code": None,
        "product_name": None,
        "packing": None,
        "opening_qty": 0.0,
        "receipts_qty": 0.0,
        "sales_qty": 0.0,
        "sales_value": 0.0,
        "closing_qty": None,
        "closing_value": 0.0,
        "extra": {},
    }
    field_source: Dict[str, str] = {}
    printed = set(printed_fields)

    for canon, raw in fields.items():
        if canon == "ignore":
            continue
        missing = raw is None or str(raw).strip() == ""
        if canon in {"product_name", "pack", "batch"}:
            text = None if missing else str(raw).strip()
            if canon == "product_name":
                item["product_name"] = text
            elif canon == "pack":
                item["packing"] = text
            else:
                item["extra"]["batch"] = text
            field_source[canon] = "missing" if missing else "printed"
            continue

        if missing:
            field_source[canon] = "missing"
            if canon == "closing_qty":
                item["closing_qty"] = None
            elif canon in _TOP_LEVEL_QTY and canon != "closing_qty":
                item[_TOP_LEVEL_QTY[canon]] = 0.0
            elif canon in _EXTRA_FIELDS:
                item["extra"][_EXTRA_FIELDS[canon]] = None
            continue

        parsed = parse_number(raw, allow_internal_spaces=True)
        if parsed is None:
            field_source[canon] = "missing"
            if canon == "closing_qty":
                item["closing_qty"] = None
            continue

        value = float(parsed)
        field_source[canon] = "printed"
        if canon in _TOP_LEVEL_QTY:
            item[_TOP_LEVEL_QTY[canon]] = value
        elif canon in _EXTRA_FIELDS:
            item["extra"][_EXTRA_FIELDS[canon]] = value

    for absent in (
        "closing_qty",
        "sales_return_qty",
        "purchase_return_qty",
        "expiry_damage_qty",
        "total_qty",
        "free_in_qty",
        "free_out_qty",
    ):
        if absent not in printed and absent not in field_source:
            field_source[absent] = "missing"
            if absent == "closing_qty":
                item["closing_qty"] = None

    item["extra"]["field_source"] = field_source
    item["extra"]["ocr_row_index"] = row_index
    item["extra"]["total_in_layout"] = "total_qty" in printed
    if source_cells is not None:
        item["extra"]["ocr_raw_cells"] = list(source_cells)
    if cell_bboxes is not None:
        item["extra"]["ocr_cell_bboxes"] = list(cell_bboxes)

    apply_closing_derived(item, printed_fields=list(printed), request_id="ocr-benchmark")
    return item


def _assign_grid_row(
    row: Sequence[Optional[str]],
    columns: Sequence[Dict[str, Any]],
) -> Dict[str, Optional[str]]:
    fields: Dict[str, Optional[str]] = {
        str(c["canonical"]): None
        for c in columns
        if c.get("canonical") and c["canonical"] != "ignore"
    }
    for c in columns:
        canon = c.get("canonical")
        if not canon or canon == "ignore":
            continue
        idx = int(c["col_index"])
        raw = row[idx] if 0 <= idx < len(row) else None
        if fields.get(canon) is not None and raw not in (None, ""):
            # collision → leave for diagnostics; prefer first
            continue
        if fields.get(canon) is None:
            fields[canon] = None if raw is None or str(raw).strip() == "" else str(raw)
    return fields


def reconstruct_from_html_tables(page: OCRPage) -> Optional[Dict[str, Any]]:
    for table in page.tables or []:
        content = table.content or ""
        if table.format.lower() == "html" or "<table" in content.lower():
            grid = parse_html_table(content)
        else:
            grid = parse_markdown_table(content)
        if len(grid) < 2:
            continue
        columns, body, errors = _grid_to_header_and_body(grid)
        if not columns:
            continue
        printed = sorted(
            {
                str(c["canonical"])
                for c in columns
                if c.get("canonical") and c["canonical"] != "ignore"
            }
        )
        items: List[Dict[str, Any]] = []
        for i, row in enumerate(body):
            fields = _assign_grid_row(row, columns)
            name = fields.get("product_name")
            if not name:
                continue
            items.append(
                fields_to_line_item(
                    fields,
                    printed_fields=printed,
                    row_index=i,
                    source_cells=list(row),
                )
            )
        if items:
            logger.info(
                "STOCK_OCR_BENCHMARK reconstruct=html_table page=%s rows=%s",
                page.page_number,
                len(items),
            )
            return {
                "strategy": "html_table",
                "column_map": columns,
                "errors": errors,
                "line_items": items,
                "printed_fields": printed,
            }
    return None


def reconstruct_from_blocks(page: OCRPage) -> Optional[Dict[str, Any]]:
    text_blocks = [
        b
        for b in (page.blocks or [])
        if b.bbox is not None
        and b.block_type in {"text", "title", "list", "caption", "aside_text", "gemini_cell"}
        and str(b.text or "").strip()
    ]
    # Skip huge multi-line table blobs; prefer short cell-like blocks.
    cell_like = [
        b
        for b in text_blocks
        if "\n" not in (b.text or "") and len(str(b.text or "")) <= 80
    ]
    if len(cell_like) < 8:
        cell_like = text_blocks
    rows = _cluster_rows(cell_like)
    columns, header_idx = _detect_header_columns_from_blocks(rows)
    if not columns or header_idx < 0:
        return None
    printed = sorted(
        {
            str(c["canonical"])
            for c in columns
            if c.get("canonical") and c["canonical"] != "ignore"
        }
    )
    # Build column index → x_center for blank preservation:
    # for each body row, create a slot per column; assign tokens by nearest x.
    from services.stock_header_resolver import assign_cells

    items: List[Dict[str, Any]] = []
    for i, row_blocks in enumerate(rows[header_idx + 1 :]):
        tokens = []
        for block in row_blocks:
            cx, _cy = _bbox_center(block.bbox)  # type: ignore[arg-type]
            tokens.append({"text": block.text, "x": cx})
        assigned = assign_cells(tokens, list(columns))
        fields = dict(assigned.get("fields") or {})
        # Ensure every printed canonical exists (blank → None)
        for canon in printed:
            fields.setdefault(canon, None)
        name = fields.get("product_name")
        if not name:
            # Heuristic: leftmost long text token as product
            texts = [b.text for b in row_blocks]
            for t in texts:
                if _PRODUCT_HINT.match(str(t).strip()) and not parse_number(t):
                    fields["product_name"] = str(t).strip()
                    break
        if not fields.get("product_name"):
            continue
        # Preserve blanks explicitly in raw cell list ordered by column index
        raw_by_col: List[Optional[str]] = [None] * (
            max(int(c["col_index"]) for c in columns) + 1
        )
        bboxes: List[Any] = [None] * len(raw_by_col)
        for block in row_blocks:
            cx, _ = _bbox_center(block.bbox)  # type: ignore[arg-type]
            # nearest column
            best = None
            best_d = None
            for c in columns:
                if c.get("x_center") is None:
                    continue
                d = abs(float(c["x_center"]) - cx)
                if best_d is None or d < best_d:
                    best_d = d
                    best = c
            if best is None:
                continue
            idx = int(best["col_index"])
            if raw_by_col[idx] is None:
                raw_by_col[idx] = block.text
                bboxes[idx] = block.bbox
        items.append(
            fields_to_line_item(
                fields,
                printed_fields=printed,
                row_index=i,
                source_cells=raw_by_col,
                cell_bboxes=bboxes,
            )
        )
    if not items:
        return None
    logger.info(
        "STOCK_OCR_BENCHMARK reconstruct=bbox_blocks page=%s rows=%s",
        page.page_number,
        len(items),
    )
    return {
        "strategy": "bbox_blocks",
        "column_map": columns,
        "errors": [],
        "line_items": items,
        "printed_fields": printed,
    }


def reconstruct_from_markdown_fallback(page: OCRPage) -> Optional[Dict[str, Any]]:
    md = page.markdown or page.raw_text or ""
    # Prefer fenced/embedded tables
    if "|" in md:
        # try each table-looking chunk
        chunks = re.split(r"\n{2,}", md)
        for chunk in chunks:
            grid = parse_markdown_table(chunk)
            if len(grid) >= 2 and _is_header_row(grid[0]):
                columns, body, errors = _grid_to_header_and_body(grid)
                if not columns:
                    continue
                printed = sorted(
                    {
                        str(c["canonical"])
                        for c in columns
                        if c.get("canonical") and c["canonical"] != "ignore"
                    }
                )
                items = []
                for i, row in enumerate(body):
                    fields = _assign_grid_row(row, columns)
                    if not fields.get("product_name"):
                        continue
                    items.append(
                        fields_to_line_item(
                            fields,
                            printed_fields=printed,
                            row_index=i,
                            source_cells=list(row),
                        )
                    )
                if items:
                    return {
                        "strategy": "markdown_table",
                        "column_map": columns,
                        "errors": errors,
                        "line_items": items,
                        "printed_fields": printed,
                    }
    return None


def reconstruct_stock_table(ocr: OCRDocumentResult) -> Dict[str, Any]:
    """Build line_items from an OCRDocumentResult; reconcile without rewriting."""
    if ocr.error:
        return {
            "status": "error",
            "error": ocr.error,
            "line_items": [],
            "provider": ocr.provider,
        }

    # Gemini adapter already mapped line_items — use them directly.
    if ocr.provider == "gemini" and ocr.pages:
        extra = ocr.pages[0].extra or {}
        items = list(extra.get("line_items") or [])
        result = {
            "status": "ok",
            "strategy": "gemini_mapped",
            "provider": ocr.provider,
            "line_items": items,
            "column_map": extra.get("column_map") or [],
            "latency_ms": ocr.latency_ms,
            "ocr_confidence": ocr.pages[0].confidence,
        }
        wrapped = {
            "line_items": items,
            "totals": {"sales_value": None, "closing_value": None, "extra": {}},
        }
        apply_stock_reconciliation(wrapped, request_id="ocr-benchmark-gemini")
        result["reconciliation"] = {
            "fail_count": (wrapped.get("totals") or {})
            .get("extra", {})
            .get("stock_reconciliation_fail_count"),
            "pass_count": (wrapped.get("totals") or {})
            .get("extra", {})
            .get("stock_reconciliation_pass_count"),
        }
        result["line_items"] = wrapped.get("line_items") or items
        return result

    all_items: List[Dict[str, Any]] = []
    strategy = None
    column_map: List[Dict[str, Any]] = []
    errors: List[Any] = []
    confidences: List[float] = []

    for page in ocr.pages:
        if page.confidence is not None:
            confidences.append(float(page.confidence))
        built = (
            reconstruct_from_html_tables(page)
            or reconstruct_from_blocks(page)
            or reconstruct_from_markdown_fallback(page)
        )
        if not built:
            continue
        strategy = built["strategy"]
        column_map = built.get("column_map") or column_map
        errors.extend(built.get("errors") or [])
        all_items.extend(built.get("line_items") or [])

    if not all_items:
        return {
            "status": "error",
            "error": "TABLE_RECONSTRUCT_FAILED",
            "provider": ocr.provider,
            "line_items": [],
            "latency_ms": ocr.latency_ms,
        }

    wrapped = {
        "line_items": all_items,
        "totals": {"sales_value": None, "closing_value": None, "extra": {}},
    }
    apply_stock_reconciliation(wrapped, request_id=f"ocr-benchmark-{ocr.provider}")
    avg_conf = sum(confidences) / len(confidences) if confidences else None
    totals_extra = (wrapped.get("totals") or {}).get("extra") or {}
    return {
        "status": "ok",
        "strategy": strategy,
        "provider": ocr.provider,
        "line_items": wrapped.get("line_items") or all_items,
        "column_map": column_map,
        "errors": errors,
        "latency_ms": ocr.latency_ms,
        "ocr_confidence": avg_conf,
        "reconciliation": {
            "fail_count": totals_extra.get("stock_reconciliation_fail_count"),
            "pass_count": totals_extra.get("stock_reconciliation_pass_count"),
        },
        "usage": ocr.usage,
        "model": ocr.model,
    }


def detect_column_shift(item: Dict[str, Any]) -> bool:
    """True when Total In identity suggests a column parking/shift."""
    detail = column_shift_detail(item)
    return bool(detail.get("column_shift_suspected"))


def column_shift_detail(item: Dict[str, Any]) -> Dict[str, Any]:
    """Record suspected column-shift patterns without repairing values."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}
    out: Dict[str, Any] = {
        "column_shift_suspected": False,
        "affected_columns": [],
        "patterns": [],
    }
    try:
        opening = float(item.get("opening_qty") or 0)
        purchase = float(item.get("receipts_qty") or 0)
        sales = float(item.get("sales_qty") or 0)
        sret = (
            float(extra.get("sale_return") or 0)
            if fs.get("sales_return_qty") == "printed"
            else None
        )
        total = (
            float(extra.get("total_stock"))
            if fs.get("total_qty") == "printed" and extra.get("total_stock") is not None
            else None
        )
        pret = (
            float(extra.get("purchase_return") or 0)
            if fs.get("purchase_return_qty") == "printed"
            else 0.0
        )
        closing = item.get("closing_qty")
        closing_f = float(closing) if closing is not None else None
    except (TypeError, ValueError):
        return out

    if total is None:
        return out

    open_pur = opening + purchase
    open_pur_ret = open_pur + (sret or 0.0)

    # Goods Ret cell holds Total In (classic left-shift).
    if sret is not None and abs(sret - total) <= 0.51 and abs(open_pur - total) > 0.51:
        out["column_shift_suspected"] = True
        out["affected_columns"].extend(["sales_return_qty", "total_qty"])
        out["patterns"].append("goods_ret_equals_total_in")

    # Sale cell holds what looks like Total In.
    if abs(sales - total) <= 0.51 and abs(open_pur_ret - total) > 0.51:
        out["column_shift_suspected"] = True
        out["affected_columns"].extend(["sales_qty", "total_qty"])
        out["patterns"].append("sale_equals_total_in")

    # Total does not match open+pur or open+pur+ret, and goods_ret looks like a parked total.
    if (
        abs(total - open_pur) > 0.51
        and abs(total - open_pur_ret) > 0.51
        and sret is not None
        and sret > total
    ):
        out["column_shift_suspected"] = True
        out["affected_columns"].extend(["sales_return_qty", "total_qty", "sales_qty"])
        out["patterns"].append("goods_ret_gt_total_possible_park")

    # Balance missing while identity would pass with shifted sale.
    if closing_f is None and fs.get("closing_qty") == "missing":
        if abs(total - open_pur_ret) > 0.51 and abs(total - open_pur) <= 0.51:
            # total looks like open+pur but goods_ret printed separately — not necessarily shift
            pass
        elif abs(total - open_pur) > 0.51:
            out["column_shift_suspected"] = True
            out["affected_columns"].append("closing_qty")
            out["patterns"].append("balance_missing_with_broken_total_identity")

    # Deduplicate affected columns
    out["affected_columns"] = sorted(set(out["affected_columns"]))
    return out


def validate_row_identity(item: Dict[str, Any]) -> Dict[str, Any]:
    """Validation-only identity checks. Never mutates extracted values."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    opening = float(item.get("opening_qty") or 0)
    purchase = float(item.get("receipts_qty") or 0)
    sales = float(item.get("sales_qty") or 0)
    sret = float(extra.get("sale_return") or 0)
    pret = float(extra.get("purchase_return") or 0)
    total = extra.get("total_stock")
    closing = item.get("closing_qty")
    expected_total = opening + purchase + sret
    expected_closing = expected_total - sales - pret
    printed_total = float(total) if total is not None else None
    printed_closing = float(closing) if closing is not None else None
    total_ok = (
        printed_total is not None and abs(printed_total - expected_total) <= 0.51
    )
    closing_ok = (
        printed_closing is not None
        and abs(printed_closing - expected_closing) <= 0.51
    )
    return {
        "expected_total": expected_total,
        "expected_closing": expected_closing,
        "printed_total": printed_total,
        "printed_closing": printed_closing,
        "total_ok": total_ok,
        "closing_ok": closing_ok,
        "reconciliation_failed": not (total_ok and closing_ok),
    }