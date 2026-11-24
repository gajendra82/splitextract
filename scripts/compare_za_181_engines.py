"""Paired output comparison for ZA images that have both OCR caches.

Agreement is not accuracy. Source-verified labels are not used here.
Both-missing quantity fields are excluded from the agreement denominator.
"""

from __future__ import annotations

import csv
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ground_truth.html_table import iter_expanded_tables

TESS_RUN = (
    ROOT
    / "phase4"
    / "secondary_repair_canary"
    / "controlled_batch"
    / "20261013T385tess"
)
VL_RUN = (
    ROOT
    / "phase4"
    / "secondary_repair_canary"
    / "controlled_batch"
    / "20261013T385vl"
)
OUT_DIR = ROOT / "phase4" / "za_ground_truth" / "evaluation"

QTY_FIELDS = (
    "opening_qty",
    "receipt_qty",
    "sales_qty",
    "sales_scheme_qty",
    "stock_out_qty",
    "closing_qty",
)

_FIELD_PATTERNS = (
    ("opening_qty", re.compile(r"\bob\b|opg\s*qty|op\.?\s*qty|op\.?\s*stk|opening(?:\s*stock)?(?!\s*bal\s*value)(?!\s*value)|op\.?\s*bal", re.I)),
    ("receipt_qty", re.compile(r"receipt\s*qty|receive\s*(?:qty|quantity)|purchase\s*qty|pur(?:chase)?\s*qty|inw\s*qty|primary\s*\(\s*qty\s*\)", re.I)),
    ("sales_qty", re.compile(r"issue\s*(?:qty|quantity)|sales?\s*qty|out\s*qty", re.I)),
    ("sales_scheme_qty", re.compile(r"scheme|free\s*qty|f\.?\s*qty|sale\s*free|pur\s*free", re.I)),
    ("stock_out_qty", re.compile(r"stock[\s-]*out|breakage|dump\s*qty", re.I)),
    ("closing_qty", re.compile(r"clos(?:e|ing)\s*(?:qty|quantity|stock)|cl\s*qty|clg\s*qty|cl\s*stk|closing\s*bal|bal\.?\s*qty|\bcb\b", re.I)),
)

_BANNER_RE = re.compile(
    r"(?i)^(from|to|phone|gstin|page|total|grand\s*total|date)\b|sales\s*&\s*stock|page\s*no|stock\s*&\s*sales"
)
_PLACEHOLDER_RE = re.compile(r"^[-–—=:_.]+$")
_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_PACK_UNIT_RE = re.compile(
    r"^(?:ml|mg|gm|g|kg|s|tab|tabs|cap|caps|pcs|pc|nos)\.?$",
    re.I,
)
_PACK_GLUED_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:ml|mg|gm|g|kg|s|'s|tab|cap|pcs|pc)\.?$",
    re.I,
)


