"""Schema-agnostic Vision table reader (Phase 2b).

Phase 2b-1: pure mapper + extract_stock_table_vision.
Phase 2b-2: run_vision_table_path wires into extract_sales_statement behind
STOCK_VISION_TABLE (default OFF).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.stock_header_resolver import (
    CANONICAL_FIELDS,
    assign_cells,
    normalize_header,
    parse_number,
    resolve_columns,
)
from services.stock_row_classifier import (
    RowStatus,
    apply_closing_derived,
    classify_row,
    read_row_fields,
)

logger = logging.getLogger(__name__)

# Re-export for callers.
CANONICAL = CANONICAL_FIELDS

_TOP_LEVEL_QTY = {
    "opening_qty": "opening_qty",
    "purchase_qty": "receipts_qty",
    "sales_qty": "sales_qty",
    "closing_qty": "closing_qty",
    "sales_value": "sales_value",
    "closing_value": "closing_value",
    "opening_value": "opening_value",
}

_EXTRA_FIELDS = {
    "lms": "lms",
    "sales_return_qty": "sale_return",
    "purchase_return_qty": "purchase_return",
    "expiry_damage_qty": "exp_damage",
    "total_qty": "total_stock",
    "free_in_qty": "free_in_qty",
    "free_out_qty": "free_out_qty",
    "order_qty": "order_qty",
    "rate": "unit_rate",
    "batch": "batch",
    "purchase_value": "purchase_value",
}


def _boundary_debug_enabled() -> bool:
    """Temporary Vision mapping boundary logs (default ON while diagnosing)."""
    raw = os.getenv("STOCK_VISION_BOUNDARY_DEBUG", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _log_vision_boundary(stage: str, payload: Dict[str, Any], *, request_id: str = "-") -> None:
    if not _boundary_debug_enabled():
        return
    try:
        body = json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        body = str(payload)
    if len(body) > 4000:
        body = body[:4000] + "…"
    logger.info(
        "STOCK_VISION_BOUNDARY request_id=%s stage=%s %s",
        request_id,
        stage,
        body,
    )


def build_table_prompt() -> str:
    fields = ", ".join(CANONICAL_FIELDS)
    return (
        "You are transcribing a pharmaceutical stockist stock statement table. "
        "Do not interpret or calculate.\n"
        "Return JSON with:\n"
        "- tables: array of table objects. If the page has more than one table "
        "side by side or stacked, return each as a separate entry in tables, "
        "left to right, top to bottom. Never merge two tables into one row.\n"
        "  Each table has:\n"
        "  - table_index (0-based)\n"
        "  - x_range: [x0, x1] as fractions of image width (0..1)\n"
        "  - header_rows: every header row, left to right, each cell "
        "{text, col_index, x_center (0..1 of image width)}. Put sub-headers "
        "like Qty / Value on their own row.\n"
        "  - column_count: number of data columns.\n"
        "  - rows: every product row, top to bottom, {row_index, "
        "y_center (0..1 of image height), cells: exactly column_count "
        "strings or null, is_total_row}.\n"
        "  - proposed_mapping: for each col_index your best guess of one of "
        f"[{fields}], with key \"canonical\" and confidence 0..1.\n"
        "- unreadable_cells: [{{table_index, row_index, col_index}}].\n"
        "- stockist_name and statement_period if printed.\n"
        "Rules: copy every number exactly as printed, including decimals and "
        "commas. An empty cell is null, never 0. Keep one entry per column even "
        "if empty so columns never shift. Do not add, balance or correct "
        "numbers. If a digit is unclear, put the cell in unreadable_cells and "
        "give your best reading. Include rows that continue from a previous page.\n"
        "Product names: copy the product name cell exactly as printed "
        "(punctuation, quotes, hyphens, pack size in the name). Do not invent "
        "or invent columns that are not printed — if there is no Closing "
        "header, do not map any column to closing_qty. "
        "Dash-only cells (—, ----) are empty/null. "
        "Balance/Closing/Stock quantity cells must be plain numeric digits "
        "(e.g. 63 or 0). Never invent placeholders like p2, p3, p19, or page "
        "markers for quantity cells — if the Balance digit is unclear, set "
        "the cell to null and list it in unreadable_cells. "
        "Dense stock grids (Opening|Purchase|Goods Ret|Total In|Sale|Purc Ret|"
        "Balance): emit exactly column_count cells per row in left-to-right "
        "header order. Do not skip a blank Goods Ret or Purc Ret cell — use "
        "null so later columns do not shift left. Total In is its own column; "
        "never put Total In digits into Goods Ret or Sale. "
        "Do not emit letterhead, address, period, company, or section-banner "
        "lines (e.g. 'NON MOVING PRODUCT') as product rows; mark those "
        "is_total_row=true or omit them. Emit every real product row, "
        "including rows whose qty cells are all dashes."
    )


def _header_cell_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "required": ["text", "col_index", "x_center"],
        "properties": {
            "text": {"type": "string"},
            "col_index": {"type": "integer"},
            "x_center": {"type": "number"},
        },
    }


def _single_table_schema_properties() -> Dict[str, Any]:
    return {
        "table_index": {"type": "integer"},
        "x_range": {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 2,
            "maxItems": 2,
        },
        "header_rows": {
            "type": "array",
            "items": {
                "type": "array",
                "items": _header_cell_schema(),
            },
        },
        "column_count": {"type": "integer"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["row_index", "y_center", "cells", "is_total_row"],
                "properties": {
                    "row_index": {"type": "integer"},
                    "y_center": {"type": "number"},
                    "cells": {
                        "type": "array",
                        # Vertex Schema rejects JSON union types like ["string","null"].
                        "items": {"type": "string", "nullable": True},
                    },
                    "is_total_row": {"type": "boolean"},
                },
            },
        },
        "proposed_mapping": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["col_index", "canonical", "confidence"],
                "properties": {
                    "col_index": {"type": "integer"},
                    "canonical": {
                        "type": "string",
                        "enum": list(CANONICAL_FIELDS),
                    },
                    "confidence": {"type": "number"},
                },
            },
        },
    }


TABLE_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["tables"],
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "table_index",
                    "x_range",
                    "header_rows",
                    "column_count",
                    "rows",
                    "proposed_mapping",
                ],
                "properties": _single_table_schema_properties(),
            },
        },
        "unreadable_cells": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "table_index": {"type": "integer"},
                    "row_index": {"type": "integer"},
                    "col_index": {"type": "integer"},
                },
            },
        },
        "stockist_name": {"type": "string", "nullable": True},
        "statement_period": {"type": "string", "nullable": True},
    },
}


def _pages_per_call() -> int:
    try:
        return max(1, int(os.getenv("STOCK_VISION_PAGES_PER_CALL", "4")))
    except (TypeError, ValueError):
        return 4


def _vision_max_output_tokens() -> int:
    """Output budget for Vision-table JSON (tall statements need more than model default).

    Vertex rejects maxOutputTokens == 65536 (range is 1..65536 exclusive).
    """
    try:
        val = int(os.getenv("STOCK_VISION_MAX_OUTPUT_TOKENS", "65535"))
    except (TypeError, ValueError):
        val = 65535
    return max(1024, min(val, 65535))


def _split_min_height() -> int:
    try:
        return max(500, int(os.getenv("STOCK_VISION_SPLIT_MIN_HEIGHT", "2800")))
    except (TypeError, ValueError):
        return 2800


def _normalize_product_key(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _gemini_response_text(response: Any) -> str:
    try:
        data = response.json() if hasattr(response, "json") else response
    except Exception:
        data = getattr(response, "_data", None) or {}
    if not isinstance(data, dict):
        return ""
    for cand in data.get("candidates") or []:
        content = (cand or {}).get("content") or {}
        for part in content.get("parts") or []:
            text = (part or {}).get("text")
            if text:
                return str(text)
    return ""


def _gemini_finish_reason(response: Any) -> str:
    try:
        data = response.json() if hasattr(response, "json") else response
    except Exception:
        data = getattr(response, "_data", None) or {}
    if not isinstance(data, dict):
        return ""
    for cand in data.get("candidates") or []:
        reason = (cand or {}).get("finishReason") or (cand or {}).get("finish_reason")
        if reason:
            return str(reason)
    return ""


def _looks_truncated_json(text: str, finish_reason: str = "") -> bool:
    reason = (finish_reason or "").upper()
    if "MAX_TOKEN" in reason or reason in {"LENGTH", "MAX_TOKENS"}:
        return True
    if not text:
        return False
    # Unbalanced braces / ends mid-object → hit output limit mid-row.
    if text.count("{") > text.count("}"):
        return True
    stripped = text.rstrip()
    if stripped.endswith(",") or stripped.endswith("{") or stripped.endswith(":"):
        return True
    return False


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _cell_text(cell: Any) -> Optional[str]:
    """Coerce a Vision cell (string or {text,...} dict) to plain text/null."""
    if cell is None:
        return None
    if isinstance(cell, dict):
        text = cell.get("text")
        if text is None:
            return None
        return str(text)
    return str(cell)


def _normalize_header_rows(
    header_rows: Any, coercions: Dict[str, int]
) -> List[List[Dict[str, Any]]]:
    """Accept nested rows, {cells:[...]} wrappers, or a flat cell list."""
    if not isinstance(header_rows, list) or not header_rows:
        return []
    first = header_rows[0]

    # Schema shape: [[{text, col_index}, ...], ...]
    if isinstance(first, list):
        out: List[List[Dict[str, Any]]] = []
        for row in header_rows:
            if not isinstance(row, list):
                continue
            cells = [c for c in row if isinstance(c, dict)]
            if cells:
                out.append(cells)
        return out

    if not isinstance(first, dict):
        return []

    # Live shape: [{cells: [{text, col_index}, ...]}, ...]
    if "cells" in first and "col_index" not in first:
        coercions["header_rows_cells_wrapper"] = (
            coercions.get("header_rows_cells_wrapper", 0) + 1
        )
        out = []
        for row in header_rows:
            if not isinstance(row, dict):
                continue
            cells = [c for c in (row.get("cells") or []) if isinstance(c, dict)]
            if cells:
                out.append(cells)
        return out

    # Live shape: flat [{text, col_index}, ...] — group on col_index reset.
    coercions["header_rows_flat"] = coercions.get("header_rows_flat", 0) + 1
    rows: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    prev_idx = -1
    for cell in header_rows:
        if not isinstance(cell, dict):
            continue
        try:
            idx = int(cell.get("col_index"))
        except (TypeError, ValueError):
            idx = 0
        if cur and idx < prev_idx and idx == 0:
            rows.append(cur)
            cur = []
        cur.append(cell)
        prev_idx = idx
    if cur:
        rows.append(cur)
    return rows


def _normalize_proposed_mapping(
    proposed: Any, coercions: Dict[str, int]
) -> List[Dict[str, Any]]:
    """Accept canonical / field / mapping keys from live responses."""
    out: List[Dict[str, Any]] = []
    for entry in proposed or []:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("col_index"))
        except (TypeError, ValueError):
            continue
        if "canonical" in entry and entry.get("canonical") is not None:
            canon = entry.get("canonical")
        elif entry.get("field") is not None:
            canon = entry.get("field")
            coercions["proposed_mapping_alias_key"] = (
                coercions.get("proposed_mapping_alias_key", 0) + 1
            )
        elif entry.get("mapping") is not None:
            canon = entry.get("mapping")
            coercions["proposed_mapping_alias_key"] = (
                coercions.get("proposed_mapping_alias_key", 0) + 1
            )
        else:
            canon = "ignore"
        try:
            conf = float(entry.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        out.append(
            {
                "col_index": idx,
                "canonical": str(canon),
                "confidence": conf,
            }
        )
    return out


def _normalize_row_cells(
    cells_raw: Any,
    column_count: int,
    coercions: Dict[str, int],
) -> List[Optional[str]]:
    """Build a dense cell array that preserves physical column positions.

    When Vision emits sparse ``{col_index, text}`` dicts (omitting blanks),
    place each value at its col_index. Never left-compact by iterating order.
    """
    if not isinstance(cells_raw, list):
        return [None] * max(0, int(column_count or 0))

    try:
        width = int(column_count or 0)
    except (TypeError, ValueError):
        width = 0

    indexed: List[Tuple[int, Optional[str]]] = []
    sequential: List[Optional[str]] = []
    saw_col_index = False
    for c in cells_raw:
        if isinstance(c, dict):
            coercions["dict_cells"] = coercions.get("dict_cells", 0) + 1
            text = _cell_text(c)
            if c.get("col_index") is not None:
                try:
                    idx = int(c.get("col_index"))
                except (TypeError, ValueError):
                    sequential.append(text)
                    continue
                saw_col_index = True
                indexed.append((idx, text))
            else:
                sequential.append(text)
        else:
            sequential.append(None if c is None else str(c))

    if saw_col_index:
        coercions["dict_cells_by_col_index"] = (
            coercions.get("dict_cells_by_col_index", 0) + 1
        )
        max_idx = max((i for i, _ in indexed), default=-1)
        width = max(width, max_idx + 1, len(sequential))
        cells: List[Optional[str]] = [None] * width
        # Non-indexed dict/string cells keep their encounter order only when
        # no col_index was supplied for that entry; indexed wins.
        for i, text in enumerate(sequential):
            if i < width and cells[i] is None:
                cells[i] = text
        for idx, text in indexed:
            if 0 <= idx < width:
                cells[idx] = text
        return cells

    cells = list(sequential)
    if width > 0:
        if len(cells) < width:
            cells = cells + [None] * (width - len(cells))
        elif len(cells) > width:
            cells = cells[:width]
    return cells


def _normalize_one_table(
    table: Dict[str, Any], coercions: Dict[str, int]
) -> Dict[str, Any]:
    """Normalize a single table object (legacy or schema)."""
    out = dict(table)
    out["header_rows"] = _normalize_header_rows(table.get("header_rows"), coercions)
    out["proposed_mapping"] = _normalize_proposed_mapping(
        table.get("proposed_mapping"), coercions
    )
    try:
        column_count = int(table.get("column_count") or 0)
    except (TypeError, ValueError):
        column_count = 0
    rows_out: List[Dict[str, Any]] = []
    for row in table.get("rows") or []:
        if not isinstance(row, dict):
            continue
        cells_raw = row.get("cells") or []
        cells = _normalize_row_cells(cells_raw, column_count, coercions)
        item = dict(row)
        item["cells"] = cells
        if item.get("y_center") is None:
            # Optional; band re-read needs it when present.
            pass
        if "is_total_row" not in item:
            item["is_total_row"] = False
            coercions["missing_is_total_row"] = (
                coercions.get("missing_is_total_row", 0) + 1
            )
        rows_out.append(item)
    out["rows"] = rows_out
    return out


def _normalize_vision_table(
    table: Dict[str, Any],
    *,
    request_id: str = "-",
    log: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Coerce live Gemini JSON quirks into the mapper's expected multi-table shape.

    Returns ``(normalized_doc, coercions)``. A non-zero coercion count means the
    response schema was not fully honoured.
    """
    coercions: Dict[str, int] = {}
    if not isinstance(table, dict):
        return {}, coercions

    out = dict(table)
    tables_raw = table.get("tables")
    if isinstance(tables_raw, list) and tables_raw:
        tables_out = []
        for i, t in enumerate(tables_raw):
            if not isinstance(t, dict):
                continue
            nt = _normalize_one_table(t, coercions)
            if nt.get("table_index") is None:
                nt["table_index"] = i
                coercions["missing_table_index"] = (
                    coercions.get("missing_table_index", 0) + 1
                )
            tables_out.append(nt)
        out["tables"] = tables_out
    elif "header_rows" in table or "rows" in table:
        # Legacy single-table top level (saved probe responses / older fixtures).
        coercions["legacy_single_table_root"] = 1
        single = _normalize_one_table(table, coercions)
        single.setdefault("table_index", 0)
        single.setdefault("x_range", [0.0, 1.0])
        out["tables"] = [single]
        # Keep top-level mirrors for older callers inspecting column_count etc.
        out["header_rows"] = single.get("header_rows")
        out["rows"] = single.get("rows")
        out["column_count"] = single.get("column_count")
        out["proposed_mapping"] = single.get("proposed_mapping")
    else:
        out["tables"] = []

    if log:
        logger.info(
            "VISION_TABLE_NORMALIZE request_id=%s coercions=%s",
            request_id,
            json.dumps(coercions, sort_keys=True),
        )
    return out, coercions


