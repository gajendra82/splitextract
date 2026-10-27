"""Strict field matching between source GT and OCR evidence."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.ground_truth.html_table import (
    parse_html_tables,
    packing_hint_from_field_id,
    row_lookup,
    _header_aliases,
)

NUMERIC_RE = re.compile(
    r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^-?\d+(?:\.\d+)?$"
)


def normalize_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("&#x27;", "'").replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def normalize_numeric(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    text = text.replace(",", "")
    text = text.replace("&#x27;", "'")
    # Strip currency / trailing junk but keep leading minus and decimal.
    text = re.sub(r"[^\d.\-]", "", text)
    if text in {"", "-", ".", "-."}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def detect_numeric_concatenation(expected_raw: str, predicted_raw: str) -> bool:
    """True when predicted glues expected with another digit run."""
    exp = re.sub(r"[^\d]", "", str(expected_raw or ""))
    pred = re.sub(r"[^\d]", "", str(predicted_raw or ""))
    if not exp or not pred:
        return False
    if exp == pred:
        return False
    return exp in pred and len(pred) > len(exp)


def detect_numeric_splitting(expected_raw: str, predicted_tokens: Sequence[str]) -> bool:
    """True when expected digits appear split across predicted tokens."""
    exp = re.sub(r"[^\d]", "", str(expected_raw or ""))
    if len(exp) < 2:
        return False
    tokens = [re.sub(r"[^\d]", "", str(t or "")) for t in predicted_tokens]
    tokens = [t for t in tokens if t]
    if exp in tokens:
        return False
    joined = "".join(tokens)
    if exp not in joined:
        return False
    # Check adjacent token pairs that reconstruct expected.
    for i in range(len(tokens) - 1):
        if tokens[i] + tokens[i + 1] == exp:
            return True
    return False


def exact_numeric_match(expected_raw: str, predicted_raw: str) -> bool:
    if str(expected_raw).strip() == str(predicted_raw).strip():
        return True
    left = normalize_numeric(expected_raw)
    right = normalize_numeric(predicted_raw)
    if left is None or right is None:
        return False
    return abs(left - right) < 1e-9


def match_field(
    gt_field: Dict[str, Any],
    *,
    ocr_text: str,
    table_cells: Optional[List[Dict[str, Any]]] = None,
    numeric_tokens: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Compare one source-verified field to OCR evidence.

    Numeric matching is strict: presence of digits elsewhere is not enough.
    Column-aware matching prefers HTML table cells when available.
    """
    expected_raw = str(gt_field.get("expected_raw") or "")
    printed_header = gt_field.get("printed_header")
    product_key = gt_field.get("product_key")
    field_type = str(gt_field.get("field_type") or "other")
    numeric_like = field_type in {
        "numeric",
        "quantity",
        "price",
        "percentage",
        "hsn_gst",
        "date",
        "batch",
        "table_cell",
    } or bool(NUMERIC_RE.match(expected_raw.replace(",", "")))

    predicted_raw = None
    match_mode = None
    cells = table_cells or parse_html_tables(ocr_text)

    if product_key and printed_header:
        pack_hint = packing_hint_from_field_id(gt_field.get("field_id"))
        hit = row_lookup(
            cells,
            product_key=product_key,
            header=printed_header,
            packing_hint=pack_hint,
        )
        if hit is not None:
            raw_hit = hit.get("value")
            # Blank / Nil table cells are missing evidence, not a wrong quantity.
            token = str(raw_hit).strip() if raw_hit is not None else ""
            if token == "" or token.casefold() in {
                "-",
                "—",
                "--",
                "–",
                "-nil-",
                "nil",
                "n/a",
                "na",
            }:
                predicted_raw = None
                match_mode = None
            else:
                predicted_raw = raw_hit
                match_mode = "column_aware_table"

    if predicted_raw is None and printed_header and field_type == "header":
        # Header presence: exact text, alias overlap, or punctuation-insensitive.
        # Also accept OCR headers whose alphabetic tokens overlap aliases
        # (e.g. printed "Sales Qty" vs VL "Sales\\n0tv").
        if normalize_text(printed_header) in normalize_text(ocr_text):
            predicted_raw = printed_header
            match_mode = "header_presence"
        else:
            target_aliases = set(_header_aliases(printed_header))
            compact_target = re.sub(
                r"[^a-z0-9]", "", normalize_text(expected_raw or printed_header)
            )
            target_tokens = set(
                re.findall(r"[a-z]{3,}", normalize_text(printed_header))
            ) | set(re.findall(r"[a-z]{3,}", normalize_text(expected_raw or "")))
            for cell in cells:
                if not cell.get("is_header"):
                    continue
                value = str(cell.get("value") or "")
                # Normalize VL newlines inside header cells.
                value_norm = normalize_text(value.replace("\\n", " ").replace("\n", " "))
                if value_norm == normalize_text(expected_raw or printed_header):
                    predicted_raw = value
                    match_mode = "header_cell"
                    break
                if target_aliases & set(_header_aliases(value)):
                    predicted_raw = value
                    match_mode = "header_alias"
                    break
                compact_value = re.sub(r"[^a-z0-9]", "", value_norm)
                if compact_target and compact_target == compact_value:
                    predicted_raw = value
                    match_mode = "header_compact"
                    break
                value_tokens = set(re.findall(r"[a-z]{3,}", value_norm))
                # Require a distinctive token — ignore shared nouns like
                # stock/qty that would equate Op. Stock with Cl. Stock.
                generic = {
                    "qty",
                    "quantity",
                    "stock",
                    "stocks",
                    "name",
                    "product",
                    "item",
                    "pack",
                    "packing",
                    "value",
                    "amount",
                    "total",
                    "free",
                    "from",
                    "the",
                    "and",
                }
                distinctive = target_tokens - generic
                if distinctive and (distinctive & value_tokens):
                    predicted_raw = value
                    match_mode = "header_token"
                    break
            if predicted_raw is None and compact_target:
                compact_ocr = re.sub(r"[^a-z0-9]", "", normalize_text(ocr_text))
                if compact_target in compact_ocr:
                    predicted_raw = printed_header
                    match_mode = "header_presence_compact"

    if predicted_raw is None and not numeric_like:
        # Text: require exact normalized token/phrase presence as own unit.
        target = normalize_text(expected_raw)
        hay = normalize_text(ocr_text)
        if target and target in hay:
            predicted_raw = expected_raw
            match_mode = "text_contains"

    if predicted_raw is None and numeric_like and match_mode is None:
        # Fall back: only accept an exact standalone numeric token match.
        tokens = list(numeric_tokens or [])
        if not tokens:
            tokens = re.findall(
                r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?",
                ocr_text or "",
            )
        for token in tokens:
            if exact_numeric_match(expected_raw, token):
                predicted_raw = token
                match_mode = "standalone_numeric_token"
                break

    correct = False
    error_type = None
    if predicted_raw is None:
        error_type = "MISSING"
    elif numeric_like:
        if exact_numeric_match(expected_raw, str(predicted_raw)):
            correct = True
        else:
            if detect_numeric_concatenation(expected_raw, str(predicted_raw)):
                error_type = "NUMERIC_CONCATENATION"
            elif detect_numeric_splitting(
                expected_raw, re.findall(r"\d+(?:\.\d+)?", str(predicted_raw))
            ):
                error_type = "NUMERIC_SPLITTING"
            elif match_mode == "column_aware_table":
                # Header already resolved; wrong digits are value errors, not
                # column shifts. Reserve COLUMN_CONFUSION for shift evidence.
                error_type = "WRONG_VALUE"
            else:
                error_type = "WRONG_VALUE"
    else:
        correct = normalize_text(predicted_raw) == normalize_text(expected_raw) or (
            normalize_text(expected_raw) in normalize_text(predicted_raw)
        )
        if not correct and field_type == "header":
            # Punctuation/spacing variants of the same printed header.
            left = re.sub(r"[^a-z0-9]", "", normalize_text(expected_raw))
            right = re.sub(r"[^a-z0-9]", "", normalize_text(predicted_raw))
            if left and left == right:
                correct = True
            elif set(_header_aliases(expected_raw or printed_header or "")) & set(
                _header_aliases(str(predicted_raw or ""))
            ):
                correct = True
        if not correct:
            error_type = "WRONG_TEXT"

    silent_wrong = bool(
        predicted_raw is not None and not correct and error_type != "MISSING"
    )
    critical = bool(
        error_type
        in {
            "WRONG_VALUE",
            "NUMERIC_CONCATENATION",
            "NUMERIC_SPLITTING",
            "COLUMN_CONFUSION",
            "MISSING",
        }
        and field_type
        in {
            "numeric",
            "quantity",
            "price",
            "date",
            "batch",
            "percentage",
            "hsn_gst",
            "table_cell",
        }
    )

    return {
        "field_id": gt_field.get("field_id"),
        "field_type": field_type,
        "printed_header": printed_header,
        "product_key": product_key,
        "expected_raw": expected_raw,
        "predicted_raw": predicted_raw,
        "match_mode": match_mode,
        "correct": correct,
        "error_type": error_type,
        "silent_wrong": silent_wrong,
        "critical": critical,
        "priority": gt_field.get("priority") or "high",
    }