def normalize_name(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def parse_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    return float(text)


def numbers_equal(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) < 1e-9


def map_header(header: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", str(header or "")).strip()
    if not text:
        return None
    if re.search(r"value|amount|amt|rate|price", text, re.I):
        return None
    for name, pattern in _FIELD_PATTERNS:
        if pattern.search(text):
            return name
    return None


def _is_total_name(name: str) -> bool:
    compact = normalize_name(name)
    return compact in {"total", "grandtotal", "totals"} or compact.startswith("total")


def _headers_are_unlabeled(headers: Sequence[str]) -> bool:
    labels = [str(h or "").strip() for h in headers]
    if not labels:
        return True
    compact = {normalize_name(h) for h in labels if normalize_name(h)}
    if len(compact) <= 1:
        return True
    return all(re.fullmatch(r"col\d+", normalize_name(h) or "x") for h in labels)


def _product_column(headers: Sequence[str]) -> int:
    for index, header in enumerate(headers):
        text = str(header or "")
        if re.search(r"value|amount|qty|quantity", text, re.I):
            continue
        if re.search(r"product|item|description|mat\s*name|particular", text, re.I):
            return index
    return 0


def _pack_column(headers: Sequence[str], product_idx: int) -> Optional[int]:
    for index, header in enumerate(headers):
        if index == product_idx:
            continue
        if re.search(r"(?i)^pack(?:ing|age)?$|uom", str(header or "").strip()):
            return index
    return None


def _shift_pack_out_of_qty_column(
    headers: Sequence[str], rows: List[List[str]]
) -> Tuple[List[List[str]], List[Optional[str]], bool]:
    """If the first quantity header holds a packing token on most rows, drop that cell.

    This reads the inserted packing token. It does not use the stock equation.
    """
    empty = [None for _ in rows]
    mapped = [map_header(h) for h in headers]
    qty_indexes = [i for i, name in enumerate(mapped) if name]
    if not qty_indexes:
        return rows, empty, False
    first = qty_indexes[0]
    seen = 0
    pack_cells = 0
    for row in rows:
        if not row or not re.search(r"[A-Za-z]", str(row[0] or "")):
            continue
        if first >= len(row):
            continue
        seen += 1
        if re.search(r"[A-Za-z]", str(row[first] or "")):
            pack_cells += 1
    if seen < 3 or pack_cells / seen < 0.6:
        return rows, empty, False
    shifted: List[List[str]] = []
    packings: List[Optional[str]] = []
    for row in rows:
        values = list(row)
        packing = None
        if first < len(values) and re.search(r"[A-Za-z]", str(values[first] or "")):
            packing = str(values.pop(first) or "").strip() or None
        shifted.append(values)
        packings.append(packing)
    return shifted, packings, True


def _positional_numbers(values: Sequence[Any], skip: Sequence[int]) -> List[Optional[float]]:
    positions: List[Optional[float]] = []
    for index, value in enumerate(values):
        if index in skip:
            continue
        text = str(value or "").strip()
        if not text or _PLACEHOLDER_RE.fullmatch(text):
            positions.append(None)
            continue
        number = parse_number(text)
        if number is None and re.search(r"[A-Za-z]", text):
            continue
        positions.append(number)
    return positions


def rows_from_html(text: str) -> Tuple[List[Dict[str, Any]], bool]:
    tables = iter_expanded_tables(text or "")
    rows: List[Dict[str, Any]] = []
    shifted_any = False
    for table in tables:
        headers = list(table.get("headers") or [])
        body = [list(raw) for raw in (table.get("rows") or []) if raw]
        body, shifted_packings, shifted = _shift_pack_out_of_qty_column(headers, body)
        shifted_any = shifted_any or shifted
        mapped = [map_header(h) for h in headers]
        unlabeled = _headers_are_unlabeled(headers)
        product_idx = 0 if unlabeled else _product_column(headers)
        pack_idx = None if unlabeled else _pack_column(headers, product_idx)
        if unlabeled and body:
            packish = 0
            checked = 0
            for raw in body[:30]:
                if len(raw) < 2 or not re.search(r"[A-Za-z]", str(raw[0] or "")):
                    continue
                checked += 1
                cell = str(raw[1] or "")
                if _PACK_GLUED_RE.fullmatch(cell.strip(".")) or re.search(r"(?i)\d.*(?:ml|mg|gm|tab|cap|s)\b", cell):
                    packish += 1
            if checked and packish / checked >= 0.5:
                pack_idx = 1
        for row_index, values in enumerate(body):
            if product_idx >= len(values):
                continue
            product = str(values[product_idx] or "").strip()
            if not re.search(r"[A-Za-z]", product) or _is_total_name(product) or _BANNER_RE.search(product):
                continue
            packing = shifted_packings[row_index] if row_index < len(shifted_packings) else None
            if not packing and pack_idx is not None and pack_idx < len(values):
                packing = str(values[pack_idx] or "").strip() or None
            fields: Dict[str, float] = {}
            if not unlabeled:
                for index, value in enumerate(values):
                    if index >= len(mapped) or not mapped[index]:
                        continue
                    number = parse_number(value)
                    if number is None:
                        continue
                    fields.setdefault(mapped[index], number)
            skip = [product_idx]
            if pack_idx is not None:
                skip.append(pack_idx)
            positions = _positional_numbers(values, skip)
            product, packing = peel_embedded_pack(product, packing)
            if not product or (not fields and not any(pos is not None for pos in positions) and not packing):
                continue
            rows.append(
                {
                    "product": product,
                    "packing": packing,
                    "fields": fields,
                    "positions": positions,
                    "source": "html_table",
                }
            )
    return rows, shifted_any


def _clean_token(token: str) -> str:
    return token.strip(".,'’\"|_")


def _is_tail_noise(token: str) -> bool:
    cleaned = _clean_token(token)
    return cleaned.isalpha() and len(cleaned) <= 3 and _PACK_UNIT_RE.fullmatch(cleaned) is None


def _peel_pack_tokens(tokens: Sequence[str]) -> Tuple[List[str], Optional[str]]:
    if not tokens:
        return [], None
    if _PACK_GLUED_RE.fullmatch(_clean_token(tokens[-1])):
        return list(tokens[:-1]), _clean_token(tokens[-1])
    if (
        len(tokens) >= 2
        and _NUMBER_RE.fullmatch(_clean_token(tokens[-2]))
        and _PACK_UNIT_RE.fullmatch(_clean_token(tokens[-1]))
    ):
        packing = f"{_clean_token(tokens[-2])} {_clean_token(tokens[-1])}"
        return list(tokens[:-2]), packing
    return list(tokens), None


def peel_embedded_pack(product: str, packing: Optional[str]) -> Tuple[str, Optional[str]]:
    """Move one trailing pack token out of the product name. Keep names like Liv 52."""
    tokens = str(product or "").replace("|", " ").split()
    if tokens and _NUMBER_RE.fullmatch(_clean_token(tokens[0])) and len(tokens) > 1 and re.search(r"[A-Za-z]", tokens[1]):
        tokens = tokens[1:]
    head, peeled = _peel_pack_tokens(tokens)
    if peeled and not packing:
        packing = peeled
        tokens = head
    else:
        tokens = head if peeled and packing and normalize_name(peeled) == normalize_name(packing) else tokens
    return " ".join(tokens).strip(" .|-_"), packing


def _split_plain_line(line: str) -> Tuple[str, Optional[str], List[Optional[float]]]:
    tokens = line.replace(",", " ").replace("|", " ").split()
    if tokens and _NUMBER_RE.fullmatch(_clean_token(tokens[0])) and len(tokens) > 1 and re.search(r"[A-Za-z]", tokens[1]):
        tokens = tokens[1:]
    name: List[str] = []
    index = 0
    while index < len(tokens):
        cleaned = _clean_token(tokens[index])
        nxt = _clean_token(tokens[index + 1]) if index + 1 < len(tokens) else ""
        if _PACK_GLUED_RE.fullmatch(cleaned) or _PLACEHOLDER_RE.fullmatch(tokens[index]):
            break
        if _NUMBER_RE.fullmatch(cleaned):
            if _PACK_UNIT_RE.fullmatch(nxt):
                break
            if nxt and re.search(r"[A-Za-z]", nxt) and not _PACK_UNIT_RE.fullmatch(nxt):
                name.append(tokens[index])
                index += 1
                continue
            break
        if not re.search(r"[A-Za-z]", cleaned):
            break
        name.append(tokens[index])
        index += 1
    _prefix, packing, rest = _take_pack_prefix(tokens[index:])
    product = " ".join(name).strip(" .|-_")
    positions: List[Optional[float]] = []
    for token in rest:
        cleaned = _clean_token(token)
        if _PLACEHOLDER_RE.fullmatch(token) or _PLACEHOLDER_RE.fullmatch(cleaned) or _is_tail_noise(token):
            positions.append(None)
        else:
            number = parse_number(cleaned)
            if number is not None:
                positions.append(number)
    return product, packing, positions


def _take_pack_prefix(tokens: Sequence[str]) -> Tuple[List[str], Optional[str], List[str]]:
    if not tokens:
        return [], None, []
    cleaned = _clean_token(tokens[0])
    if _PACK_GLUED_RE.fullmatch(cleaned):
        return [], cleaned, list(tokens[1:])
    if len(tokens) >= 2 and _NUMBER_RE.fullmatch(cleaned) and _PACK_UNIT_RE.fullmatch(_clean_token(tokens[1])):
        packing = f"{cleaned} {_clean_token(tokens[1])}"
        return [], packing, list(tokens[2:])
    return [], None, list(tokens)


def rows_from_plain(text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if len(line) < 6 or not re.search(r"[A-Za-z]", line):
            continue
        product, packing, positions = _split_plain_line(line)
        if len(re.sub(r"[^A-Za-z]", "", product)) < 3 or _is_total_name(product):
            continue
        if _BANNER_RE.search(product) or re.search(r"(?i)product\s*name|page\s*no", product):
            continue
        if not packing and not any(pos is not None for pos in positions):
            continue
        rows.append(
            {
                "product": product,
                "packing": packing,
                "fields": {},
                "positions": positions,
                "numbers": [pos for pos in positions if pos is not None],
                "source": "plain_line",
            }
        )
    return rows


def _header_field_plan(text: str) -> Tuple[List[Optional[str]], bool]:
    """Return named slots and whether body numbers are qty/value pairs.

    Slots follow the header words on one printed line. A None slot skips one
    number for the unlabeled middle column on Out Qty / Cl Qty pages.
    """
    lines = (text or "").splitlines()[:25]
    head = "\n".join(lines).casefold()
    if "out qty" in head and re.search(r"cl(?:osing)?\s*qty", head) and "opening" not in head:
        return ["sales_qty", None, "closing_qty"], False
    token_fields = (
        ("opening_qty", re.compile(r"^(?:opening|opg|opstk)$")),
        ("receipt_qty", re.compile(r"^(?:receipt|receive|purchase|pur|inw)$")),
        ("sales_qty", re.compile(r"^(?:issue|sales|sale|out)$")),
        ("sales_scheme_qty", re.compile(r"^(?:scheme|free)$")),
        ("stock_out_qty", re.compile(r"^(?:dump|breakage)$")),
        ("closing_qty", re.compile(r"^(?:closing|clg|cl|cb)$")),
    )
    for index, line in enumerate(lines):
        found: List[str] = []
        for token in re.findall(r"[a-z]+", line.casefold()):
            for name, pattern in token_fields:
                if pattern.fullmatch(token) and name not in found:
                    found.append(name)
                    break
        if len(found) < 2:
            continue
        nxt = lines[index + 1].casefold() if index + 1 < len(lines) else ""
        pair = line.casefold().count("value") >= 2 or nxt.count("value") >= 2
        return found, pair
    return [], False


def assign_plain_fields(rows: List[Dict[str, Any]], text: str) -> None:
    """Assign quantity columns only when this OCR text prints those headers."""
    fields, pair_mode = _header_field_plan(text)
    if len([field for field in fields if field]) < 2:
        return
    for row in rows:
        if row.get("source") != "plain_line":
            continue
        positions = list(row.get("positions") or [])
        if pair_mode:
            positions = positions[0::2]
        for field, value in zip(fields, positions):
            if field and value is not None:
                row["fields"][field] = value


def extract_rows(text: str) -> Tuple[List[Dict[str, Any]], bool]:
    html_rows, shifted = rows_from_html(text)
    if html_rows:
        return html_rows, shifted
    rows = rows_from_plain(text)
    assign_plain_fields(rows, text)
    return rows, False


def index_rows(rows: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """First occurrence wins. Later copies are duplicates, not extra products."""
    indexed: Dict[str, Dict[str, Any]] = {}
    duplicates = 0
    order = 0
    for row in rows:
        key = normalize_name(row.get("product"))
        if not key:
            continue
        if key in indexed:
            duplicates += 1
            continue
        indexed[key] = {**row, "order": order}
        order += 1
    return indexed, duplicates


def compare_quantity_fields(
    left: Dict[str, float], right: Dict[str, float]
) -> List[Dict[str, Any]]:
    results = []
    for field in QTY_FIELDS:
        has_left = field in left
        has_right = field in right
        if not has_left and not has_right:
            continue
        if has_left and has_right:
            status = "agree" if numbers_equal(left[field], right[field]) else "disagree"
        elif has_left:
            status = "missing_paddle"
        else:
            status = "missing_tesseract"
        results.append(
            {
                "field": field,
                "tesseract": left.get(field),
                "paddle": right.get(field),
                "status": status,
            }
        )
    return results


def classify_disagreement(field: str, status: str, both_products: bool) -> str:
    if status in {"missing_tesseract", "missing_paddle"} and both_products:
        return "missing_row" if field == "product_name" else "column_alignment"
    if field == "product_name":
        return "product_name_recognition"
    if field == "packing":
        return "packing_unit"
    if field in QTY_FIELDS:
        return "quantity_digit"
    return "other"


def compare_document(
    document_id: str,
    tess_text: str,
    vl_text: str,
    *,
    tess_ms: Optional[float] = None,
    vl_ms: Optional[float] = None,
    tess_cohort: Optional[str] = None,
    vl_cohort: Optional[str] = None,
    sha_match: Optional[bool] = None,
) -> Dict[str, Any]:
    tess_extracted, _tess_shifted = extract_rows(tess_text)
    vl_extracted, vl_shifted = extract_rows(vl_text)
    tess_rows, tess_dups = index_rows(tess_extracted)
    vl_rows, vl_dups = index_rows(vl_extracted)
    tess_keys = list(tess_rows)
    vl_keys = list(vl_rows)
    shared = [key for key in tess_keys if key in vl_rows]
    only_tess = [key for key in tess_keys if key not in vl_rows]
    only_vl = [key for key in vl_keys if key not in tess_rows]
    union = len(shared) + len(only_tess) + len(only_vl)
    order_ok = True
    last = -1
    for key in shared:
        pos = vl_rows[key]["order"]
        if pos < last:
            order_ok = False
            break
        last = pos

    disagreements: List[Dict[str, Any]] = []
    for key in only_tess:
        disagreements.append(
            {
                "document_id": document_id,
                "product": tess_rows[key]["product"],
                "field": "product_name",
                "tesseract": tess_rows[key]["product"],
                "paddleocr_vl": "",
                "category": "missing_row",
            }
        )
    for key in only_vl:
        disagreements.append(
            {
                "document_id": document_id,
                "product": vl_rows[key]["product"],
                "field": "product_name",
                "tesseract": "",
                "paddleocr_vl": vl_rows[key]["product"],
                "category": "extra_row",
            }
        )

    qty_compared = 0
    qty_agree = 0
    positional_compared = 0
    positional_agree = 0
    packing_compared = 0
    packing_agree = 0
    field_counts = {field: {"agree": 0, "disagree": 0, "missing_tesseract": 0, "missing_paddle": 0} for field in QTY_FIELDS}
    for key in shared:
        left = tess_rows[key]
        right = vl_rows[key]
        if left.get("packing") and right.get("packing"):
            packing_compared += 1
            same = normalize_name(left["packing"]) == normalize_name(right["packing"])
            if same:
                packing_agree += 1
            else:
                disagreements.append(
                    {
                        "document_id": document_id,
                        "product": left["product"],
                        "field": "packing",
                        "tesseract": left.get("packing") or "",
                        "paddleocr_vl": right.get("packing") or "",
                        "category": "packing_unit",
                    }
                )
        named = compare_quantity_fields(left.get("fields") or {}, right.get("fields") or {})
        for item in named:
            qty_compared += 1
            field_counts[item["field"]][item["status"]] += 1
            if item["status"] == "agree":
                qty_agree += 1
                continue
            disagreements.append(
                {
                    "document_id": document_id,
                    "product": left["product"],
                    "field": item["field"],
                    "tesseract": "" if item["tesseract"] is None else item["tesseract"],
                    "paddleocr_vl": "" if item["paddle"] is None else item["paddle"],
                    "category": classify_disagreement(item["field"], item["status"], True),
                }
            )
        if not named:
            left_pos = list(left.get("positions") or [])
            right_pos = list(right.get("positions") or [])
            width = max(len(left_pos), len(right_pos))
            for index in range(width):
                left_value = left_pos[index] if index < len(left_pos) else None
                right_value = right_pos[index] if index < len(right_pos) else None
                if left_value is None and right_value is None:
                    continue
                positional_compared += 1
                if left_value is not None and right_value is not None and numbers_equal(left_value, right_value):
                    positional_agree += 1
                    continue
                if left_value is None:
                    status = "missing_tesseract"
                elif right_value is None:
                    status = "missing_paddle"
                else:
                    status = "disagree"
                disagreements.append(
                    {
                        "document_id": document_id,
                        "product": left["product"],
                        "field": f"positional_qty_{index + 1}",
                        "tesseract": "" if left_value is None else left_value,
                        "paddleocr_vl": "" if right_value is None else right_value,
                        "category": "column_alignment" if status != "disagree" else "quantity_digit",
                    }
                )
        if not order_ok:
            pass

    if not order_ok and shared:
        disagreements.append(
            {
                "document_id": document_id,
                "product": "",
                "field": "product_order",
                "tesseract": " | ".join(tess_rows[k]["product"] for k in shared),
                "paddleocr_vl": " | ".join(vl_rows[k]["product"] for k in shared),
                "category": "other",
            }
        )

    product_agreement = (len(shared) / union) if union else None
    qty_agreement = (qty_agree / qty_compared) if qty_compared else None
    exact = (
        union > 0
        and not only_tess
        and not only_vl
        and qty_compared > 0
        and qty_agree == qty_compared
        and packing_agree == packing_compared
        and order_ok
    )
    review = bool(
        (product_agreement is not None and product_agreement < 0.5)
        or (qty_agreement is not None and qty_agreement < 0.5 and qty_compared >= 3)
        or len(disagreements) >= 8
    )
    return {
        "document_id": document_id,
        "sha_match": sha_match,
        "tess_cohort": tess_cohort,
        "vl_cohort": vl_cohort,
        "tess_products": len(tess_rows),
        "vl_products": len(vl_rows),
        "shared_products": len(shared),
        "only_tesseract_products": len(only_tess),
        "only_paddle_products": len(only_vl),
        "product_union": union,
        "tess_duplicate_products": tess_dups,
        "vl_duplicate_products": vl_dups,
        "product_order_agrees": order_ok if shared else None,
        "product_agreement": product_agreement,
        "qty_compared": qty_compared,
        "qty_agree": qty_agree,
        "qty_agreement": qty_agreement,
        "positional_compared": positional_compared,
        "positional_agree": positional_agree,
        "vl_pack_column_shifted": vl_shifted,
        "packing_compared": packing_compared,
        "packing_agree": packing_agree,
        "field_counts": field_counts,
        "disagreement_count": len(disagreements),
        "exact_document_agreement": exact,
        "manual_review": review,
        "tess_ms": tess_ms,
        "vl_ms": vl_ms,
        "vl_more_products": len(vl_rows) > len(tess_rows),
        "tess_more_products": len(tess_rows) > len(vl_rows),
        "disagreements": disagreements,
    }


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def shared_document_ids(vl_run: Path = VL_RUN, tess_run: Path = TESS_RUN) -> List[str]:
    checkpoints = vl_run / "checkpoints.jsonl"
    latest: Dict[str, str] = {}
    if checkpoints.is_file():
        for line in checkpoints.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            doc_id = row.get("document_id")
            if doc_id:
                latest[doc_id] = row.get("status")
    completed = sorted(doc_id for doc_id, status in latest.items() if status == "complete")
    shared = []
    for doc_id in completed:
        stem = Path(doc_id).stem
        vl_cache = vl_run / "primary_ocr_cache" / f"{stem}.json"
        tess_cache = tess_run / "primary_ocr_cache" / f"{stem}.json"
        if vl_cache.is_file() and tess_cache.is_file():
            shared.append(doc_id)
    if len(shared) != len(set(shared)):
        raise RuntimeError("duplicate document ids in shared set")
    return shared


def _load_pair(doc_id: str) -> Dict[str, Any]:
    stem = Path(doc_id).stem
    vl_cache = json.loads((VL_RUN / "primary_ocr_cache" / f"{stem}.json").read_text(encoding="utf-8"))
    tess_cache = json.loads((TESS_RUN / "primary_ocr_cache" / f"{stem}.json").read_text(encoding="utf-8"))
    vl_engine = str(vl_cache.get("engine") or "")
    tess_engine = str(tess_cache.get("engine") or "")
    if "paddleocr-vl" not in tess_engine and tess_engine != "tesseract":
        raise RuntimeError(f"{doc_id} unexpected tesseract cache engine {tess_engine}")
    if tess_engine != "tesseract":
        raise RuntimeError(f"{doc_id} tesseract cache engine is {tess_engine}")
    if "paddleocr-vl" not in vl_engine:
        raise RuntimeError(f"{doc_id} VL cache engine is {vl_engine}")
    vl_per_path = VL_RUN / "per_document" / f"{stem}.json"
    tess_per_path = TESS_RUN / "per_document" / f"{stem}.json"
    vl_per = json.loads(vl_per_path.read_text(encoding="utf-8")) if vl_per_path.is_file() else {}
    tess_per = json.loads(tess_per_path.read_text(encoding="utf-8")) if tess_per_path.is_file() else {}
    sha_match = None
    if vl_cache.get("sha256") and tess_cache.get("sha256"):
        sha_match = vl_cache.get("sha256") == tess_cache.get("sha256")
    return compare_document(
        doc_id,
        tess_cache.get("text") or "",
        vl_cache.get("text") or "",
        tess_ms=tess_per.get("primary_ms"),
        vl_ms=vl_per.get("primary_ms"),
        tess_cohort=tess_per.get("cohort"),
        vl_cohort=vl_per.get("cohort"),
        sha_match=sha_match,
    )


def aggregate(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    images = len(rows)
    tess_products = sum(r["tess_products"] for r in rows)
    vl_products = sum(r["vl_products"] for r in rows)
    shared = sum(r["shared_products"] for r in rows)
    union = sum(r["product_union"] for r in rows)
    qty_compared = sum(r["qty_compared"] for r in rows)
    qty_agree = sum(r["qty_agree"] for r in rows)
    positional_compared = sum(r.get("positional_compared") or 0 for r in rows)
    positional_agree = sum(r.get("positional_agree") or 0 for r in rows)
    exact = sum(1 for r in rows if r["exact_document_agreement"])
    field_totals = {
        field: {"agree": 0, "disagree": 0, "missing_tesseract": 0, "missing_paddle": 0}
        for field in QTY_FIELDS
    }
    for row in rows:
        for field, counts in row["field_counts"].items():
            for key, value in counts.items():
                field_totals[field][key] += value
    field_rates = {}
    for field, counts in field_totals.items():
        both = counts["agree"] + counts["disagree"]
        any_present = both + counts["missing_tesseract"] + counts["missing_paddle"]
        field_rates[field] = {
            **counts,
            "both_present": both,
            "both_present_agreement": (counts["agree"] / both) if both else None,
            "any_present": any_present,
        }
    both_present = sum(item["both_present"] for item in field_rates.values())
    both_present_agree = sum(item["agree"] for item in field_rates.values())
    tess_ms = [r["tess_ms"] for r in rows if isinstance(r.get("tess_ms"), (int, float))]
    vl_ms = [r["vl_ms"] for r in rows if isinstance(r.get("vl_ms"), (int, float))]
    by_cohort: Dict[str, Dict[str, int]] = {}
    for row in rows:
        cohort = row.get("tess_cohort") or "unknown"
        bucket = by_cohort.setdefault(
            cohort, {"images": 0, "shared_products": 0, "product_union": 0, "qty_agree": 0, "qty_compared": 0}
        )
        bucket["images"] += 1
        bucket["shared_products"] += row["shared_products"]
        bucket["product_union"] += row["product_union"]
        bucket["qty_agree"] += row["qty_agree"]
        bucket["qty_compared"] += row["qty_compared"]
    return {
        "images": images,
        "unique_images": len({r["document_id"] for r in rows}),
        "sha_matches": sum(1 for r in rows if r.get("sha_match") is True),
        "sha_mismatches": sum(1 for r in rows if r.get("sha_match") is False),
        "sha_unknown": sum(1 for r in rows if r.get("sha_match") is None),
        "tesseract_products": tess_products,
        "paddle_products": vl_products,
        "avg_tesseract_products": (tess_products / images) if images else None,
        "avg_paddle_products": (vl_products / images) if images else None,
        "shared_products": shared,
        "product_union": union,
        "product_name_agreement": (shared / union) if union else None,
        "only_tesseract_products": sum(r["only_tesseract_products"] for r in rows),
        "only_paddle_products": sum(r["only_paddle_products"] for r in rows),
        "tesseract_duplicate_products": sum(r["tess_duplicate_products"] for r in rows),
        "paddle_duplicate_products": sum(r["vl_duplicate_products"] for r in rows),
        "qty_compared": qty_compared,
        "qty_agree": qty_agree,
        "quantity_agreement": (qty_agree / qty_compared) if qty_compared else None,
        "both_present_qty": both_present,
        "both_present_qty_agree": both_present_agree,
        "both_present_quantity_agreement": (both_present_agree / both_present) if both_present else None,
        "named_qty_images": sum(1 for r in rows if r["qty_compared"]),
        "positional_compared": positional_compared,
        "positional_agree": positional_agree,
        "positional_agreement": (positional_agree / positional_compared) if positional_compared else None,
        "positional_images": sum(1 for r in rows if r.get("positional_compared")),
        "vl_pack_column_shifted_images": sum(1 for r in rows if r.get("vl_pack_column_shifted")),
        "field_rates": field_rates,
        "exact_documents": exact,
        "exact_document_agreement": (exact / images) if images else None,
        "paddle_more_products_images": sum(1 for r in rows if r["vl_more_products"]),
        "tesseract_more_products_images": sum(1 for r in rows if r["tess_more_products"]),
        "equal_product_count_images": sum(
            1 for r in rows if r["tess_products"] == r["vl_products"]
        ),
        "manual_review_images": sum(1 for r in rows if r["manual_review"]),
        "order_compared": sum(1 for r in rows if r["product_order_agrees"] is not None),
        "order_agrees": sum(1 for r in rows if r["product_order_agrees"] is True),
        "runtime": {
            "tesseract_images_with_timing": len(tess_ms),
            "paddle_images_with_timing": len(vl_ms),
            "tesseract_total_ms": sum(tess_ms),
            "paddle_total_ms": sum(vl_ms),
            "tesseract_mean_ms": statistics.mean(tess_ms) if tess_ms else None,
            "paddle_mean_ms": statistics.mean(vl_ms) if vl_ms else None,
            "tesseract_median_ms": statistics.median(tess_ms) if tess_ms else None,
            "paddle_median_ms": statistics.median(vl_ms) if vl_ms else None,
            "tesseract_p95_ms": percentile(tess_ms, 0.95),
            "paddle_p95_ms": percentile(vl_ms, 0.95),
        },
        "by_tesseract_cohort": by_cohort,
    }


def _fmt_pct(rate: Optional[float]) -> str:
    if rate is None:
        return "n/a"
    return f"{rate * 100:.1f}%"


def write_reports(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = aggregate(rows)
    disagreements: List[Dict[str, Any]] = []
    for row in rows:
        disagreements.extend(row["disagreements"])
    category_counts: Dict[str, int] = {}
    for item in disagreements:
        category_counts[item["category"]] = category_counts.get(item["category"], 0) + 1
    summary["disagreement_rows"] = len(disagreements)
    summary["disagreement_categories"] = category_counts

    image_csv = OUT_DIR / "za_181_image_comparison.csv"
    with image_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "document_id",
                "sha_match",
                "tess_cohort",
                "vl_cohort",
                "tess_products",
                "vl_products",
                "shared_products",
                "only_tesseract_products",
                "only_paddle_products",
                "product_agreement",
                "qty_compared",
                "qty_agree",
                "qty_agreement",
                "positional_compared",
                "positional_agree",
                "disagreement_count",
                "exact_document_agreement",
                "manual_review",
                "tess_ms",
                "vl_ms",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in writer.fieldnames})

    disagree_csv = OUT_DIR / "za_181_field_disagreements.csv"
    with disagree_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["document_id", "product", "field", "tesseract", "paddleocr_vl", "category"],
        )
        writer.writeheader()
        writer.writerows(disagreements)

    payload = {
        "comparison_type": "output_agreement_not_accuracy",
        "images": summary["images"],
        "summary": summary,
        "documents": [
            {key: value for key, value in row.items() if key != "disagreements"}
            for row in rows
        ],
    }
    json_path = OUT_DIR / "za_181_image_engine_comparison.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    runtime = summary["runtime"]
    ratio = None
    if runtime["tesseract_median_ms"] and runtime["paddle_median_ms"]:
        ratio = runtime["paddle_median_ms"] / runtime["tesseract_median_ms"]
    lines = [
        "# ZA 181-image engine comparison",
        "",
        "This report compares cached Tesseract and PaddleOCR-VL outputs. **Agreement is not accuracy.** The two source-verified ZA documents stay in `phase4/za_ground_truth/REPORT.md` and are not used as the denominator here.",
        "",
        "## Executive summary",
        "",
        f"- Images compared: **{summary['images']}** (unique {summary['unique_images']}).",
        f"- Product-name agreement: **{_fmt_pct(summary['product_name_agreement'])}** ({summary['shared_products']}/{summary['product_union']} shared names over the union).",
        f"- Named quantity-field agreement, one or both values present: **{_fmt_pct(summary['quantity_agreement'])}** ({summary['qty_agree']}/{summary['qty_compared']}) across {summary['named_qty_images']}/{summary['images']} images. Both-missing fields are not in this denominator.",
        f"- Headerless positional number agreement, kept separate from named fields: **{_fmt_pct(summary['positional_agreement'])}** ({summary['positional_agree']}/{summary['positional_compared']}) on {summary['positional_images']}/{summary['images']} images.",
        f"- Exact document agreement: **{_fmt_pct(summary['exact_document_agreement'])}** ({summary['exact_documents']}/{summary['images']}).",
        f"- Paddle extracted more product rows on **{summary['paddle_more_products_images']}** images; Tesseract on **{summary['tesseract_more_products_images']}**; equal counts on **{summary['equal_product_count_images']}**.",
        f"- Manual review flagged on **{summary['manual_review_images']}** images.",
        "",
        "## Coverage",
        "",
        "- Shared IDs are VL checkpoint status `complete` with both primary OCR caches present.",
        f"- Cache SHA-256 matches: {summary['sha_matches']}. Mismatches: {summary['sha_mismatches']}. Missing hash: {summary['sha_unknown']}.",
        "- Structured rows come from Paddle HTML tables when present, otherwise from plain OCR lines.",
        "- Named quantity columns are assigned only from header words printed by that engine. Pack sizes and amount columns are not treated as stock quantities.",
        f"- Paddle tables where a packing token sat in the first quantity column, and that token was removed before reading the printed headers: {summary['vl_pack_column_shifted_images']}.",
        "- Pages with no printed quantity headers are not given opening/receipt/sales/closing labels. Their trailing numbers are compared only as positional tokens.",
        "- Production `line_items` arrays are not stored in the batch artifacts, so they are not the comparison source.",
        "",
        "## Aggregate metrics",
        "",
        f"| Metric | Value | Denominator |",
        f"|---|---:|---:|",
        f"| Tesseract products | {summary['tesseract_products']} | {summary['images']} images |",
        f"| Paddle products | {summary['paddle_products']} | {summary['images']} images |",
        f"| Mean Tesseract products / image | {summary['avg_tesseract_products']:.2f} | {summary['images']} |",
        f"| Mean Paddle products / image | {summary['avg_paddle_products']:.2f} | {summary['images']} |",
        f"| Names only in Tesseract | {summary['only_tesseract_products']} | {summary['product_union']} union |",
        f"| Names only in Paddle | {summary['only_paddle_products']} | {summary['product_union']} union |",
        f"| Tesseract duplicate names dropped | {summary['tesseract_duplicate_products']} | extra copies after the first |",
        f"| Paddle duplicate names dropped | {summary['paddle_duplicate_products']} | extra copies after the first |",
        f"| Shared-name order agrees | {summary['order_agrees']} | {summary['order_compared']} images with a shared name |",
        "",
        "### Quantity fields",
        "",
        "Both-present agreement ignores rows where neither engine has the field. Missing-on-one is a disagreement in the any-present rate, not an agreement.",
        f"Both-present named quantity agreement: **{_fmt_pct(summary['both_present_quantity_agreement'])}** ({summary['both_present_qty_agree']}/{summary['both_present_qty']}).",
        "",
        "| Field | Agree | Disagree | Missing Tesseract | Missing Paddle | Both-present agreement |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for field, counts in summary["field_rates"].items():
        lines.append(
            f"| {field} | {counts['agree']} | {counts['disagree']} | {counts['missing_tesseract']} | {counts['missing_paddle']} | {_fmt_pct(counts['both_present_agreement'])} ({counts['agree']}/{counts['both_present']}) |"
        )
    lines.extend(
        [
            "",
            "## Layout metadata",
            "",
            "No separate image-quality label exists. The table below uses the Tesseract run's existing text cohort. It is not a visual quality grade.",
            "",
            "| Tesseract cohort | Images | Product agreement | Quantity agreement |",
            "|---|---:|---:|---:|",
        ]
    )
    for cohort, bucket in sorted(summary["by_tesseract_cohort"].items()):
        prod = (bucket["shared_products"] / bucket["product_union"]) if bucket["product_union"] else None
        qty = (bucket["qty_agree"] / bucket["qty_compared"]) if bucket["qty_compared"] else None
        lines.append(
            f"| {cohort} | {bucket['images']} | {_fmt_pct(prod)} ({bucket['shared_products']}/{bucket['product_union']}) | {_fmt_pct(qty)} ({bucket['qty_agree']}/{bucket['qty_compared']}) |"
        )
    lines.extend(
        [
            "",
            "## Runtime",
            "",
            "Timings are the stored `primary_ms` values from each run. They were not measured in one sitting, so the ratio is operational, not a lab benchmark.",
            "",
            f"| | Tesseract | PaddleOCR-VL |",
            f"|---|---:|---:|",
            f"| Images with timing | {runtime['tesseract_images_with_timing']} | {runtime['paddle_images_with_timing']} |",
            f"| Total | {runtime['tesseract_total_ms'] / 1000:.1f} s | {runtime['paddle_total_ms'] / 1000:.1f} s |",
            f"| Mean | {runtime['tesseract_mean_ms'] / 1000:.1f} s | {runtime['paddle_mean_ms'] / 1000:.1f} s |",
            f"| Median | {runtime['tesseract_median_ms'] / 1000:.1f} s | {runtime['paddle_median_ms'] / 1000:.1f} s |",
            f"| P95 | {runtime['tesseract_p95_ms'] / 1000:.1f} s | {runtime['paddle_p95_ms'] / 1000:.1f} s |",
            "",
            f"Median Paddle / Tesseract ratio: **{ratio:.1f}x**." if ratio else "Median ratio unavailable.",
            "",
            "## Disagreements",
            "",
            f"Disagreement rows written: **{summary['disagreement_rows']}**.",
            "",
        ]
    )
    for category, count in sorted(category_counts.items(), key=lambda item: -item[1]):
        lines.append(f"- {category}: {count}")
    lines.extend(
        [
            "",
            "## Source-verified accuracy, kept separate",
            "",
            "Only 2 ZA images have source-verified labels. Those results are in `phase4/za_ground_truth/REPORT.md`. They are not extrapolated to these 181 images.",
            "",
            "## Limitations",
            "",
            "- A shared product name does not mean either name matches the printed page.",
            "- Plain-text quantity positions are named only when the OCR text prints the stock headers. Other plain lines contribute product names and, when neither engine has a named field, positional numbers.",
            "- A packing token in Paddle's first quantity column is removed only when most rows show that pattern. The stock identity equation is not used.",
            "- HTML column repair belongs to the existing table parser. A shifted cell can still be compared.",
            "- Duplicate normalized product names keep the first row only. A second pack of the same name is not a separate product in this comparison.",
            "",
            "## Conclusion",
            "",
            "The 181-image result is an output-agreement comparison. It does not name an accuracy winner. PaddleOCR-VL emits more product rows. Tesseract is much faster. Where both engines expose the same quantity, use the both-present rates above; do not read those rates as correctness.",
            "",
        ]
    )
    md_path = OUT_DIR / "za_181_image_engine_comparison.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return summary


def main() -> int:
    doc_ids = shared_document_ids()
    rows = [_load_pair(doc_id) for doc_id in doc_ids]
    summary = write_reports(rows)
    print(
        json.dumps(
            {
                "images": summary["images"],
                "product_name_agreement": summary["product_name_agreement"],
                "quantity_agreement": summary["quantity_agreement"],
                "exact_document_agreement": summary["exact_document_agreement"],
                "paddle_more_products_images": summary["paddle_more_products_images"],
                "tesseract_more_products_images": summary["tesseract_more_products_images"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