def _validate_one_table_shape(obj: Dict[str, Any]) -> Optional[str]:
    if "column_count" not in obj or "rows" not in obj:
        return "INVALID_VISION_JSON"
    try:
        int(obj.get("column_count"))
    except (TypeError, ValueError):
        return "INVALID_VISION_JSON"
    if not isinstance(obj.get("rows"), list):
        return "INVALID_VISION_JSON"
    if "header_rows" not in obj or not isinstance(obj.get("header_rows"), list):
        return "INVALID_VISION_JSON"
    if "proposed_mapping" not in obj or not isinstance(
        obj.get("proposed_mapping"), list
    ):
        return "INVALID_VISION_JSON"
    for row in obj.get("rows") or []:
        if not isinstance(row, dict) or "row_index" not in row or "cells" not in row:
            return "INVALID_VISION_JSON"
        if not isinstance(row.get("cells"), list):
            return "INVALID_VISION_JSON"
    return None


def _validate_table_shape(obj: Dict[str, Any]) -> Optional[str]:
    """Return an error code if the object fails basic schema checks.

    Accepts new ``tables`` shape or legacy single-table top level.
    """
    if not isinstance(obj, dict):
        return "INVALID_VISION_JSON"
    tables = obj.get("tables")
    if isinstance(tables, list) and tables:
        for t in tables:
            if not isinstance(t, dict):
                return "INVALID_VISION_JSON"
            err = _validate_one_table_shape(t)
            if err:
                return err
        return None
    # Legacy single-table root.
    if "header_rows" in obj or "rows" in obj:
        return _validate_one_table_shape(obj)
    return "INVALID_VISION_JSON"


def _image_dimensions(img: bytes) -> Tuple[int, int]:
    """Return (width, height) or (0, 0) if unreadable."""
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(img)) as im:
            return int(im.size[0]), int(im.size[1])
    except Exception:
        return 0, 0


def _needs_vertical_split(img: bytes) -> bool:
    """Tall / high-res single pages often exceed output token budget."""
    w, h = _image_dimensions(img)
    if h <= 0:
        return False
    if h >= _split_min_height():
        return True
    # Wide phone-photo sheets that are also tall enough to pack many rows.
    return w >= 3000 and h >= 2200


def _vertical_halves(img: bytes) -> List[bytes]:
    """Split into overlapping top/bottom crops (JPEG/PNG matching input)."""
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(img)) as im:
            w, h = im.size
            if h < 400:
                return [img]
            overlap = int(h * 0.10)
            mid = h // 2
            top_box = (0, 0, w, min(h, mid + overlap))
            bot_box = (0, max(0, mid - overlap), w, h)
            fmt = "JPEG" if img[:2] == b"\xff\xd8" else "PNG"
            out: List[bytes] = []
            for box in (top_box, bot_box):
                crop = im.crop(box)
                buf = io.BytesIO()
                save_kw: Dict[str, Any] = {}
                if fmt == "JPEG":
                    save_kw["quality"] = 92
                    if crop.mode not in ("RGB", "L"):
                        crop = crop.convert("RGB")
                crop.save(buf, format=fmt, **save_kw)
                out.append(buf.getvalue())
            return out
    except Exception:
        return [img]


def render_pdf_pages(file_bytes: bytes, *, zoom: float = 2.5) -> List[bytes]:
    """fitz page renders at the given zoom (PNG bytes)."""
    import fitz

    images: List[bytes] = []
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception:
        return []
    try:
        for page in doc:
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                images.append(pix.tobytes("png"))
            except Exception:
                continue
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return images


