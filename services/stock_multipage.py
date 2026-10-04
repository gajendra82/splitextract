"""Cross-page stock-statement row resolution (camera-merged multi-page PDFs).

Runs AFTER per-page extraction and BEFORE final Laravel payload assembly.
Does not invent or overwrite printed quantities. Single-page statements are
unchanged (this module is a no-op when page_count < 2).
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
from collections import defaultdict
from copy import deepcopy
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

STOCK_MULTI_PAGE_FLAG = "STOCK_MULTI_PAGE_CAMERA_PDF_ENABLED"

# Top-level qty fields + extra aliases used for conflict / continuation checks.
_TOP_QTY = (
    "opening_qty",
    "receipts_qty",
    "sales_qty",
    "closing_qty",
)
_EXTRA_QTY = (
    ("sale_return", "sales_return_qty"),
    ("purchase_return", "purchase_return_qty"),
    ("total_stock", "total_qty"),
    ("lms", "lms"),
)


def is_multipage_camera_pdf_enabled() -> bool:
    return os.getenv(STOCK_MULTI_PAGE_FLAG, "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def normalize_product_name_key(name: Any) -> str:
    """Reuse project-style alphanumeric product key (same as OCR benchmark)."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def normalize_pack_key(pack: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(pack or "").lower())


def normalize_product_code_key(code: Any) -> str:
    raw = str(code or "").strip()
    if not raw:
        return ""
    return re.sub(r"[^a-z0-9]", "", raw.lower())


def _extra(item: Dict[str, Any]) -> Dict[str, Any]:
    ex = item.get("extra")
    if not isinstance(ex, dict):
        ex = {}
        item["extra"] = ex
    return ex


def _field_source(item: Dict[str, Any]) -> Dict[str, str]:
    ex = _extra(item)
    fs = ex.get("field_source")
    if not isinstance(fs, dict):
        fs = {}
        ex["field_source"] = fs
    return fs


def _as_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def printed_qty(item: Dict[str, Any], field: str) -> Optional[float]:
    """Return printed qty or None when missing / not readable.

    Never treats legacy API 0.0 as printed when field_source says missing.
    """
    fs = _field_source(item)
    ex = _extra(item)
    # Map logical field → storage
    if field in _TOP_QTY:
        if fs.get(field) == "missing":
            return None
        return _as_float(item.get(field))
    for ex_key, canon in _EXTRA_QTY:
        if field in (ex_key, canon):
            src_key = canon if canon in fs else field
            if fs.get(src_key) == "missing" or fs.get(ex_key) == "missing":
                return None
            if ex.get(ex_key) is not None:
                return _as_float(ex.get(ex_key))
            return _as_float(item.get(canon))
    return None


def stamp_page_number(item: Dict[str, Any], page_number: int) -> Dict[str, Any]:
    """Attach page provenance (1-based). Internal + extra; Laravel-safe."""
    out = item
    ex = _extra(out)
    out["page_number"] = int(page_number)
    ex["page_number"] = int(page_number)
    pages = ex.get("source_pages")
    if not isinstance(pages, list):
        pages = []
    if int(page_number) not in pages:
        pages.append(int(page_number))
    ex["source_pages"] = pages
    return out


def product_identity(
    item: Dict[str, Any],
) -> Tuple[str, Tuple[Any, ...]]:
    """Return (method, key_tuple) for grouping.

    Priority: product_code → name+pack → name → weak unique.
    """
    code = normalize_product_code_key(item.get("product_code"))
    if code:
        return "product_code", ("code", code)
    name = normalize_product_name_key(item.get("product_name"))
    pack = normalize_pack_key(item.get("packing"))
    if name and pack:
        return "name_pack", ("np", name, pack)
    if name:
        return "name", ("n", name)
    # No identity — never merge with others.
    return "none", ("id", id(item))


def _printed_qty_map(item: Dict[str, Any]) -> Dict[str, Optional[float]]:
    fields = (
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "closing_qty",
        "sales_return_qty",
        "purchase_return_qty",
        "total_qty",
        "lms",
    )
    return {f: printed_qty(item, f) for f in fields}


def _row_completeness(qty: Dict[str, Optional[float]]) -> int:
    return sum(
        1
        for f in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
        if qty.get(f) is not None
    )


def _values_conflict(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) > 0.51


