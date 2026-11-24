"""Read-only GT-20 quantity error tracer (stored PaddleOCR-VL → extract → score).

Does not modify production code, GT, or benchmark inputs. Writes artifacts under
this directory only.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_phase2_ab_gt20 import (  # noqa: E402
    _canonical_field,
    _find_line_item,
    _line_items,
    _qty_from_item,
)
from services.ground_truth.html_table import (  # noqa: E402
    _normalize_product_key,
    parse_html_tables,
)
from services.ground_truth.matching import exact_numeric_match, normalize_numeric  # noqa: E402
from services.ground_truth.schema import only_source_verified_fields  # noqa: E402
from services.html_table.column_matcher import match_headers  # noqa: E402
from services.vl_html_table import extract_vl_table_html, parse_vl_html_table  # noqa: E402

OUT = Path(__file__).resolve().parent
PER = ROOT / "phase4" / "phase2_ab_gt20" / "per_document"
VL_DIR = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"
GT_DIR = ROOT / "phase4" / "ground_truth_50" / "per_document"
SEED = ROOT / "phase4" / "ground_truth_50" / "seed_index_20.json"

CATEGORY = {
    "A": "OCR digit recognition",
    "B": "Table cell extraction",
    "C": "Row alignment",
    "D": "Header/column mapping",
    "E": "Product matching",
    "F": "Value normalization",
    "G": "Ground-truth or scoring mismatch",
    "H": "Other / undetermined",
}

# Production column_matcher patterns that the Phase2 A/B scorer omits.
_SCORER_GAP_OPENING = re.compile(r"op\.?\s*stk|\bopstk\b", re.I)
_SCORER_GAP_CLOSING = re.compile(r"^bal\.?$|\bbalance\b", re.I)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_vl(document_id: str) -> Tuple[str, List[Dict[str, Any]], Optional[int]]:
    path = VL_DIR / f"{document_id}.json"
    if not path.is_file():
        return "", [], None
    payload = json.loads(path.read_text(encoding="utf-8"))
    pages = []
    texts = []
    page_no = None
    for page in payload.get("pages") or []:
        if not isinstance(page, dict):
            continue
        if page.get("status") != "success":
            continue
        pages.append(page)
        texts.append(str(page.get("text") or ""))
        if page_no is None:
            page_no = int(page.get("page") or 1)
    return "\n\n".join(texts), pages, page_no


def _cell_text(cell: Dict[str, Any]) -> str:
    return str(cell.get("value") if cell.get("value") is not None else cell.get("text") or "")


def find_product_row_cells(
    cells: List[Dict[str, Any]], product_key: str
) -> List[Dict[str, Any]]:
    target = _normalize_product_key(product_key)
    if not target:
        return []
    # Prefer cells already tagged with product_key by parse_html_tables.
    tagged = [
        c
        for c in cells
        if not c.get("is_header")
        and _normalize_product_key(str(c.get("product_key") or ""))
        and (
            target == _normalize_product_key(str(c.get("product_key") or ""))
            or target in _normalize_product_key(str(c.get("product_key") or ""))
            or _normalize_product_key(str(c.get("product_key") or "")) in target
        )
    ]
    if tagged:
        first = tagged[0]
        return [
            c
            for c in cells
            if c.get("row_index") == first.get("row_index")
            and c.get("table_index") == first.get("table_index")
            and not c.get("is_header")
        ]
    hits = []
    for cell in cells:
        text = _normalize_product_key(_cell_text(cell))
        if not text:
            continue
        if text == target or target in text or text in target:
            hits.append(cell)
    if not hits:
        return []
    first = hits[0]
    return [
        c
        for c in cells
        if c.get("row_index") == first.get("row_index")
        and c.get("table_index") == first.get("table_index")
    ]


def cell_value_for_header(row_cells: List[Dict[str, Any]], printed_header: str) -> Optional[str]:
    header_n = _normalize_product_key(printed_header)
    if not header_n:
        return None
    for cell in row_cells:
        hdr = _normalize_product_key(str(cell.get("header") or ""))
        if not hdr:
            continue
        if header_n == hdr or header_n in hdr or hdr in header_n:
            return _cell_text(cell)
    mapped = match_headers([printed_header])
    if not mapped:
        return None
    field = next(iter(mapped.values()), None)
    for cell in row_cells:
        hdr = str(cell.get("header") or "")
        m = match_headers([hdr])
        if field in m.values():
            return _cell_text(cell)
    return None


def expected_in_ocr_text(expected_raw: str, ocr_text: str) -> bool:
    raw = str(expected_raw or "").strip()
    if not raw:
        return False
    if raw in ocr_text:
        return True
    num = normalize_numeric(raw)
    if num is None:
        return False
    # Accept int form without trailing .0
    candidates = {str(num), f"{num:g}"}
    if float(num).is_integer():
        candidates.add(str(int(num)))
    return any(c in ocr_text.replace(",", "") for c in candidates)


def product_in_ocr(product_key: str, ocr_text: str) -> bool:
    target = _normalize_product_key(product_key)
    hay = _normalize_product_key(ocr_text)
    if not target or not hay:
        return False
    if target in hay:
        return True
    # Token overlap (≥2 significant tokens).
    toks = [t for t in target.split() if len(t) >= 4]
    if len(toks) >= 2 and sum(1 for t in toks if t in hay) >= 2:
        return True
    if len(toks) == 1 and toks[0] in hay:
        return True
    return False


def scorer_gap_field(printed_header: str) -> Optional[str]:
    """If production maps header but Phase2 scorer `_canonical_field` does not."""
    hdr = str(printed_header or "")
    if _canonical_field(hdr):
        return None
    mapped = match_headers([hdr])
    if not mapped:
        # Known scorer gaps not always in single-header match_headers alone.
        if _SCORER_GAP_OPENING.search(hdr):
            return "opening_qty"
        if _SCORER_GAP_CLOSING.search(hdr) and not re.search(r"open|op\.?", hdr, re.I):
            return "closing_qty"
        return None
    field = next(iter(mapped.values()))
    # Scorer uses receipts_qty alias.
    if field == "receive_qty":
        return "receipts_qty"
    return field


def classify(
    *,
    error_type: str,
    item: Optional[Dict[str, Any]],
    parsed_cell: Optional[str],
    expected_raw: str,
    predicted_raw: Any,
    ocr_has_expected: bool,
    ocr_has_product: bool,
    row_found_in_html: bool,
    scorer_alt_field: Optional[str],
    scorer_alt_value: Any,
    empty_qty_row: bool,
    headers_garbage: bool,
    n_extract_items: int,
    ambiguous_pack: bool,
) -> Tuple[str, str, bool]:
    """Return (category, reason, confirmed)."""
    # G: scorer cannot map header but extraction holds correct value.
    if error_type == "UNMAPPED_HEADER" and scorer_alt_field and scorer_alt_value is not None:
        if exact_numeric_match(expected_raw, str(scorer_alt_value)):
            return (
                "G",
                "Phase2 scorer `_canonical_field` misses header synonym that production "
                f"column_matcher maps to `{scorer_alt_field}`; extracted value matches GT",
                True,
            )

    if error_type == "MISSING_PRODUCT":
        if headers_garbage and n_extract_items == 0:
            return (
                "D",
                "OCR destroyed qty headers (garbage tokens); fuzzy mapper kept only "
                "product_name → zero qty line_items",
                True,
            )
        if n_extract_items == 0 and (ocr_has_product or row_found_in_html):
            return (
                "D",
                "HTML table present and product visible in OCR, but column mapping "
                "produced no usable qty columns / empty extract",
                True,
            )
        if row_found_in_html and item is None:
            return (
                "E",
                "Product row exists in parsed HTML but was not present/matched in "
                "extraction line_items (name normalization / row drop)",
                True,
            )
        if ocr_has_product and item is None:
            return (
                "E",
                "Product present in OCR/HTML but not matched into extraction line_items "
                "(name normalization / row drop)",
                True,
            )
        if not ocr_has_product and ocr_has_expected:
            return (
                "A",
                "Expected quantity appears in OCR text but GT product string is absent "
                "or OCR-mangled (product identity OCR failure)",
                True,
            )
        if not ocr_has_product and not ocr_has_expected:
            return (
                "A",
                "Neither GT product name nor expected quantity reliably found in stored VL text",
                False,
            )

    if error_type == "WRONG_VALUE":
        if ambiguous_pack:
            return (
                "E",
                "Multiple pack sizes share product stem; scorer/extract took a different "
                "row than GT pack",
                True,
            )
        if empty_qty_row and ocr_has_expected:
            return (
                "C",
                "Product label row has empty qty cells; quantities live on another row "
                "(row alignment / split product row)",
                True,
            )
        if parsed_cell is not None and exact_numeric_match(expected_raw, parsed_cell):
            if predicted_raw is not None and not exact_numeric_match(expected_raw, str(predicted_raw)):
                return (
                    "D",
                    "HTML cell under GT header holds correct value, but mapped canonical "
                    "field on line_item differs (column mapping / Free-vs-Qty)",
                    True,
                )
        if ocr_has_expected and predicted_raw in (0, 0.0, "0", "0.0"):
            return (
                "D",
                "OCR contains expected digit; extract reported 0 — likely Free/Qty column "
                "collision or shifted mapping",
                False,
            )
        if parsed_cell is not None and not exact_numeric_match(expected_raw, parsed_cell):
            if ocr_has_expected:
                return (
                    "B",
                    "Parsed table cell under associated header disagrees with GT while "
                    "raw OCR text still contains expected token",
                    False,
                )
            return (
                "A",
                "Parsed cell and OCR lack exact GT digit — OCR digit error suspected",
                False,
            )
        if not ocr_has_expected:
            return (
                "A",
                "Expected quantity not found in stored VL OCR text",
                False,
            )

    if predicted_raw is not None and exact_numeric_match(expected_raw, str(predicted_raw)):
        return (
            "G",
            "Extracted value matches GT but was marked incorrect — scoring bug",
            True,
        )

    return ("H", "Insufficient stage evidence to confirm a single cause", False)


def headers_look_garbage(headers: List[str]) -> bool:
    if not headers:
        return False
    bad = 0
    for h in headers:
        t = str(h or "").strip().lower()
        if not t:
            continue
        if re.search(r"\d+\s*g\b|0tv|0g|a2m", t) or t.count("\n") >= 1 and "qty" not in t:
            bad += 1
    return bad >= 2


def analyze_doc(document_id: str) -> Dict[str, Any]:
    bench_path = PER / f"{document_id}.json"
    gt_path = GT_DIR / f"{document_id}.json"
    bench = json.loads(bench_path.read_text(encoding="utf-8")) if bench_path.is_file() else {}
    gt = json.loads(gt_path.read_text(encoding="utf-8")) if gt_path.is_file() else {}
    ocr_text, vl_pages, vl_page = load_vl(document_id)
    html = extract_vl_table_html(ocr_text)
    cells = parse_html_tables(html or ocr_text) if (html or ocr_text) else []
    parsed = (
        parse_vl_html_table(html or ocr_text, filename=document_id, allow_llm=False)
        if (html or "<table" in ocr_text.lower())
        else {"line_items": [], "totals": {"extra": {}}}
    )
    parsed_items = list(parsed.get("line_items") or [])
    extract_result = (bench.get("off") or {}).get("result") or {}
    extract_items = _line_items(extract_result)
    columns_matched = ((parsed.get("totals") or {}).get("extra") or {}).get("columns_matched") or {}
    columns_unmatched = ((parsed.get("totals") or {}).get("extra") or {}).get("columns_unmatched") or []

    # Header row from cells
    header_names = []
    if cells:
        # first row headers unique by col
        by_col = {}
        for c in cells:
            if c.get("row_index") == 0 or c.get("is_header"):
                by_col[c.get("col_index")] = str(c.get("header") or c.get("text") or "")
        if not by_col:
            for c in cells:
                h = str(c.get("header") or "")
                if h and h not in header_names:
                    header_names.append(h)
        else:
            header_names = [by_col[k] for k in sorted(by_col)]
    garbage = headers_look_garbage(list(columns_unmatched) + header_names)

    page = int(gt.get("page") or vl_page or 1)
    traces = []
    stage = {
        "ocr_qty_correct": 0,
        "ocr_qty_total": 0,
        "table_cell_correct": 0,
        "table_cell_total": 0,
        "row_aligned": 0,
        "row_align_total": 0,
        "header_mapped": 0,
        "header_map_total": 0,
        "final_qty_correct": 0,
        "final_qty_total": 0,
        "product_row_exact": 0,
        "product_row_total": 0,
    }

    matches = (bench.get("off") or {}).get("matches") or []
    # Also compute stages for correct quantities for denominators.
    qty_matches = [m for m in matches if m.get("field_type") == "quantity"]

    # Product-row accuracy: GT products with all qty fields correct.
    by_product: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for m in qty_matches:
        if m.get("product_key"):
            by_product[str(m["product_key"])].append(m)

    for pk, fields in by_product.items():
        stage["product_row_total"] += 1
        if fields and all(f.get("correct") for f in fields):
            stage["product_row_exact"] += 1

    for m in qty_matches:
        expected = str(m.get("expected_raw") or "")
        product_key = str(m.get("product_key") or "")
        printed_header = str(m.get("printed_header") or "")
        predicted = m.get("predicted_raw")
        correct = bool(m.get("correct"))
        error_type = m.get("error_type")

        stage["final_qty_total"] += 1
        if correct:
            stage["final_qty_correct"] += 1

        ocr_has_expected = expected_in_ocr_text(expected, ocr_text)
        stage["ocr_qty_total"] += 1
        if ocr_has_expected:
            stage["ocr_qty_correct"] += 1

        row_cells = find_product_row_cells(cells, product_key)
        stage["row_align_total"] += 1
        if row_cells:
            stage["row_aligned"] += 1

        parsed_cell = cell_value_for_header(row_cells, printed_header) if row_cells else None
        stage["table_cell_total"] += 1
        if parsed_cell is not None and exact_numeric_match(expected, parsed_cell):
            stage["table_cell_correct"] += 1

        stage["header_map_total"] += 1
        canon = _canonical_field(printed_header)
        prod_map = match_headers([printed_header])
        if canon or prod_map:
            stage["header_mapped"] += 1

        item = _find_line_item(extract_items, product_key)
        # Ambiguous pack: multiple extract items share stem.
        stem = _normalize_product_key(product_key).split()[:1]
        ambiguous = False
        if stem:
            hits = [
                i
                for i in extract_items
                if stem[0] and stem[0] in _normalize_product_key(str(i.get("product_name") or ""))
            ]
            ambiguous = len(hits) > 1 and item is not None

        empty_qty_row = False
        if row_cells:
            nums = [
                c
                for c in row_cells
                if re.search(r"\d", _cell_text(c))
                and _normalize_product_key(_cell_text(c))
                != _normalize_product_key(product_key)
            ]
            empty_qty_row = len(nums) == 0

        scorer_alt = scorer_gap_field(printed_header)
        scorer_alt_value = None
        if item is not None and scorer_alt:
            scorer_alt_value = _qty_from_item(item, scorer_alt)
            if scorer_alt_value is None and scorer_alt == "receipts_qty":
                scorer_alt_value = item.get("receive_qty")

        bbox = None
        for f in only_source_verified_fields(gt) if gt else []:
            if f.get("field_id") == m.get("field_id"):
                bbox = f.get("bbox")
                break

        if correct:
            continue

        cat, reason, confirmed = classify(
            error_type=str(error_type or "MISSING"),
            item=item,
            parsed_cell=parsed_cell,
            expected_raw=expected,
            predicted_raw=predicted,
            ocr_has_expected=ocr_has_expected,
            ocr_has_product=product_in_ocr(product_key, ocr_text),
            row_found_in_html=bool(row_cells),
            scorer_alt_field=scorer_alt,
            scorer_alt_value=scorer_alt_value,
            empty_qty_row=empty_qty_row,
            headers_garbage=garbage,
            n_extract_items=len(extract_items),
            ambiguous_pack=ambiguous,
        )

        # Source function hints
        source_fn = {
            "A": "services/ocr/paddle_vl_engine.py (stored page text)",
            "B": "services/ground_truth/html_table.py::parse_html_tables / services/html_table/html_table_parser.py::parse_html_table",
            "C": "services/ground_truth/html_table.py::_expand_span_grid / row split",
            "D": "services/html_table/column_matcher.py::match_headers + html_table_parser.resolve_column_mapping",
            "E": "scripts/benchmark_phase2_ab_gt20.py::_find_line_item / html_table_parser product_col",
            "F": "services/html_table/html_table_parser.py::parse_numeric",
            "G": "scripts/benchmark_phase2_ab_gt20.py::_canonical_field / score_extraction",
            "H": "undetermined",
        }[cat]

        traces.append(
            {
                "document_id": document_id,
                "page": page,
                "product_name": product_key,
                "field_id": m.get("field_id"),
                "field_name": printed_header,
                "canonical_scorer_field": canon,
                "ground_truth_value": expected,
                "raw_ocr_has_expected_token": ocr_has_expected,
                "raw_ocr_product_present": product_in_ocr(product_key, ocr_text),
                "parsed_table_cell_value": parsed_cell,
                "final_extracted_value": predicted,
                "extract_line_item": {
                    "product_name": (item or {}).get("product_name"),
                    "opening_qty": (item or {}).get("opening_qty"),
                    "receipts_qty": (item or {}).get("receipts_qty")
                    if item
                    else None,
                    "receive_qty": (item or {}).get("receive_qty") if item else None,
                    "sales_qty": (item or {}).get("sales_qty") if item else None,
                    "closing_qty": (item or {}).get("closing_qty") if item else None,
                }
                if item
                else None,
                "bbox": bbox,
                "bench_error_type": error_type,
                "failure_category": cat,
                "failure_category_name": CATEGORY[cat],
                "confirmed": confirmed,
                "reason": reason,
                "source_file_function": source_fn,
                "columns_matched": columns_matched,
                "columns_unmatched": columns_unmatched,
                "n_extract_items": len(extract_items),
                "n_parsed_items": len(parsed_items),
                "empty_qty_row": empty_qty_row,
                "headers_garbage": garbage,
            }
        )

    return {
        "document_id": document_id,
        "page": page,
        "extraction_method": (
            ((extract_result.get("totals") or {}).get("extra") or {}).get(
                "extraction_method"
            )
        ),
        "n_vl_pages": len(vl_pages),
        "html_chars": len(html or ""),
        "n_extract_items": len(extract_items),
        "n_parsed_items": len(parsed_items),
        "columns_matched": columns_matched,
        "columns_unmatched": columns_unmatched,
        "stage_metrics": stage,
        "incorrect_quantity_traces": traces,
        "field_accuracy_off": ((bench.get("off") or {}).get("score") or {})
        .get("metrics", {})
        .get("field_accuracy"),
        "quantity_accuracy_off": ((bench.get("off") or {}).get("score") or {}).get(
            "quantity_accuracy"
        ),
    }


def main() -> int:
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    docs = list(seed.get("document_ids") or [])
    results = []
    all_traces: List[Dict[str, Any]] = []
    stage_sum = Counter()
    stage_den = Counter()
    cat_counts = Counter()
    confirmed_counts = Counter()

    print(f"investigating docs={len(docs)}", flush=True)
    for i, document_id in enumerate(docs, 1):
        print(f"[{i}/{len(docs)}] {document_id}", flush=True)
        row = analyze_doc(document_id)
        results.append(row)
        all_traces.extend(row["incorrect_quantity_traces"])
        for k, v in (row.get("stage_metrics") or {}).items():
            if k.endswith("_total"):
                stage_den[k] += int(v or 0)
            else:
                stage_sum[k] += int(v or 0)
        for t in row["incorrect_quantity_traces"]:
            cat_counts[t["failure_category"]] += 1
            if t.get("confirmed"):
                confirmed_counts[t["failure_category"]] += 1

    def ratio(num_key: str, den_key: str) -> Optional[float]:
        d = stage_den[den_key]
        return (stage_sum[num_key] / d) if d else None

    metrics = {
        "raw_ocr_numeric_accuracy": {
            "correct": stage_sum["ocr_qty_correct"],
            "total": stage_den["ocr_qty_total"],
            "accuracy": ratio("ocr_qty_correct", "ocr_qty_total"),
            "note": "Expected qty token present in stored VL text (not cell-aligned).",
        },
        "table_cell_extraction_accuracy": {
            "correct": stage_sum["table_cell_correct"],
            "total": stage_den["table_cell_total"],
            "accuracy": ratio("table_cell_correct", "table_cell_total"),
            "note": "HTML cell under GT product row + header matches expected.",
        },
        "row_alignment_accuracy": {
            "correct": stage_sum["row_aligned"],
            "total": stage_den["row_align_total"],
            "accuracy": ratio("row_aligned", "row_align_total"),
            "note": "GT product string found as a cell in parsed HTML table.",
        },
        "header_mapping_accuracy": {
            "correct": stage_sum["header_mapped"],
            "total": stage_den["header_map_total"],
            "accuracy": ratio("header_mapped", "header_map_total"),
            "note": "Printed header maps via scorer `_canonical_field` OR production match_headers.",
        },
        "final_quantity_accuracy": {
            "correct": stage_sum["final_qty_correct"],
            "total": stage_den["final_qty_total"],
            "accuracy": ratio("final_qty_correct", "final_qty_total"),
            "note": "Phase2-OFF score_extraction quantity fields (published ~46.5%).",
        },
        "exact_product_row_accuracy": {
            "correct": stage_sum["product_row_exact"],
            "total": stage_den["product_row_total"],
            "accuracy": ratio("product_row_exact", "product_row_total"),
            "note": "All quantity fields for a GT product_key correct.",
        },
    }

    payload = {
        "created_at": _utc(),
        "scope": "SOURCE_VERIFIED GT-20, Phase2 OFF, stored PaddleOCR-VL",
        "published_reference": {
            "field_accuracy": 0.5180412371134021,
            "quantity_accuracy": 0.4652777777777778,
            "source": "phase4/phase2_ab_gt20/COMPARISON_REPORT.md",
        },
        "ocr_direct_eval_reference": {
            "quantity_accuracy": 0.993,
            "source": "phase4/PADDLE_VL_GROUND_TRUTH_20_REPORT.md",
            "note": "Direct OCR/HTML match_field eval — NOT extraction JSON scoring.",
        },
        "stage_metrics": metrics,
        "error_category_counts": {
            k: {
                "count": cat_counts[k],
                "confirmed": confirmed_counts[k],
                "name": CATEGORY[k],
            }
            for k in "ABCDEFGH"
            if cat_counts[k]
        },
        "incorrect_quantity_count": len(all_traces),
        "documents": results,
        "all_incorrect_quantity_traces": all_traces,
    }

    (OUT / "investigation_aggregate.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "incorrect_quantity_traces.json").write_text(
        json.dumps(all_traces, indent=2, default=str), encoding="utf-8"
    )
    print(
        f"done incorrect={len(all_traces)} cats={dict(cat_counts)} "
        f"final_qty={metrics['final_quantity_accuracy']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