def extract_stock_table_vision(
    images: List[bytes],
    ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One Gemini call for up to STOCK_VISION_PAGES_PER_CALL images.

    Returns the parsed table dict, or
    ``{"error": "INVALID_VISION_JSON", "raw": "<first 2000 chars>"}``.
    Does not retry on invalid JSON.
    """
    ctx = dict(ctx or {})
    if not images:
        return {"error": "INVALID_VISION_JSON", "raw": ""}

    limit = _pages_per_call()
    batch = list(images[:limit])
    prompt = str(ctx.get("prompt") or build_table_prompt())

    from app import call_gemini_with_quota, get_current_model_config

    model_config = get_current_model_config()
    max_tokens = _vision_max_output_tokens()
    parts: List[Dict[str, Any]] = [{"text": prompt}]
    for img in batch:
        mime = "image/png"
        if img[:2] == b"\xff\xd8":
            mime = "image/jpeg"
        # Do not set per-part media_resolution: flash-lite rejects it
        # ("Per part media resolution is not supported for this Model").
        parts.append(
            {
                "inline_data": {
                    "mime_type": mime,
                    "data": base64.b64encode(img).decode("ascii"),
                }
            }
        )

    gen_cfg: Dict[str, Any] = {
        "temperature": 0,
        "maxOutputTokens": max_tokens,
        "responseMimeType": "application/json",
        "responseSchema": TABLE_RESPONSE_SCHEMA,
        "thinkingConfig": {"thinkingBudget": 0, "includeThoughts": False},
    }
    # Generation-level mediaResolution is optional; skip for lite models.
    model_name = str(model_config.get("name") or "").lower()
    if "lite" not in model_name:
        gen_cfg["mediaResolution"] = "MEDIA_RESOLUTION_HIGH"

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": gen_cfg,
    }

    # Prefer sales Vertex path when available (quota + cooldown); fall back to app.
    response = None
    try:
        from services.sales_extraction_runtime import sales_generate_content_via_vertex

        response = sales_generate_content_via_vertex(
            model=model_config["name"],
            payload=payload,
            timeout=int(model_config.get("timeout") or 120),
            label=str(ctx.get("label") or "stock_vision_table"),
        )
    except Exception:
        response = call_gemini_with_quota(
            model=model_config["name"],
            payload=payload,
            timeout=int(model_config.get("timeout") or 120),
            request_type="vision",
        )

    raw_text = _gemini_response_text(response)
    finish_reason = _gemini_finish_reason(response)
    parsed = _extract_json_object(raw_text)
    if parsed is None or _validate_table_shape(parsed) is not None:
        truncated = _looks_truncated_json(raw_text or "", finish_reason)
        logger.info(
            "VISION_TABLE_EXTRACT error=INVALID_VISION_JSON truncated=%s "
            "finish_reason=%s max_output_tokens=%s raw_len=%s",
            truncated,
            finish_reason or "-",
            max_tokens,
            len(raw_text or ""),
        )
        return {
            "error": "INVALID_VISION_JSON",
            "raw": (raw_text or "")[:2000],
            "truncated": truncated,
            "finish_reason": finish_reason or None,
        }
    return parsed


def _flatten_header_cells(
    header_rows: Sequence[Any], column_count: int
) -> List[Dict[str, Any]]:
    """Combine multi-row headers into resolve_columns cells."""
    rows: List[List[Dict[str, Any]]] = []
    for raw_row in header_rows or []:
        if not isinstance(raw_row, list):
            continue
        cells = [c for c in raw_row if isinstance(c, dict)]
        if cells:
            rows.append(cells)
    if not rows:
        return []

    by_index: Dict[int, Dict[str, Any]] = {}
    for cell in rows[0]:
        idx = int(cell.get("col_index") if cell.get("col_index") is not None else -1)
        if idx < 0:
            continue
        by_index[idx] = {
            "text": str(cell.get("text") or ""),
            "col_index": idx,
            "x_center": cell.get("x_center"),
            "subheader_text": None,
        }

    if len(rows) >= 2:
        for cell in rows[1]:
            idx = int(cell.get("col_index") if cell.get("col_index") is not None else -1)
            if idx < 0:
                continue
            sub = str(cell.get("text") or "")
            if idx in by_index:
                by_index[idx]["subheader_text"] = sub or None
                # Prefer x_center from the data-aligned sub-row when present.
                if cell.get("x_center") is not None:
                    by_index[idx]["x_center"] = cell.get("x_center")
            else:
                by_index[idx] = {
                    "text": sub,
                    "col_index": idx,
                    "x_center": cell.get("x_center"),
                    "subheader_text": None,
                }

    # Ensure column_count slots exist (empty → ignore later).
    for idx in range(int(column_count or 0)):
        by_index.setdefault(
            idx,
            {
                "text": "",
                "col_index": idx,
                "x_center": None,
                "subheader_text": None,
            },
        )
    return [by_index[i] for i in sorted(by_index)]


def _x_order_violation(header_cells: List[Dict[str, Any]]) -> bool:
    last_x: Optional[float] = None
    last_i: Optional[int] = None
    for cell in sorted(header_cells, key=lambda c: int(c.get("col_index") or 0)):
        x = cell.get("x_center")
        idx = int(cell.get("col_index") or 0)
        if x is None:
            continue
        try:
            xf = float(x)
        except (TypeError, ValueError):
            continue
        if last_x is not None and last_i is not None and idx > last_i and xf < last_x:
            return True
        last_x = xf
        last_i = idx
    return False


def _merge_proposed_mapping(
    columns: List[Dict[str, Any]],
    proposed: Sequence[Dict[str, Any]],
    errors: List[str],
) -> List[Dict[str, Any]]:
    """Resolver is authoritative; model fills low-confidence ignore only.

    Never invent return/expiry fields unless the printed header text itself
    resolves to that group (no phantom SaleRet column).
    """
    from services.stock_header_resolver import _pick_group, normalize_header

    _HEADER_MATCH_REQUIRED = frozenset(
        {
            "sales_return_qty",
            "purchase_return_qty",
            "expiry_damage_qty",
            "closing_qty",
            "closing_value",
        }
    )
    _GROUP_FOR_CANON = {
        "sales_return_qty": "sales_return",
        "purchase_return_qty": "purchase_return",
        "expiry_damage_qty": "expiry_damage",
        "closing_qty": "closing",
        "closing_value": "closing",
    }

    by_prop = {}
    for entry in proposed or []:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("col_index"))
        except (TypeError, ValueError):
            continue
        canon = str(entry.get("canonical") or "ignore")
        try:
            conf = float(entry.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        by_prop[idx] = (canon, conf)

    out: List[Dict[str, Any]] = []
    for col in columns:
        item = dict(col)
        idx = int(col.get("col_index") or 0)
        resolver_canon = str(col.get("canonical") or "ignore")
        try:
            resolver_conf = float(col.get("confidence") or 0.0)
        except (TypeError, ValueError):
            resolver_conf = 0.0
        item["source"] = "resolver"
        if idx in by_prop:
            model_canon, model_conf = by_prop[idx]
            header_ok = True
            if str(col.get("reason") or "") == "date_or_near_expiry":
                # Date headers (Expiry / M.EXP) must never become expiry_damage_qty.
                header_ok = False
            elif model_canon in _HEADER_MATCH_REQUIRED:
                header_norm = normalize_header(col.get("header_text"))
                group, _gconf, _reason = _pick_group(header_norm)
                header_ok = _GROUP_FOR_CANON.get(model_canon) == group
                # Bare "expiry" is a date column in resolve_columns; do not
                # revive it as damage qty via proposed_mapping.
                if model_canon == "expiry_damage_qty" and header_norm in {
                    "expiry",
                    "expiry date",
                    "exp date",
                    "exp dt",
                }:
                    header_ok = False
            header_norm = normalize_header(col.get("header_text"))
            # LMS must never be remapped to opening/purchase by the model.
            if header_norm == "lms" or resolver_canon == "lms":
                if model_canon in {
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                }:
                    header_ok = False
                    errors.append(f"LMS_MODEL_BLOCK col={idx}")
            if (
                resolver_canon == "ignore"
                and resolver_conf < 0.5
                and model_canon in CANONICAL_FIELDS
                and model_canon != "ignore"
                and header_ok
            ):
                item["canonical"] = model_canon
                item["confidence"] = model_conf
                item["source"] = "model"
                item["reason"] = "proposed_mapping"
            elif (
                resolver_conf >= 0.8
                and model_canon in CANONICAL_FIELDS
                and model_canon != "ignore"
                and model_canon != resolver_canon
            ):
                errors.append(f"MAPPING_DISAGREE col={idx}")
        out.append(item)
    return out


_GARBAGE_QTY_TOKEN = re.compile(r"^[pP]\d{1,4}$")


def _maybe_repair_shifted_qty_row(
    item: Dict[str, Any],
    *,
    printed_fields: Optional[Sequence[str]] = None,
) -> bool:
    """Repair a common Vision left-shift on dense Monthly SS rows.

    Symptom: Purchase/Goods Ret unread, Goods Ret cell holds Total In, Total
    cell holds Sale, Sale/Balance empty or garbage. Never invents Balance —
    only reassigns already-read numbers to the correct canonical fields.
    """
    printed = set(printed_fields or [])
    if "total_qty" not in printed or "sales_qty" not in printed:
        return False
    extra = item.setdefault("extra", {})
    fs = extra.setdefault("field_source", {})
    if not isinstance(fs, dict):
        return False

    # Only when Sale is missing and Total In appears parked in sales_return.
    if fs.get("sales_qty") == "printed" and item.get("sales_qty") not in (None, ""):
        return False
    if fs.get("closing_qty") == "printed" and item.get("closing_qty") not in (None, ""):
        # Closing was read — do not reshuffle; risk corrupting a good row.
        return False
    if fs.get("sales_return_qty") != "printed":
        return False
    if fs.get("total_qty") != "printed":
        return False

    try:
        sret = float(extra.get("sale_return"))
        total = float(extra.get("total_stock"))
    except (TypeError, ValueError):
        return False
    if sret <= total:
        return False

    # Reassign: Total In <- old sales_return; Sale <- old total; Goods Ret unknown.
    logger.info(
        "STOCK_VISION_SHIFT_REPAIR product=%s sale_return=%s total=%s "
        "-> total=%s sales=%s",
        str(item.get("product_name") or "")[:60],
        sret,
        total,
        sret,
        total,
    )
    extra["total_stock"] = sret
    item["sales_qty"] = total
    fs["total_qty"] = "printed"
    fs["sales_qty"] = "printed"
    extra["sale_return"] = None
    fs["sales_return_qty"] = "missing"
    extra["shift_repair"] = {
        "from_sale_return": sret,
        "from_total": total,
        "to_total": sret,
        "to_sales": total,
    }
    return True


def _is_garbage_qty_token(raw: Any) -> bool:
    """True for Vision placeholders like p2/p3/p19 mistaken for Balance qty."""
    if raw is None:
        return False
    text = str(raw).strip()
    if not text:
        return False
    return bool(_GARBAGE_QTY_TOKEN.match(text))


def _empty_item() -> Dict[str, Any]:
    return {
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


def _apply_fields_to_item(
    fields: Dict[str, Optional[str]],
    *,
    unreadable_cols: set,
    col_to_canon: Dict[int, str],
    row_index: int,
    printed_fields: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    item = _empty_item()
    field_source: Dict[str, str] = {}
    unreadable_fields: List[str] = []
    printed = set(printed_fields or [])

    for canon, raw in fields.items():
        if canon == "ignore":
            continue
        is_name = canon in {"product_name", "pack", "batch"}
        missing = raw is None or str(raw).strip() == ""

        if is_name:
            text = None if missing else str(raw).strip()
            if canon == "product_name":
                item["product_name"] = text
            elif canon == "pack":
                item["packing"] = text
            else:
                item.setdefault("extra", {})["batch"] = text
            field_source[canon] = "missing" if missing else "printed"
            continue

        # Numeric canonicals — reject Vision placeholders like Balance="p3".
        if (not isinstance(raw, (int, float))) and _is_garbage_qty_token(raw):
            field_source[canon] = "missing"
            if canon not in unreadable_fields:
                unreadable_fields.append(canon)
            if canon in _TOP_LEVEL_QTY:
                if canon == "closing_qty":
                    item["closing_qty"] = None
                else:
                    item[_TOP_LEVEL_QTY[canon]] = 0.0
            elif canon in _EXTRA_FIELDS:
                item.setdefault("extra", {})[_EXTRA_FIELDS[canon]] = None
            continue

        parsed = parse_number(raw, allow_internal_spaces=True)

        if missing or parsed is None:
            src = "missing"
            if canon == "closing_qty":
                item["closing_qty"] = None
            elif canon in _TOP_LEVEL_QTY:
                item[_TOP_LEVEL_QTY[canon]] = 0.0
            elif canon in _EXTRA_FIELDS:
                item.setdefault("extra", {})[_EXTRA_FIELDS[canon]] = None
            field_source[canon] = src
            continue

        value = float(parsed)
        src = "printed"
        field_source[canon] = src

        if canon in _TOP_LEVEL_QTY:
            item[_TOP_LEVEL_QTY[canon]] = value
            if canon == "opening_value":
                item.setdefault("extra", {})["opening_value"] = value
        elif canon in _EXTRA_FIELDS:
            item.setdefault("extra", {})[_EXTRA_FIELDS[canon]] = value

    # Columns not printed on the document stay missing — never invent them.
    for absent in (
        "closing_qty",
        "sales_return_qty",
        "purchase_return_qty",
        "expiry_damage_qty",
        "total_qty",
        "free_in_qty",
        "free_out_qty",
    ):
        if printed and absent not in printed and absent not in field_source:
            field_source[absent] = "missing"
            if absent == "closing_qty":
                item["closing_qty"] = None
            elif absent in _EXTRA_FIELDS:
                item.setdefault("extra", {})[_EXTRA_FIELDS[absent]] = None

    for col_idx, canon in col_to_canon.items():
        if col_idx in unreadable_cols and canon and canon != "ignore":
            if canon not in unreadable_fields:
                unreadable_fields.append(canon)

    extra = item.setdefault("extra", {})
    extra["field_source"] = field_source
    extra["unreadable_fields"] = unreadable_fields
    extra["vision_row_index"] = int(row_index)
    return item


_PRODUCT_HEADER_HINTS = frozenset(
    {
        "product",
        "product name",
        "item",
        "item name",
        "item description",
        "particulars",
        "description",
        "medicine name",
        "drug name",
    }
)

# Letterhead / banner lines Vision sometimes emits as fake product rows.
_NON_PRODUCT_ROW_NAME = re.compile(
    r"(?ix)^\s*(?:"
    r"from\s+\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|to\s+\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    # Period fragments: "01-08-2026 to 31-08-2026" or OCR "6 to 31-08-2026"
    r"|(?:from\s+)?\d{1,2}(?:[-/]\d{1,2}(?:[-/]\d{2,4})?)?\s+to\s+\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|last\s*6\s*months"
    r"|non\s*moving\s*product"
    r"|stock[_\s-]*statement"
    r"|undo|redo|sheet\s*\d+"
    r"|edit\s*selection"
    r")\b"
    r"|(?:apt\.|nagar|wadi|road|street|surat|mumbai|pincode|pin\s*code)"
    r"|(?:^\s*(?:nt|to)\s*$)"
)

# Never a product name, even if Vision filled zeros as "printed".
_ALWAYS_NON_PRODUCT_NAME = re.compile(
    r"(?ix)"
    r"(?:=>)|"  # OCR arrow junk from logo/header lines
    r"\b(?:zandra|division)\b|"
    r"\bdivi\b|"
    r"\([^)]*\bdiv(?:i|ision)?\b[^)]*\)|"
    r"\bhimalaya\b\s*\("
)

# Company / stockist letterhead when the row has no real movement quantities.
_COMPANY_LETTERHEAD = re.compile(
    r"(?ix)\b(?:medicals?|pharma(?:ceuticals?)?|distributors?|agency|"
    r"pvt\.?\s*ltd|private\s+limited)\b"
)


def _is_non_product_vision_row(item: Dict[str, Any]) -> bool:
    """True for letterhead/address/period/section-banner rows, not products."""
    if not isinstance(item, dict):
        return True
    name = str(item.get("product_name") or "").strip()
    if not name:
        return True
    if _NON_PRODUCT_ROW_NAME.search(name):
        return True
    if _ALWAYS_NON_PRODUCT_NAME.search(name):
        return True

    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}
    qty_keys = (
        "opening_qty",
        "purchase_qty",
        "sales_qty",
        "closing_qty",
        "total_qty",
    )
    any_printed_qty = any(fs.get(k) == "printed" for k in qty_keys)
    has_nonzero = any(
        v not in (None, "", 0, 0.0)
        for v in (
            item.get("opening_qty"),
            item.get("receipts_qty"),
            item.get("sales_qty"),
            item.get("closing_qty"),
        )
    )
    if has_nonzero:
        return False
    if not any_printed_qty and name.count(",") >= 2:
        return True
    if not any_printed_qty and _COMPANY_LETTERHEAD.search(name):
        return True
    return False


def _is_product_header_text(text: Any) -> bool:
    norm = normalize_header(text)
    if not norm:
        return False
    if norm in _PRODUCT_HEADER_HINTS:
        return True
    # "Product Nand" etc.
    return any(norm.startswith(h + " ") or norm.endswith(" " + h) for h in ("product", "item"))


def _dual_column_split_col(
    header_cells: List[Dict[str, Any]],
    resolve_errors: Sequence[Any],
) -> Optional[int]:
    """Return col_index where the second product column starts, or None."""
    product_cols: List[int] = []
    for cell in sorted(header_cells, key=lambda c: int(c.get("col_index") or 0)):
        if _is_product_header_text(cell.get("text")):
            try:
                product_cols.append(int(cell.get("col_index")))
            except (TypeError, ValueError):
                continue
    if len(product_cols) >= 2:
        return product_cols[1]

    # Resolver duplicate signal.
    for err in resolve_errors or []:
        if isinstance(err, dict):
            if (
                err.get("code") == "DUPLICATE_CANONICAL"
                and err.get("canonical") == "product_name"
                and err.get("col_index") is not None
            ):
                try:
                    return int(err["col_index"])
                except (TypeError, ValueError):
                    pass
        else:
            text = str(err)
            if "DUPLICATE_CANONICAL" in text and "product_name" in text:
                m = re.search(r"col[_\s=]*(\d+)", text)
                if m:
                    return int(m.group(1))
    return None


def _slice_table_columns(table: Dict[str, Any], start: int, end: Optional[int]) -> Dict[str, Any]:
    """Keep columns [start, end) and reindex col_index to 0..n-1."""
    try:
        column_count = int(table.get("column_count") or 0)
    except (TypeError, ValueError):
        column_count = 0
    if end is None:
        end = column_count
    start = max(0, int(start))
    end = max(start, int(end))
    width = end - start

    def _remap_header_rows(header_rows: Any) -> List[List[Dict[str, Any]]]:
        out_rows: List[List[Dict[str, Any]]] = []
        for row in header_rows or []:
            if not isinstance(row, list):
                continue
            new_row = []
            for cell in row:
                if not isinstance(cell, dict):
                    continue
                try:
                    idx = int(cell.get("col_index"))
                except (TypeError, ValueError):
                    continue
                if start <= idx < end:
                    nc = dict(cell)
                    nc["col_index"] = idx - start
                    new_row.append(nc)
            if new_row:
                out_rows.append(new_row)
        return out_rows

    rows_out = []
    for row in table.get("rows") or []:
        if not isinstance(row, dict):
            continue
        cells = list(row.get("cells") or [])
        sliced = cells[start:end]
        if len(sliced) < width:
            sliced = sliced + [None] * (width - len(sliced))
        nr = dict(row)
        nr["cells"] = sliced
        rows_out.append(nr)

    proposed = []
    for entry in table.get("proposed_mapping") or []:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("col_index"))
        except (TypeError, ValueError):
            continue
        if start <= idx < end:
            ne = dict(entry)
            ne["col_index"] = idx - start
            proposed.append(ne)

    return {
        "table_index": table.get("table_index"),
        "x_range": table.get("x_range"),
        "header_rows": _remap_header_rows(table.get("header_rows")),
        "column_count": width,
        "rows": rows_out,
        "proposed_mapping": proposed,
        "unreadable_cells": [
            c
            for c in (table.get("unreadable_cells") or [])
            if isinstance(c, dict)
            and start <= int(c.get("col_index") or -1) < end
        ],
    }


def _maybe_split_wide_table(
    table: Dict[str, Any],
    *,
    request_id: str = "-",
) -> List[Dict[str, Any]]:
    """If one wide table has a second product column, split into two halves."""
    try:
        column_count = int(table.get("column_count") or 0)
    except (TypeError, ValueError):
        column_count = 0
    header_cells = _flatten_header_cells(table.get("header_rows") or [], column_count)
    if not header_cells:
        return [table]
    resolved = resolve_columns(header_cells)
    split_at = _dual_column_split_col(header_cells, resolved.get("errors") or [])
    if split_at is None or split_at <= 0 or split_at >= column_count:
        return [table]

    left = _slice_table_columns(table, 0, split_at)
    right = _slice_table_columns(table, split_at, column_count)
    left["table_index"] = int(table.get("table_index") or 0)
    right["table_index"] = int(table.get("table_index") or 0) + 1
    logger.info(
        "VISION_TABLE_SPLIT request_id=%s halves=2 split_col=%s",
        request_id,
        split_at,
    )
    return [left, right]


def _map_single_table(
    table: Dict[str, Any],
    carry_header: Optional[Dict[str, Any]] = None,
    *,
    request_id: str = "-",
) -> Dict[str, Any]:
    """Map one normalized table object onto line_items."""
    errors: List[str] = []
    try:
        column_count = int(table.get("column_count") or 0)
    except (TypeError, ValueError):
        column_count = 0

    header_rows = table.get("header_rows") or []
    header_used: Optional[Dict[str, Any]] = None
    columns: List[Dict[str, Any]] = []

    if header_rows:
        header_cells = _flatten_header_cells(header_rows, column_count)
        if _x_order_violation(header_cells):
            errors.append("STRUCTURAL_ERROR")
            return {
                "line_items": [],
                "column_map": [],
                "errors": errors,
                "header_used": None,
                "totals_rows": [],
            }
        resolved = resolve_columns(header_cells)
        columns = list(resolved.get("columns") or [])
        for err in resolved.get("errors") or []:
            code = err.get("code") if isinstance(err, dict) else str(err)
            if code:
                errors.append(str(code))
        columns = _merge_proposed_mapping(
            columns, table.get("proposed_mapping") or [], errors
        )
        header_used = {
            "column_count": column_count,
            "columns": columns,
            "header_cells": header_cells,
        }
    elif carry_header and isinstance(carry_header, dict):
        carried_count = int(carry_header.get("column_count") or 0)
        if carried_count != column_count:
            errors.append("STRUCTURAL_ERROR")
            return {
                "line_items": [],
                "column_map": [],
                "errors": errors,
                "header_used": carry_header,
                "totals_rows": [],
            }
        columns = list(carry_header.get("columns") or [])
        header_used = carry_header
    else:
        errors.append("STRUCTURAL_ERROR")
        return {
            "line_items": [],
            "column_map": [],
            "errors": errors,
            "header_used": None,
            "totals_rows": [],
        }

    col_to_canon = {
        int(c["col_index"]): str(c.get("canonical") or "ignore")
        for c in columns
        if isinstance(c, dict)
    }
    printed_fields = sorted(
        {
            str(c.get("canonical"))
            for c in columns
            if isinstance(c, dict)
            and c.get("canonical")
            and c.get("canonical") != "ignore"
        }
    )

    _log_vision_boundary(
        "resolved_headers",
        {
            "column_count": column_count,
            "columns": [
                {
                    "col_index": int(c.get("col_index") or 0),
                    "header_text": c.get("header_text"),
                    "canonical": c.get("canonical"),
                    "source": c.get("source"),
                }
                for c in columns
                if isinstance(c, dict)
            ],
        },
        request_id=request_id,
    )

    unreadable = set()
    for cell in table.get("unreadable_cells") or []:
        if not isinstance(cell, dict):
            continue
        try:
            unreadable.add((int(cell["row_index"]), int(cell["col_index"])))
        except (KeyError, TypeError, ValueError):
            continue

    line_items: List[Dict[str, Any]] = []
    totals_rows: List[Dict[str, Any]] = []

    for row in table.get("rows") or []:
        if not isinstance(row, dict):
            continue
        try:
            row_index = int(row.get("row_index"))
        except (TypeError, ValueError):
            continue
        cells = list(row.get("cells") or [])
        if len(cells) < column_count:
            cells = cells + [None] * (column_count - len(cells))
        elif len(cells) > column_count:
            cells = cells[:column_count]

        physical_pairs = {
            str(i): cells[i] for i in range(column_count) if i < len(cells)
        }
        _log_vision_boundary(
            "physical_cells",
            {
                "row_index": row_index,
                "product_text": cells[0] if cells else None,
                "physical_column_index_value_pairs": physical_pairs,
            },
            request_id=request_id,
        )

        tokens = [{"col_index": i, "text": cells[i]} for i in range(column_count)]
        assigned = assign_cells(tokens, columns)
        fields = dict(assigned.get("fields") or {})
        for c in columns:
            canon = str(c.get("canonical") or "ignore")
            if canon != "ignore" and canon not in fields:
                fields[canon] = None

        _log_vision_boundary(
            "mapped_semantic_fields",
            {
                "row_index": row_index,
                "product_text": cells[0] if cells else None,
                "fields": {
                    "lms": fields.get("lms"),
                    "opening_qty": fields.get("opening_qty"),
                    "purchase_qty": fields.get("purchase_qty"),
                    "sales_qty": fields.get("sales_qty"),
                    "closing_qty": fields.get("closing_qty"),
                    "closing_value": fields.get("closing_value"),
                },
            },
            request_id=request_id,
        )

        unread_cols = {col for (r, col) in unreadable if r == row_index}
        _log_vision_boundary(
            "fields_to_line_item_input",
            {
                "row_index": row_index,
                "note": "vision path uses _apply_fields_to_item (not fields_to_line_item)",
                "fields": fields,
            },
            request_id=request_id,
        )
        item = _apply_fields_to_item(
            fields,
            unreadable_cols=unread_cols,
            col_to_canon=col_to_canon,
            row_index=row_index,
            printed_fields=printed_fields,
        )
        _log_vision_boundary(
            "fields_to_line_item_output",
            {
                "row_index": row_index,
                "product_name": item.get("product_name"),
                "lms": (item.get("extra") or {}).get("lms"),
                "opening_qty": item.get("opening_qty"),
                "receipts_qty": item.get("receipts_qty"),
                "sales_qty": item.get("sales_qty"),
                "closing_qty": item.get("closing_qty"),
                "closing_value": item.get("closing_value"),
                "field_source": (item.get("extra") or {}).get("field_source"),
            },
            request_id=request_id,
        )
        # Preserve source cells for diagnostics (never overwrite later).
        item.setdefault("extra", {})["vision_raw_cells"] = list(cells[:column_count])
        item["extra"]["vision_column_headers"] = [
            str(c.get("header_text") or "") for c in columns
        ]
        item["extra"]["physical_cells"] = physical_pairs
        if row.get("y_center") is not None:
            try:
                item.setdefault("extra", {})["y_center"] = float(row.get("y_center"))
            except (TypeError, ValueError):
                pass
        if row.get("is_total_row"):
            totals_rows.append(item)
        else:
            if _is_non_product_vision_row(item):
                continue
            _maybe_repair_shifted_qty_row(item, printed_fields=printed_fields)
            apply_closing_derived(
                item, printed_fields=printed_fields, request_id=request_id
            )
            line_items.append(item)

    return {
        "line_items": line_items,
        "column_map": columns,
        "errors": errors,
        "header_used": header_used,
        "totals_rows": totals_rows,
        "stockist_name": table.get("stockist_name"),
        "statement_period": table.get("statement_period"),
    }


def map_vision_table(
    table: Dict[str, Any],
    carry_header: Optional[Dict[str, Any]] = None,
    *,
    request_id: str = "-",
) -> Dict[str, Any]:
    """Map a Vision table JSON onto line_items using the shared header resolver.

    Supports multi-table responses and a deterministic dual-column fallback split.
    """
    if not isinstance(table, dict) or table.get("error"):
        return {
            "line_items": [],
            "column_map": [],
            "errors": [str((table or {}).get("error") or "INVALID_VISION_JSON")],
            "header_used": None,
            "totals_rows": [],
            "tables_found": 0,
            "coercions": {},
        }

    req_id = request_id or str(
        (table.get("_request_id") if isinstance(table, dict) else None) or "-"
    )
    normalized, coercions = _normalize_vision_table(table, request_id=req_id)
    tables = list(normalized.get("tables") or [])

    # Dual-column fallback when the model returned one wide table.
    expanded: List[Dict[str, Any]] = []
    split_events = 0
    for t in tables:
        halves = _maybe_split_wide_table(t, request_id=req_id)
        if len(halves) > 1:
            split_events += 1
        expanded.extend(halves)

    all_items: List[Dict[str, Any]] = []
    all_totals: List[Dict[str, Any]] = []
    all_errors: List[str] = []
    column_map: List[Dict[str, Any]] = []
    header_used = None
    seen_keys: Dict[str, int] = {}
    rows_per_table: List[int] = []

    for t_idx, t in enumerate(expanded):
        # Carry header only when this table lacks headers (continuation crop).
        carry = carry_header if not (t.get("header_rows") or []) else None
        mapped = _map_single_table(t, carry_header=carry, request_id=req_id)
        all_errors.extend(str(e) for e in (mapped.get("errors") or []))
        if mapped.get("header_used") and header_used is None:
            header_used = mapped["header_used"]
            column_map = list(mapped.get("column_map") or [])
        elif mapped.get("column_map") and not column_map:
            column_map = list(mapped.get("column_map") or [])

        table_items = list(mapped.get("line_items") or [])
        rows_per_table.append(len(table_items))
        for item in table_items:
            extra = item.setdefault("extra", {})
            extra["table_index"] = int(t.get("table_index") if t.get("table_index") is not None else t_idx)
            key = _normalize_product_key(item.get("product_name"))
            if key:
                if key in seen_keys:
                    flags = list(extra.get("flags") or [])
                    if "DUPLICATE_PRODUCT_IN_PAGE" not in flags:
                        flags.append("DUPLICATE_PRODUCT_IN_PAGE")
                    extra["flags"] = flags
                    all_errors.append("DUPLICATE_PRODUCT_IN_PAGE")
                    # Also flag the earlier occurrence.
                    prev = seen_keys[key]
                    if 0 <= prev < len(all_items):
                        pextra = all_items[prev].setdefault("extra", {})
                        pflags = list(pextra.get("flags") or [])
                        if "DUPLICATE_PRODUCT_IN_PAGE" not in pflags:
                            pflags.append("DUPLICATE_PRODUCT_IN_PAGE")
                        pextra["flags"] = pflags
                else:
                    seen_keys[key] = len(all_items)
            all_items.append(item)
        all_totals.extend(mapped.get("totals_rows") or [])

    total_in_layout = False
    for col in column_map or []:
        if not isinstance(col, dict):
            continue
        if col.get("canonical") != "total_qty":
            continue
        ht = normalize_header(col.get("header_text") or "")
        if "in" in ht.split() or "total in" in ht:
            total_in_layout = True
            break
    if total_in_layout:
        for item in all_items:
            if isinstance(item, dict):
                item.setdefault("extra", {})["total_in_layout"] = True
                item["extra"]["column_map"] = column_map

    return {
        "line_items": all_items,
        "column_map": column_map,
        "errors": all_errors,
        "header_used": header_used,
        "totals_rows": all_totals,
        "stockist_name": normalized.get("stockist_name"),
        "statement_period": normalized.get("statement_period"),
        "tables_found": len(expanded),
        "rows_per_table": rows_per_table,
        "coercions": coercions,
        "split_events": split_events,
        "total_in_layout": total_in_layout,
    }


def _numeric_fingerprint(item: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        item.get("opening_qty"),
        item.get("receipts_qty"),
        item.get("sales_qty"),
        item.get("closing_qty"),
        item.get("sales_value"),
        item.get("closing_value"),
    )


def dedupe_crop_overlap_rows(
    items: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Dedupe tall-crop overlap: same name + same numbers → keep one.

    Same name but different numbers → keep both, flag CROP_OVERLAP_CONFLICT.
    """
    errors: List[str] = []
    out: List[Dict[str, Any]] = []
    by_key: Dict[str, List[int]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = _normalize_product_key(item.get("product_name"))
        if not key:
            out.append(item)
            continue
        idxs = by_key.get(key) or []
        matched_exact = False
        conflict = False
        for idx in idxs:
            prev = out[idx]
            if _numeric_fingerprint(prev) == _numeric_fingerprint(item):
                matched_exact = True
                break
            conflict = True
        if matched_exact:
            continue
        if conflict:
            extra = item.setdefault("extra", {})
            flags = list(extra.get("flags") or [])
            if "CROP_OVERLAP_CONFLICT" not in flags:
                flags.append("CROP_OVERLAP_CONFLICT")
            extra["flags"] = flags
            errors.append("CROP_OVERLAP_CONFLICT")
            for idx in idxs:
                pextra = out[idx].setdefault("extra", {})
                pflags = list(pextra.get("flags") or [])
                if "CROP_OVERLAP_CONFLICT" not in pflags:
                    pflags.append("CROP_OVERLAP_CONFLICT")
                pextra["flags"] = pflags
        by_key.setdefault(key, []).append(len(out))
        out.append(item)
    return out, errors


def crop_row_bands(
    image_bytes: bytes,
    flagged: Sequence[Dict[str, Any]],
    *,
    row_height_frac: float = 0.035,
    header_frac: float = 0.12,
    scale: float = 2.0,
) -> List[bytes]:
    """Crop flagged rows using y_center (±1.5 row heights) plus header band; upscale."""
    try:
        from PIL import Image
        import io
    except Exception:
        return [image_bytes]

    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            w, h = im.size
            if h <= 0 or w <= 0:
                return [image_bytes]
            fmt = "JPEG" if image_bytes[:2] == b"\xff\xd8" else "PNG"
            header_band = im.crop((0, 0, w, max(1, int(h * header_frac))))
            out: List[bytes] = []
            half = 1.5 * row_height_frac
            for entry in flagged or []:
                if not isinstance(entry, dict):
                    continue
                y = entry.get("y_center")
                try:
                    yf = float(y) if y is not None else None
                except (TypeError, ValueError):
                    yf = None
                if yf is None:
                    # Fall back to full image once.
                    buf = io.BytesIO()
                    save_im = im
                    if fmt == "JPEG" and save_im.mode not in ("RGB", "L"):
                        save_im = save_im.convert("RGB")
                    save_im.save(buf, format=fmt, quality=92)
                    out.append(buf.getvalue())
                    continue
                y0 = max(0, int((yf - half) * h))
                y1 = min(h, int((yf + half) * h))
                if y1 <= y0:
                    y1 = min(h, y0 + max(1, int(row_height_frac * h)))
                row_band = im.crop((0, y0, w, y1))
                # Stack header + row band, then upscale.
                stacked_h = header_band.height + row_band.height
                stacked = Image.new(im.mode, (w, stacked_h))
                stacked.paste(header_band, (0, 0))
                stacked.paste(row_band, (0, header_band.height))
                if scale and scale != 1.0:
                    try:
                        resample = Image.Resampling.LANCZOS
                    except AttributeError:
                        resample = Image.LANCZOS  # type: ignore[attr-defined]
                    stacked = stacked.resize(
                        (max(1, int(w * scale)), max(1, int(stacked_h * scale))),
                        resample,
                    )
                buf = io.BytesIO()
                save_im = stacked
                if fmt == "JPEG" and save_im.mode not in ("RGB", "L"):
                    save_im = save_im.convert("RGB")
                save_kw: Dict[str, Any] = {"quality": 92} if fmt == "JPEG" else {}
                save_im.save(buf, format=fmt, **save_kw)
                out.append(buf.getvalue())
            return out or [image_bytes]
    except Exception:
        return [image_bytes]


def build_reread_prompt(flagged: List[Dict[str, Any]]) -> str:
    """Targeted digit re-read — row list only, never expected values or identity diffs."""
    labels = []
    for entry in flagged or []:
        if not isinstance(entry, dict):
            continue
        labels.append(
            f"{entry.get('row_index')}:{entry.get('product_name') or ''}".strip(":")
        )
    listed = ", ".join(labels) if labels else "(none)"
    return (
        "You are re-reading specific rows from a pharmaceutical stock statement. "
        "These rows may have misread digits. Read every digit of every cell again "
        "from the image. Do not calculate or balance anything.\n"
        f"Re-read ONLY these rows: {listed}.\n"
        "Return the same JSON shape (tables array) containing only these rows, "
        "with all columns. Copy numbers exactly as printed. Empty cells are null, "
        "never 0."
    )


def build_reconciliation_recovery_prompt(flagged: List[Dict[str, Any]]) -> str:
    """Row-level recon recovery: include mismatch context; forbid inventing balance."""
    blocks: List[str] = []
    for entry in flagged or []:
        if not isinstance(entry, dict):
            continue
        headers = entry.get("headers") or []
        header_line = ", ".join(str(h) for h in headers) if headers else "(from image)"
        name = entry.get("product_name") or ""
        blocks.append(
            f"Row {entry.get('row_index')}: {name}\n"
            f"Detected columns: {header_line}\n"
            f"Opening={entry.get('opening_qty')}, "
            f"Purchase={entry.get('purchase_qty')}, "
            f"Sales={entry.get('sales_qty')}, "
            f"SalesReturn/GoodsRet={entry.get('sales_return_qty')}, "
            f"PurchaseReturn={entry.get('purchase_return_qty')}, "
            f"ExpiryDamage={entry.get('expiry_damage_qty')}, "
            f"Sample={entry.get('sample_qty')}, "
            f"extracted Closing={entry.get('extracted_closing_qty')}.\n"
            f"Expected closing mathematically equals "
            f"{entry.get('expected_closing_qty')} "
            f"(difference={entry.get('quantity_difference')}).\n"
            "Re-read the source image and determine what is actually printed "
            "in the Closing column (and other quantity columns for this row).\n"
            "Do not change the value merely to make the arithmetic balance.\n"
            "Return the value actually visible in the source."
        )
    body = "\n\n".join(blocks) if blocks else "(none)"
    return (
        "You are re-reading ONLY the failed product rows below from a "
        "pharmaceutical stock statement image. Preserve row boundaries and "
        "column positions. Distinguish quantity columns from monetary columns.\n"
        "Do not invent numbers to satisfy a formula.\n\n"
        f"{body}\n\n"
        "Return the same JSON shape (tables array) containing only these rows, "
        "with all columns. Copy numbers exactly as printed. Empty cells are null, "
        "never 0."
    )


def merge_reread(
    first_items: List[Dict[str, Any]],
    reread_items: List[Dict[str, Any]],
    *,
    prefer_reconciled: bool = False,
) -> List[Dict[str, Any]]:
    """Replace a row only when the re-read classifies as VALID/MINOR.

    Never mix cells from two readings in one row.
    When ``prefer_reconciled`` is True (recon recovery), also accept a
    candidate that passes stock reconciliation while the first reading fails.
    """
    from services.stock_reconciliation import reconcile_row

    reread_by_index: Dict[int, Dict[str, Any]] = {}
    reread_by_name: Dict[str, Dict[str, Any]] = {}
    for item in reread_items or []:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        vidx = extra.get("vision_row_index")
        if vidx is not None:
            try:
                reread_by_index[int(vidx)] = item
            except (TypeError, ValueError):
                pass
        key = _normalize_product_key(item.get("product_name"))
        if key:
            reread_by_name[key] = item

    out: List[Dict[str, Any]] = []
    for item in first_items or []:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        vidx = extra.get("vision_row_index")
        candidate = None
        if vidx is not None:
            try:
                candidate = reread_by_index.get(int(vidx))
            except (TypeError, ValueError):
                candidate = None
        if candidate is None:
            candidate = reread_by_name.get(_normalize_product_key(item.get("product_name")))
        if candidate is None:
            out.append(item)
            continue

        status, _info = classify_row(read_row_fields(candidate))
        accept = status in {
            RowStatus.VALID,
            RowStatus.MINOR_DISCREPANCY,
            RowStatus.CLOSING_DERIVED,
        }
        if not accept and prefer_reconciled:
            try:
                cand_ok = bool(reconcile_row(candidate).get("valid"))
                first_ok = bool(reconcile_row(item).get("valid"))
            except Exception:
                cand_ok, first_ok = False, False
            accept = cand_ok and not first_ok
        if accept:
            out.append(candidate)
        else:
            kept = dict(item)
            keptextra = dict(kept.get("extra") or {})
            candidates = list(keptextra.get("candidates") or [])
            candidates.append(
                {
                    "source": "vision_reread",
                    "fields": {
                        k: candidate.get(k)
                        for k in (
                            "product_name",
                            "opening_qty",
                            "receipts_qty",
                            "sales_qty",
                            "closing_qty",
                            "sales_value",
                            "closing_value",
                        )
                    },
                    "status": status.value,
                }
            )
            keptextra["candidates"] = candidates
            kept["extra"] = keptextra
            out.append(kept)
    return out


# ---------------------------------------------------------------------------
# Phase 2b-2 fallback meta (thread-local) so early returns still carry budgets
# ---------------------------------------------------------------------------

import threading

_VISION_TABLE_TLS = threading.local()


def stash_vision_table_fallback_meta(meta: Optional[Dict[str, Any]]) -> None:
    _VISION_TABLE_TLS.meta = dict(meta or {})


def take_vision_table_fallback_meta() -> Dict[str, Any]:
    meta = getattr(_VISION_TABLE_TLS, "meta", None) or {}
    _VISION_TABLE_TLS.meta = None
    return dict(meta) if isinstance(meta, dict) else {}


def apply_vision_table_fallback_meta(result: Dict[str, Any]) -> Dict[str, Any]:
    meta = take_vision_table_fallback_meta()
    if not meta or not isinstance(result, dict):
        return result
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    if not isinstance(extra, dict):
        return result
    prior = 0
    try:
        prior = int(extra.get("gemini_calls") or 0)
    except (TypeError, ValueError):
        prior = 0
    try:
        carried = int(meta.get("gemini_calls") or 0)
    except (TypeError, ValueError):
        carried = 0
    extra["gemini_calls"] = max(prior, carried)
    try:
        budget = int(meta.get("gemini_budget") or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget > 0:
        extra["gemini_budget"] = budget
    return result


# ---------------------------------------------------------------------------
# Phase 2b-2: primary path behind STOCK_VISION_TABLE (default OFF)
# ---------------------------------------------------------------------------


def stock_vision_table_enabled() -> bool:
    return os.getenv("STOCK_VISION_TABLE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def stock_vision_table_types() -> set:
    raw = os.getenv("STOCK_VISION_TABLE_TYPES", "image")
    return {part.strip().lower() for part in str(raw).split(",") if part.strip()}


def vision_table_active_for(input_type: Optional[str]) -> bool:
    if not stock_vision_table_enabled():
        return False
    return str(input_type or "").strip().lower() in stock_vision_table_types()


def is_scanned_pdf_bytes(file_bytes: bytes, doc: Any = None) -> bool:
    """Same embedded-char test as _maybe_early_vision_for_image_only_pdf."""
    embedded_chars = 0
    try:
        if doc is not None:
            page_count = int(getattr(doc, "page_count", 0) or 0)
            for page_index in range(page_count):
                embedded_chars += len(
                    re.sub(r"\s+", "", doc[page_index].get_text("text") or "")
                )
        else:
            import fitz

            opened = fitz.open(stream=file_bytes, filetype="pdf")
            try:
                for page in opened:
                    embedded_chars += len(re.sub(r"\s+", "", page.get_text("text") or ""))
            finally:
                opened.close()
    except Exception:
        return False
    return embedded_chars < 40


def _empty_result(filename: str, ext: str) -> Dict[str, Any]:
    return {
        "source_file": filename,
        "source_format": (ext or "").lstrip("."),
        "stockist_name": None,
        "stockist_address": None,
        "company_name": None,
        "period_from": None,
        "period_to": None,
        "report_title": None,
        "line_items": [],
        "totals": {"sales_value": None, "closing_value": None, "extra": {}},
    }


def _stamp_vision_result(
    *,
    filename: str,
    ext: str,
    line_items: List[Dict[str, Any]],
    column_map: List[Dict[str, Any]],
    classified: Dict[str, Any],
    errors: List[str],
    gemini_calls: int,
    gemini_budget: int,
    stockist_name: Any = None,
    statement_period: Any = None,
    totals_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    result = _empty_result(filename, ext)
    result["line_items"] = line_items
    if stockist_name:
        result["stockist_name"] = stockist_name
    period = str(statement_period or "").strip()
    if period:
        # Best-effort: leave as report_title period text; parsers may normalize later.
        result["report_title"] = result.get("report_title") or period
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "vision_table"
    extra["vision_table_final"] = True
    extra["column_map"] = column_map
    extra["row_status_counts"] = classified.get("row_status_counts") or {}
    extra["valid_ratio"] = classified.get("valid_ratio")
    extra["vision_table_errors"] = list(errors or [])
    extra["gemini_calls"] = int(gemini_calls)
    extra["gemini_budget"] = int(gemini_budget)
    if totals_rows:
        extra["vision_table_total_rows"] = totals_rows
    # Skip post-pipeline Gemini gates.
    extra["stock_image_vision_decided"] = True
    extra["final_selected_extraction"] = "vision_table"
    return result


def _pages_for_flagged(
    page_images: List[bytes],
    item_page: List[int],
    flagged_line_indices: List[int],
) -> List[bytes]:
    pages_needed = set()
    for idx in flagged_line_indices:
        if 0 <= idx < len(item_page):
            pages_needed.add(item_page[idx])
    if not pages_needed:
        return list(page_images)
    return [page_images[p] for p in sorted(pages_needed) if 0 <= p < len(page_images)]


def run_vision_table_path(
    file_bytes: bytes,
    input_type: str,
    ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Primary Vision-table extract. Returns {status, reason, result, ...}.

    status=ok keeps the vision result (even with identity failures).
    status=fallback hands control back to the existing pipeline.
    """
    ctx = dict(ctx or {})
    filename = str(ctx.get("filename") or "upload")
    ext = str(ctx.get("ext") or "")
    request_id = str(ctx.get("request_id") or "-")
    kind = str(input_type or "").strip().lower()

    def _log(msg: str, **fields: Any) -> None:
        parts = " ".join(f"{k}={v}" for k, v in fields.items())
        logger.info("VISION_TABLE request_id=%s %s %s", request_id, msg, parts)

    try:
        from services.stock_row_classifier import (
            bump_gemini_calls,
            classify_result,
            get_gemini_calls,
            veto_decision,
        )

        page_images: List[bytes]
        if kind == "image":
            page_images = [file_bytes]
            gemini_budget = 2
        elif kind == "scanned_pdf":
            from services.gemini_extraction_fallback import _pdf_images

            page_images = _pdf_images(file_bytes, max_pages=500)
            if not page_images:
                _log("status=fallback", reason="INVALID_VISION_JSON")
                return {
                    "status": "fallback",
                    "reason": "INVALID_VISION_JSON",
                    "result": None,
                    "gemini_calls": 0,
                    "gemini_budget": 2,
                }
            pages_per = _pages_per_call()
            n_batches = max(1, (len(page_images) + pages_per - 1) // pages_per)
            gemini_budget = n_batches + 1
        else:
            _log("status=fallback", reason="unsupported_input_type")
            return {
                "status": "fallback",
                "reason": "unsupported_input_type",
                "result": None,
                "gemini_calls": 0,
                "gemini_budget": 2,
            }

        pages_per = _pages_per_call()
        # Defer tall-image vertical split: recon recovery has higher priority
        # than optional quality crops that would consume the spare Gemini call.
        deferred_vertical_split = False
        original_image_for_reread: Optional[bytes] = (
            file_bytes if kind == "image" else None
        )
        if (
            kind == "image"
            and len(page_images) == 1
            and _needs_vertical_split(page_images[0])
        ):
            deferred_vertical_split = True
            _log(
                "vertical_split_deferred",
                reason="recon_priority",
                height=_image_dimensions(file_bytes)[1],
                min_height=_split_min_height(),
            )

        all_items: List[Dict[str, Any]] = []
        all_totals: List[Dict[str, Any]] = []
        item_page: List[int] = []
        column_map: List[Dict[str, Any]] = []
        errors: List[str] = []
        carry = None
        stockist_name = None
        statement_period = None
        calls = 0
        global_row = 0
        coerce_total: Dict[str, int] = {}
        tables_found_total = 0

        for start in range(0, len(page_images), pages_per):
            batch = page_images[start : start + pages_per]
            raw = extract_stock_table_vision(
                batch, {"label": "stock_vision_table", "request_id": request_id}
            )
            calls += 1
            _log_vision_boundary(
                "raw_vision_response",
                {
                    "batch_start": start,
                    "tables": len(raw.get("tables") or [])
                    if isinstance(raw, dict)
                    else 0,
                    "error": raw.get("error") if isinstance(raw, dict) else None,
                    "sample_rows": [
                        {
                            "row_index": r.get("row_index"),
                            "cells": (r.get("cells") or [])[:14],
                        }
                        for t in (raw.get("tables") or [])[:1]
                        if isinstance(t, dict)
                        for r in (t.get("rows") or [])[:12]
                        if isinstance(r, dict)
                    ],
                    "headers": [
                        [
                            {
                                "col_index": c.get("col_index"),
                                "text": c.get("text"),
                            }
                            for c in (hr if isinstance(hr, list) else [])
                            if isinstance(c, dict)
                        ]
                        for t in (raw.get("tables") or [])[:1]
                        if isinstance(t, dict)
                        for hr in (t.get("header_rows") or [])[:1]
                    ],
                },
                request_id=request_id,
            )
            if raw.get("error"):
                _log(
                    "status=fallback",
                    reason=raw.get("error") or "INVALID_VISION_JSON",
                    gemini_calls=calls,
                    truncated=bool(raw.get("truncated")),
                )
                return {
                    "status": "fallback",
                    "reason": str(raw.get("error") or "INVALID_VISION_JSON"),
                    "result": None,
                    "gemini_calls": calls,
                    "gemini_budget": gemini_budget,
                }
            mapped = map_vision_table(raw, carry_header=carry, request_id=request_id)
            for k, v in (mapped.get("coercions") or {}).items():
                coerce_total[k] = coerce_total.get(k, 0) + int(v or 0)
            tables_found_total += int(mapped.get("tables_found") or 0)
            if any(str(e) == "STRUCTURAL_ERROR" for e in (mapped.get("errors") or [])):
                if carry is None or not mapped.get("line_items"):
                    _log("status=fallback", reason="STRUCTURAL_ERROR", gemini_calls=calls)
                    return {
                        "status": "fallback",
                        "reason": "STRUCTURAL_ERROR",
                        "result": None,
                        "gemini_calls": calls,
                        "gemini_budget": gemini_budget,
                    }
            if mapped.get("header_used"):
                carry = mapped["header_used"]
                column_map = list(mapped.get("column_map") or column_map)
            errors.extend(str(e) for e in (mapped.get("errors") or []))
            if mapped.get("stockist_name") and not stockist_name:
                stockist_name = mapped.get("stockist_name")
            if mapped.get("statement_period") and not statement_period:
                statement_period = mapped.get("statement_period")
            for item in mapped.get("line_items") or []:
                extra = item.setdefault("extra", {})
                extra["vision_row_index"] = global_row
                item_page.append(start)
                all_items.append(item)
                global_row += 1
            for tot in mapped.get("totals_rows") or []:
                all_totals.append(tot)

        if not all_items:
            _log("status=fallback", reason="zero_product_rows", gemini_calls=calls)
            return {
                "status": "fallback",
                "reason": "zero_product_rows",
                "result": None,
                "gemini_calls": calls,
                "gemini_budget": gemini_budget,
            }

        result = _stamp_vision_result(
            filename=filename,
            ext=ext,
            line_items=all_items,
            column_map=column_map,
            classified=classify_result({"line_items": all_items}),
            errors=errors,
            gemini_calls=calls,
            gemini_budget=gemini_budget,
            stockist_name=stockist_name,
            statement_period=statement_period,
            totals_rows=all_totals,
        )
        bump_gemini_calls(result, 0)
        result["totals"]["extra"]["gemini_calls"] = calls
        result["totals"]["extra"]["gemini_budget"] = gemini_budget
        result["totals"]["extra"]["vision_normalize_coercions"] = coerce_total
        result["totals"]["extra"]["tables_found"] = tables_found_total
        if deferred_vertical_split:
            result["totals"]["extra"]["vertical_split_deferred"] = "recon_priority"
        _log_vision_boundary(
            "api_response_payload",
            {
                "note": "pre-reconciliation vision result (DB/API use same line_items keys)",
                "extraction_method": ((result.get("totals") or {}).get("extra") or {}).get(
                    "extraction_method"
                ),
                "line_items": [
                    {
                        "product_name": it.get("product_name"),
                        "lms": (it.get("extra") or {}).get("lms"),
                        "opening_qty": it.get("opening_qty"),
                        "receipts_qty": it.get("receipts_qty"),
                        "sales_qty": it.get("sales_qty"),
                        "closing_qty": it.get("closing_qty"),
                        "closing_value": it.get("closing_value"),
                        "physical_cells": (it.get("extra") or {}).get("physical_cells"),
                    }
                    for it in (result.get("line_items") or [])
                    if "BONN" in str(it.get("product_name") or "").upper()
                    or "ARJUNA" in str(it.get("product_name") or "").upper()
                    or "BRESOL" in str(it.get("product_name") or "").upper()
                ],
            },
            request_id=request_id,
        )
        _log_vision_boundary(
            "db_payload_before_persist",
            {
                "note": "same line_item dicts returned to /extract-sales-statement caller",
                "count": len(result.get("line_items") or []),
            },
            request_id=request_id,
        )

        classified = classify_result(result)
        result["totals"]["extra"]["row_status_counts"] = (
            classified.get("row_status_counts") or {}
        )
        result["totals"]["extra"]["valid_ratio"] = classified.get("valid_ratio")

        # Priority: reconcile BEFORE optional identity reread / secondary Vision.
        from services.stock_reconciliation import (
            apply_stock_reconciliation,
            flagged_row_indices,
            flagged_row_recovery_meta,
        )

        result = apply_stock_reconciliation(
            result, request_id=request_id, page=1, recovery_attempt=0
        )
        flagged = flagged_row_indices(result)
        if flagged and calls < gemini_budget and kind in {"image", "scanned_pdf"}:
            logger.info(
                "STOCK_RECONCILIATION_RECOVERY request_id=%s flagged=%s "
                "gemini_calls=%s budget=%s",
                request_id,
                len(flagged),
                calls,
                gemini_budget,
            )
            flagged_meta = flagged_row_recovery_meta(result, column_map)
            if flagged_meta:
                if kind == "scanned_pdf":
                    reread_images = _pages_for_flagged(
                        page_images, item_page, flagged
                    )
                else:
                    base = original_image_for_reread or (
                        page_images[0] if page_images else file_bytes
                    )
                    reread_images = crop_row_bands(base, flagged_meta)
                prompt = build_reconciliation_recovery_prompt(flagged_meta)
                raw2 = extract_stock_table_vision(
                    reread_images,
                    {
                        "label": "stock_vision_table_recon_recovery",
                        "prompt": prompt,
                        "request_id": request_id,
                    },
                )
                calls += 1
                result["totals"]["extra"]["gemini_calls"] = calls
                if not raw2.get("error"):
                    mapped2 = map_vision_table(
                        raw2, carry_header=carry, request_id=request_id
                    )
                    merged = merge_reread(
                        result.get("line_items") or [],
                        mapped2.get("line_items") or [],
                        prefer_reconciled=True,
                    )
                    result["line_items"] = merged
                    all_items = merged
                result = apply_stock_reconciliation(
                    result, request_id=request_id, page=1, recovery_attempt=1
                )

        # Optional secondary Vision (identity reread) only if budget remains.
        decision = veto_decision(result, kind)
        if decision.get("veto") and calls < gemini_budget:
            flagged_idxs = [
                int(i)
                for i in (decision.get("flagged_rows") or [])
                if isinstance(i, int) or str(i).isdigit()
            ]
            # Prefer rows still failing reconciliation; fall back to veto list.
            still_bad = flagged_row_indices(result)
            target_idxs = still_bad or flagged_idxs
            flagged_meta = []
            for idx in target_idxs:
                if 0 <= idx < len(all_items):
                    item = all_items[idx]
                    flagged_meta.append(
                        {
                            "row_index": int(
                                ((item.get("extra") or {}).get("vision_row_index") or idx)
                            ),
                            "product_name": item.get("product_name"),
                            "y_center": (item.get("extra") or {}).get("y_center"),
                        }
                    )
            if flagged_meta:
                if kind == "scanned_pdf":
                    reread_images = _pages_for_flagged(
                        page_images, item_page, target_idxs
                    )
                else:
                    base = original_image_for_reread or (
                        page_images[0] if page_images else file_bytes
                    )
                    reread_images = crop_row_bands(base, flagged_meta)
                prompt = build_reread_prompt(flagged_meta)
                raw2 = extract_stock_table_vision(
                    reread_images,
                    {
                        "label": "stock_vision_table_reread",
                        "prompt": prompt,
                        "request_id": request_id,
                    },
                )
                calls += 1
                result["totals"]["extra"]["gemini_calls"] = calls
                if not raw2.get("error"):
                    mapped2 = map_vision_table(
                        raw2, carry_header=carry, request_id=request_id
                    )
                    merged = merge_reread(
                        result.get("line_items") or [],
                        mapped2.get("line_items") or [],
                    )
                    classified = classify_result({"line_items": merged})
                    result["line_items"] = merged
                    result["totals"]["extra"]["row_status_counts"] = (
                        classified.get("row_status_counts") or {}
                    )
                    result["totals"]["extra"]["valid_ratio"] = classified.get(
                        "valid_ratio"
                    )
                    all_items = merged
                    result = apply_stock_reconciliation(
                        result, request_id=request_id, page=1, recovery_attempt=1
                    )
        elif decision.get("veto") and calls >= gemini_budget:
            result["totals"]["extra"]["reread_skipped"] = "budget"
        elif deferred_vertical_split and calls >= gemini_budget:
            result["totals"]["extra"]["reread_skipped"] = "budget"

        classified = classify_result(result)
        result["totals"]["extra"]["row_status_counts"] = (
            classified.get("row_status_counts") or {}
        )
        result["totals"]["extra"]["valid_ratio"] = classified.get("valid_ratio")
        result["totals"]["extra"]["gemini_calls"] = calls

        skipped = [
            "early_vision",
            "ocr_probes",
            "apply_direct_stock_image_gemini",
            "maybe_apply_gemini_fallback",
            "sanitize_numeric_overrides",
        ]
        _log(
            "status=ok",
            rows=len(result.get("line_items") or []),
            valid_ratio=result["totals"]["extra"].get("valid_ratio"),
            recon_fail=result["totals"]["extra"].get("stock_reconciliation_fail_count"),
            gemini_calls=calls,
            skipped_gates=",".join(skipped),
        )
        return {
            "status": "ok",
            "reason": "ok",
            "result": result,
            "gemini_calls": calls,
            "gemini_budget": gemini_budget,
            "skipped_gates": skipped,
        }
    except Exception as exc:
        _log("status=fallback", reason="exception", error=type(exc).__name__)
        logger.info(
            "VISION_TABLE request_id=%s exception=%s", request_id, type(exc).__name__
        )
        return {
            "status": "fallback",
            "reason": "exception",
            "result": None,
            "gemini_calls": int(ctx.get("gemini_calls") or 0),
            "gemini_budget": 2,
        }