def classify_pair(
    left: Dict[str, Any], right: Dict[str, Any]
) -> str:
    """exact_duplicate | continuation | conflict | independent."""
    qa, qb = _printed_qty_map(left), _printed_qty_map(right)
    conflicts = [f for f in qa if _values_conflict(qa[f], qb[f])]
    if conflicts:
        return "conflict"
    # Any overlapping printed equal values + no conflicts
    overlap_equal = [
        f
        for f in qa
        if qa[f] is not None and qb[f] is not None and abs(qa[f] - qb[f]) <= 0.51
    ]
    a_only = [f for f in qa if qa[f] is not None and qb[f] is None]
    b_only = [f for f in qa if qb[f] is not None and qa[f] is None]
    if a_only and b_only and not conflicts:
        return "continuation"
    if overlap_equal and not a_only and not b_only:
        return "exact_duplicate"
    if overlap_equal and (a_only or b_only) and not conflicts:
        return "continuation"
    # Both look like complete independent rows with no shared printed fields
    if _row_completeness(qa) >= 2 and _row_completeness(qb) >= 2 and not overlap_equal:
        return "independent"
    if a_only or b_only:
        return "continuation"
    if overlap_equal:
        return "exact_duplicate"
    return "independent"


def _merge_field_source(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    dfs = _field_source(dst)
    sfs = _field_source(src)
    for k, v in sfs.items():
        if dfs.get(k) == "printed":
            continue
        if v == "printed":
            dfs[k] = "printed"
        elif k not in dfs:
            dfs[k] = v


def merge_line_items(
    items: Sequence[Dict[str, Any]],
    *,
    identity_method: str,
    resolution: str,
) -> Dict[str, Any]:
    """Merge complementary / duplicate rows. Never invents values."""
    assert items
    base = deepcopy(items[0])
    ex = _extra(base)
    source_pages: List[int] = []
    for it in items:
        p = it.get("page_number")
        if p is None:
            p = (_extra(it).get("page_number"))
        if p is not None and int(p) not in source_pages:
            source_pages.append(int(p))
        for sp in _extra(it).get("source_pages") or []:
            if int(sp) not in source_pages:
                source_pages.append(int(sp))

    for other in items[1:]:
        oex = _extra(other)
        # Top-level qty: fill missing only.
        for field in _TOP_QTY:
            cur = printed_qty(base, field)
            nxt = printed_qty(other, field)
            if cur is None and nxt is not None:
                base[field] = float(nxt)
                _field_source(base)[field] = "printed"
            # conflict already prevented by classify_pair
        # Extra qty aliases
        for ex_key, canon in _EXTRA_QTY:
            cur = printed_qty(base, canon)
            nxt = printed_qty(other, canon)
            if cur is None and nxt is not None:
                ex[ex_key] = float(nxt)
                _field_source(base)[canon] = "printed"
                _field_source(base)[ex_key] = "printed"
        if not base.get("product_code") and other.get("product_code"):
            base["product_code"] = other.get("product_code")
        if not base.get("packing") and other.get("packing"):
            base["packing"] = other.get("packing")
        if not base.get("product_name") and other.get("product_name"):
            base["product_name"] = other.get("product_name")
        _merge_field_source(base, other)

    source_pages.sort()
    ex["source_pages"] = source_pages
    if source_pages:
        base["page_number"] = source_pages[0]
        ex["page_number"] = source_pages[0]
    ex["cross_page_resolution"] = resolution
    ex["identity_match_method"] = identity_method
    ex["cross_page_merged"] = resolution == "merged"
    return base


def resolve_cross_page_product_rows(
    line_items: Sequence[Dict[str, Any]],
    *,
    page_count: int = 1,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Deduplicate / continue products across pages.

    Returns (resolved_items, diagnostics).
    """
    diagnostics: Dict[str, Any] = {
        "multi_page": page_count > 1,
        "page_count": int(page_count),
        "input_rows": len(line_items or []),
        "dedup_candidates": 0,
        "merged_rows": 0,
        "ambiguous_rows": 0,
        "kept_separate": 0,
        "exact_duplicates_collapsed": 0,
        "continuations_merged": 0,
    }
    items = [deepcopy(it) for it in (line_items or []) if isinstance(it, dict)]
    if page_count <= 1 or len(items) < 2:
        diagnostics["output_rows"] = len(items)
        return items, diagnostics

    # Group by strong identity. Name-only groups are candidates only.
    groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = defaultdict(list)
    methods: Dict[Tuple[Any, ...], str] = {}
    unmatched: List[Dict[str, Any]] = []
    for it in items:
        method, key = product_identity(it)
        if method == "none":
            unmatched.append(it)
            continue
        groups[key].append(it)
        methods[key] = method

    resolved: List[Dict[str, Any]] = list(unmatched)

    for key, group in groups.items():
        method = methods[key]
        if len(group) == 1:
            it = group[0]
            ex = _extra(it)
            ex.setdefault("cross_page_resolution", "single")
            ex.setdefault("identity_match_method", method)
            resolved.append(it)
            continue

        diagnostics["dedup_candidates"] += len(group)
        # Sort by page for stable continuation left→right.
        group_sorted = sorted(
            group,
            key=lambda r: int(
                r.get("page_number")
                or _extra(r).get("page_number")
                or 0
            ),
        )

        # Name-only: do not merge across different packs if packs appear.
        if method == "name":
            packs = {
                normalize_pack_key(g.get("packing"))
                for g in group_sorted
                if normalize_pack_key(g.get("packing"))
            }
            if len(packs) > 1:
                for g in group_sorted:
                    ex = _extra(g)
                    ex["cross_page_resolution"] = "kept_separate"
                    ex["cross_page_identity_ambiguous"] = True
                    ex["identity_match_method"] = method
                    diagnostics["ambiguous_rows"] += 1
                    diagnostics["kept_separate"] += 1
                    resolved.append(g)
                continue

        # Greedy merge chain: try to fold each next row into an accumulator bucket.
        buckets: List[List[Dict[str, Any]]] = [[group_sorted[0]]]
        for nxt in group_sorted[1:]:
            placed = False
            for bucket in buckets:
                kind = classify_pair(bucket[-1], nxt)
                # Also allow merge against merged view of bucket
                if kind in ("conflict", "independent"):
                    kind2 = classify_pair(merge_line_items(bucket, identity_method=method, resolution="probe"), nxt)
                    if kind2 in ("continuation", "exact_duplicate"):
                        kind = kind2
                if kind == "exact_duplicate":
                    bucket.append(nxt)
                    placed = True
                    diagnostics["exact_duplicates_collapsed"] += 1
                    break
                if kind == "continuation":
                    # Name-only continuation requires sparse complementary side
                    if method == "name":
                        qa = _printed_qty_map(
                            merge_line_items(
                                bucket, identity_method=method, resolution="probe"
                            )
                        )
                        qb = _printed_qty_map(nxt)
                        if _row_completeness(qa) >= 3 and _row_completeness(qb) >= 3:
                            # Two full rows, same name, no pack — keep separate.
                            continue
                    bucket.append(nxt)
                    placed = True
                    diagnostics["continuations_merged"] += 1
                    break
                if kind == "conflict":
                    continue
            if not placed:
                # Ambiguous or independent — keep separate.
                buckets.append([nxt])
                if method in ("name", "name_pack"):
                    diagnostics["ambiguous_rows"] += 1

        for bucket in buckets:
            if len(bucket) == 1:
                it = bucket[0]
                ex = _extra(it)
                ex["cross_page_resolution"] = "kept_separate" if len(group_sorted) > 1 else "single"
                ex["identity_match_method"] = method
                if ex.get("cross_page_resolution") == "kept_separate":
                    ex["duplicate_candidate"] = True
                    diagnostics["kept_separate"] += 1
                resolved.append(it)
            else:
                merged = merge_line_items(
                    bucket, identity_method=method, resolution="merged"
                )
                diagnostics["merged_rows"] += 1
                resolved.append(merged)

    # Stable order: first page_number, then product name.
    def _sort_key(it: Dict[str, Any]) -> Tuple[int, str]:
        p = int(it.get("page_number") or _extra(it).get("page_number") or 0)
        return (p, str(it.get("product_name") or ""))

    resolved.sort(key=_sort_key)
    diagnostics["output_rows"] = len(resolved)
    logger.info(
        "STOCK_MULTIPAGE resolve input=%s output=%s candidates=%s merged=%s "
        "ambiguous=%s kept_separate=%s pages=%s",
        diagnostics["input_rows"],
        diagnostics["output_rows"],
        diagnostics["dedup_candidates"],
        diagnostics["merged_rows"],
        diagnostics["ambiguous_rows"],
        diagnostics["kept_separate"],
        page_count,
    )
    return resolved, diagnostics


def pdf_is_image_only_multipage(doc: Any, *, max_embedded_chars: int = 40) -> bool:
    """True when PDF has 2+ pages and almost no extractable text (camera merge)."""
    try:
        page_count = int(getattr(doc, "page_count", 0) or 0)
    except Exception:
        return False
    if page_count < 2:
        return False
    embedded = 0
    try:
        for i in range(page_count):
            embedded += len(re.sub(r"\s+", "", doc[i].get_text("text") or ""))
    except Exception:
        return False
    return embedded < max_embedded_chars


def render_pdf_page_png(doc: Any, page_index: int, *, zoom: float = 2.0) -> bytes:
    import fitz

    pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


class MultiPageImageExtractionError(Exception):
    """One page failed; caller must not return a completed statement."""

    def __init__(
        self,
        *,
        page_number: int,
        message: str,
        error: str = "page_processing_failed",
    ) -> None:
        super().__init__(message)
        self.page_number = int(page_number)
        self.message = message
        self.error = error


_MAX_MULTI_PAGE_IMAGES = 10

_REPEATED_HEADER_NAME = re.compile(
    r"^(product(\s*name)?|item(\s*name)?|particulars?|description|"
    r"opening|op\.?\s*stk|receipts?|receipt|sales?|closing|cl\.?\s*stk|"
    r"lms|pack(ing|g)?|code|qty|value)$",
    re.I,
)


def is_valid_stock_page_image(file_bytes: bytes) -> bool:
    """True when bytes look like a decodable raster image (JPEG/PNG/WebP/BMP/TIFF)."""
    if not file_bytes or len(file_bytes) < 24:
        return False
    head = file_bytes[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return True
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if head[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
        return True
    if head[:2] == b"BM":
        return True
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return True
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(file_bytes))
        img.verify()
        return True
    except Exception:
        return False


def _looks_like_repeated_header_row(item: Dict[str, Any]) -> bool:
    name = str(item.get("product_name") or "").strip()
    if not name:
        return False
    if _REPEATED_HEADER_NAME.match(name):
        return True
    # Header rows rarely have any printed qty identity fields.
    qty_printed = any(
        printed_qty(item, f) is not None
        for f in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
    )
    if not qty_printed and name.lower() in {
        "product",
        "item",
        "particulars",
        "op.stk",
        "cl.stk",
    }:
        return True
    return False


def extract_multipage_image_stock_statement(
    pages: Sequence[Tuple[str, bytes]],
    *,
    request_id: str = "",
    stockist_id: Optional[str] = None,
    month: Optional[str] = None,
    parse_image_fn=None,
    max_pages: int = _MAX_MULTI_PAGE_IMAGES,
) -> Dict[str, Any]:
    """Extract one stock statement from ordered page images in a single request.

    Pages are processed sequentially via the existing image path (_parse_image /
    Geometry V3). Cross-page reconciliation reuses resolve_cross_page_product_rows
    (no blind product_name uniquing).
    """
    t0 = time.perf_counter()
    page_count = len(pages)
    logger.info(
        "MULTI_PAGE_REQUEST_RECEIVED request_id=%s page_count=%s stockist_id=%s "
        "month=%s max_pages=%s",
        request_id or "-",
        page_count,
        stockist_id or "-",
        month or "-",
        max_pages,
    )
    if page_count < 1:
        raise MultiPageImageExtractionError(
            page_number=0,
            message="At least one page image is required",
            error="validation_failed",
        )
    if page_count > max_pages:
        raise MultiPageImageExtractionError(
            page_number=0,
            message=f"Maximum {max_pages} pages allowed; received {page_count}",
            error="too_many_pages",
        )

    if parse_image_fn is None:
        from services.sales_statement_extractor import _parse_image

        parse_image_fn = _parse_image

    page_results: List[Dict[str, Any]] = []
    all_items: List[Dict[str, Any]] = []
    geometry_pages = 0
    vision_pages = 0
    gemini_calls = 0

    for idx, (filename, file_bytes) in enumerate(pages):
        page_no = idx + 1
        safe_name = str(filename or f"page_{page_no}.jpg")
        logger.info(
            "MULTI_PAGE_PAGE_RECEIVED request_id=%s page_number=%s page_count=%s "
            "filename=%s bytes=%s",
            request_id or "-",
            page_no,
            page_count,
            safe_name,
            len(file_bytes or b""),
        )
        if not file_bytes:
            logger.info(
                "MULTI_PAGE_EXTRACTION_FAILED request_id=%s page_number=%s "
                "page_count=%s filename=%s error=empty_page",
                request_id or "-",
                page_no,
                page_count,
                safe_name,
            )
            raise MultiPageImageExtractionError(
                page_number=page_no,
                message=f"Unable to process page {page_no}: empty file",
                error="empty_page",
            )
        if not is_valid_stock_page_image(file_bytes):
            logger.info(
                "MULTI_PAGE_EXTRACTION_FAILED request_id=%s page_number=%s "
                "page_count=%s filename=%s error=invalid_image",
                request_id or "-",
                page_no,
                page_count,
                safe_name,
            )
            raise MultiPageImageExtractionError(
                page_number=page_no,
                message=f"Unable to process page {page_no}: invalid image",
                error="invalid_image",
            )

        ext = ".jpg"
        lower = safe_name.lower()
        if lower.endswith(".png"):
            ext = ".png"
        elif lower.endswith(".webp"):
            ext = ".webp"
        elif lower.endswith((".tif", ".tiff")):
            ext = ".tiff"
        elif lower.endswith(".bmp"):
            ext = ".bmp"

        page_t0 = time.perf_counter()
        logger.info(
            "MULTI_PAGE_PAGE_EXTRACTION_STARTED request_id=%s page_number=%s "
            "page_count=%s filename=%s",
            request_id or "-",
            page_no,
            page_count,
            safe_name,
        )
        try:
            page_result = parse_image_fn(file_bytes, safe_name, ext)
        except Exception as exc:
            logger.info(
                "MULTI_PAGE_EXTRACTION_FAILED request_id=%s page_number=%s "
                "page_count=%s filename=%s error=page_processing_failed "
                "exc=%s duration=%.3f",
                request_id or "-",
                page_no,
                page_count,
                safe_name,
                type(exc).__name__,
                time.perf_counter() - page_t0,
            )
            raise MultiPageImageExtractionError(
                page_number=page_no,
                message=f"Unable to process page {page_no}",
                error="page_processing_failed",
            ) from exc

        if not isinstance(page_result, dict):
            raise MultiPageImageExtractionError(
                page_number=page_no,
                message=f"Unable to process page {page_no}",
                error="page_processing_failed",
            )

        extra = (page_result.get("totals") or {}).get("extra") or {}
        if extra.get("extraction_failed"):
            raise MultiPageImageExtractionError(
                page_number=page_no,
                message=f"Unable to process page {page_no}",
                error="page_processing_failed",
            )

        method = str(extra.get("extraction_method") or "")
        if "geometry" in method:
            geometry_pages += 1
        elif "vision" in method or extra.get("vision_called"):
            vision_pages += 1
        gemini_calls += int(extra.get("gemini_calls") or 0)

        page_rows: List[Dict[str, Any]] = []
        for it in page_result.get("line_items") or []:
            if not isinstance(it, dict):
                continue
            stamped = stamp_page_number(deepcopy(it), page_no)
            if _looks_like_repeated_header_row(stamped):
                continue
            page_rows.append(stamped)
            all_items.append(stamped)

        page_duration = time.perf_counter() - page_t0
        page_entry = {
            "page_number": page_no,
            "filename": safe_name,
            "rows": page_rows,
            "line_item_count": len(page_rows),
            "duration": round(page_duration, 3),
            "extraction_method": method or None,
            "stockist_name": page_result.get("stockist_name"),
            "company_name": page_result.get("company_name"),
            "period_from": page_result.get("period_from"),
            "period_to": page_result.get("period_to"),
            "report_title": page_result.get("report_title"),
            "stockist_address": page_result.get("stockist_address"),
        }
        page_results.append(page_entry)
        logger.info(
            "MULTI_PAGE_PAGE_EXTRACTION_COMPLETED request_id=%s page_number=%s "
            "page_count=%s filename=%s duration=%.3f line_item_count=%s",
            request_id or "-",
            page_no,
            page_count,
            safe_name,
            page_duration,
            len(page_rows),
        )

    logger.info(
        "MULTI_PAGE_RECONCILIATION_STARTED request_id=%s page_count=%s "
        "line_item_count=%s",
        request_id or "-",
        page_count,
        len(all_items),
    )
    recon_t0 = time.perf_counter()
    resolved, dedup_diag = resolve_cross_page_product_rows(
        all_items, page_count=page_count
    )
    logger.info(
        "MULTI_PAGE_RECONCILIATION_COMPLETED request_id=%s page_count=%s "
        "duration=%.3f line_item_count=%s input_rows=%s output_rows=%s "
        "kept_separate=%s merged_rows=%s",
        request_id or "-",
        page_count,
        time.perf_counter() - recon_t0,
        len(resolved),
        dedup_diag.get("input_rows"),
        dedup_diag.get("output_rows"),
        dedup_diag.get("kept_separate"),
        dedup_diag.get("merged_rows"),
    )

    engine = (
        "geometry_v3"
        if geometry_pages > 0
        else ("vision_table" if vision_pages > 0 else "image_multipage")
    )
    merged: Dict[str, Any] = {
        "success": True,
        "is_multi_page": True,
        "page_count": page_count,
        "pages_processed": len(page_results),
        "extraction_engine": engine,
        "source_file": pages[0][0] if pages else "multipage",
        "source_format": "image_multipage",
        "stockist_id": stockist_id,
        "month": month,
        "stockist_name": None,
        "stockist_address": None,
        "company_name": None,
        "period_from": None,
        "period_to": None,
        "report_title": None,
        "line_items": resolved,
        "page_results": page_results,
        "totals": {
            "sales_value": None,
            "closing_value": None,
            "extra": {},
        },
    }
    for pr in page_results:
        for key in (
            "stockist_name",
            "stockist_address",
            "company_name",
            "period_from",
            "period_to",
            "report_title",
        ):
            if pr.get(key) and not merged.get(key):
                merged[key] = pr.get(key)

    any_geo = geometry_pages > 0
    any_vision = vision_pages > 0
    tex = merged["totals"]["extra"]
    tex.update(
        {
            "extraction_method": (
                "geometry_cell_ocr"
                if any_geo
                else ("vision_table" if any_vision else "image_multipage")
            ),
            "extraction_engine": engine,
            "multi_page": True,
            "is_multi_page": True,
            "page_count": page_count,
            "pages_processed": len(page_results),
            "geometry_pages": geometry_pages,
            "vision_pages": vision_pages,
            "gemini_calls": gemini_calls,
            "statement_count": 1,
            "final_statement_count": 1,
            "external_extraction_request_count": 1,
            "image_multipage": True,
            "geometry_cell_ocr_final": bool(any_geo),
            "vision_table_final": bool(any_geo or any_vision),
            "skip_post_pipeline_gemini": True,
            "geometry_skipped_vision_table": bool(any_geo),
            "geometry_called": geometry_pages > 0,
            "vision_called": vision_pages > 0,
            "cross_page_dedup": dedup_diag,
            "stockist_id": stockist_id,
            "month": month,
            "processing_time_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }
    )
    logger.info(
        "MULTI_PAGE_EXTRACTION_COMPLETED request_id=%s page_count=%s "
        "pages_processed=%s duration=%.3f line_item_count=%s "
        "extraction_engine=%s",
        request_id or "-",
        page_count,
        len(page_results),
        time.perf_counter() - t0,
        len(resolved),
        engine,
    )
    return merged


def parse_camera_multipage_stock_pdf(
    file_bytes: bytes,
    filename: str,
    *,
    parse_image_fn=None,
    max_pages: int = 20,
    zoom: float = 2.0,
) -> Optional[Dict[str, Any]]:
    """Extract each PDF page via existing image path, then resolve cross-page rows.

    Returns None when this path should not claim the file (caller continues).
    """
    if not is_multipage_camera_pdf_enabled():
        return None

    try:
        import fitz
    except ImportError:
        return None

    t0 = time.perf_counter()
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        if not pdf_is_image_only_multipage(doc):
            return None
        page_count = int(doc.page_count or 0)
        if page_count < 2 or page_count > max_pages:
            return None

        if parse_image_fn is None:
            # Late import avoids circular import at module load.
            from services.sales_statement_extractor import _parse_image

            parse_image_fn = _parse_image

        page_results: List[Dict[str, Any]] = []
        geometry_pages = 0
        vision_pages = 0
        gemini_calls = 0
        all_items: List[Dict[str, Any]] = []

        for page_index in range(page_count):
            page_no = page_index + 1
            png = render_pdf_page_png(doc, page_index, zoom=zoom)
            page_name = f"{filename}#page{page_no}"
            try:
                page_result = parse_image_fn(png, page_name, ".png")
            except Exception as exc:
                logger.info(
                    "STOCK_MULTIPAGE page=%s extract_failed err=%s",
                    page_no,
                    type(exc).__name__,
                )
                page_result = {
                    "line_items": [],
                    "totals": {"extra": {"extraction_error": type(exc).__name__}},
                }
            if not isinstance(page_result, dict):
                page_result = {"line_items": []}

            extra = (page_result.get("totals") or {}).get("extra") or {}
            method = str(extra.get("extraction_method") or "")
            if "geometry" in method:
                geometry_pages += 1
            elif "vision" in method or extra.get("vision_called"):
                vision_pages += 1
            gemini_calls += int(extra.get("gemini_calls") or 0)

            for it in page_result.get("line_items") or []:
                if not isinstance(it, dict):
                    continue
                stamped = stamp_page_number(deepcopy(it), page_no)
                all_items.append(stamped)

            page_results.append(page_result)

        if len(all_items) < 1:
            return None

        resolved, dedup_diag = resolve_cross_page_product_rows(
            all_items, page_count=page_count
        )

        # Metadata from first non-empty page.
        merged: Dict[str, Any] = {
            "source_file": filename,
            "source_format": "pdf",
            "stockist_name": None,
            "stockist_address": None,
            "company_name": None,
            "period_from": None,
            "period_to": None,
            "report_title": None,
            "line_items": resolved,
            "totals": {
                "sales_value": None,
                "closing_value": None,
                "extra": {},
            },
        }
        for pr in page_results:
            for key in (
                "stockist_name",
                "stockist_address",
                "company_name",
                "period_from",
                "period_to",
                "report_title",
            ):
                if pr.get(key) and not merged.get(key):
                    merged[key] = pr.get(key)

        # Prefer geometry_cell_ocr_final if any page used geometry.
        any_geo_final = any(
            bool(((pr.get("totals") or {}).get("extra") or {}).get("geometry_cell_ocr_final"))
            for pr in page_results
        )
        any_vision_final = any(
            bool(((pr.get("totals") or {}).get("extra") or {}).get("vision_table_final"))
            for pr in page_results
        )
        tex = merged["totals"]["extra"]
        tex.update(
            {
                "extraction_method": (
                    "geometry_cell_ocr"
                    if any_geo_final
                    else ("vision_table" if any_vision_final else "camera_multipage_pdf")
                ),
                "extraction_engine": "camera_multipage_pdf",
                "multi_page": True,
                "page_count": page_count,
                "pages_processed": len(page_results),
                "geometry_pages": geometry_pages,
                "vision_pages": vision_pages,
                "gemini_calls": gemini_calls,
                "statement_count": 1,
                "camera_multipage_pdf": True,
                "geometry_cell_ocr_final": bool(any_geo_final),
                "vision_table_final": bool(any_geo_final or any_vision_final),
                "skip_post_pipeline_gemini": True,
                "geometry_skipped_vision_table": bool(any_geo_final),
                "geometry_called": geometry_pages > 0,
                "vision_called": vision_pages > 0,
                "cross_page_dedup": dedup_diag,
                "processing_time_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }
        )
        logger.info(
            "STOCK_MULTIPAGE done file=%s pages=%s items_in=%s items_out=%s "
            "geometry_pages=%s vision_pages=%s gemini_calls=%s ms=%s",
            filename,
            page_count,
            dedup_diag.get("input_rows"),
            dedup_diag.get("output_rows"),
            geometry_pages,
            vision_pages,
            gemini_calls,
            tex["processing_time_ms"],
        )
        return merged
    finally:
        try:
            doc.close()
        except Exception:
            pass
