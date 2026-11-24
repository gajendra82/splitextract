"""Source-ground-truth workflow for the 181 ZA images.

Verified labels are copied from the existing two-document set.
Images without a source reading stay not_annotated. OCR text is never
written in as a label, and blank cells are never stored as zero.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.compare_za_181_engines import (  # noqa: E402
    QTY_FIELDS,
    TESS_RUN,
    VL_RUN,
    extract_rows,
    normalize_name,
    numbers_equal,
    parse_number,
    shared_document_ids,
)
from scripts.evaluate_za_ocr_accuracy import _qty_hit, _score_doc, _summarize, _tokens, _window  # noqa: E402
from services.ground_truth.matching import normalize_text  # noqa: E402

OUT_DIR = ROOT / "phase4" / "za_181_accuracy"
EXISTING_GT = ROOT / "phase4" / "za_ground_truth" / "per_document"
IMAGE_ROOT = Path(r"C:\Users\amnsa\Downloads\image dataset stock")

QTY_LABELS = {
    "opening_qty": "opening",
    "receipt_qty": "receipt",
    "sales_qty": "sales",
    "sales_scheme_qty": "sales_scheme",
    "stock_out_qty": "stock_out",
    "closing_qty": "closing",
}
UNSCORED_STATUSES = {"uncertain", "unreadable", "absent", "not_annotated", "blank"}
KNOWN_SEMANTICS = set(QTY_FIELDS)


def _field(raw: Any, normalized: Any, status: str) -> Dict[str, Any]:
    return {"raw": raw, "normalized": normalized, "status": status}


def _absent() -> Dict[str, Any]:
    return _field(None, None, "absent")


def rows_from_verified_document(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Convert an existing source-verified document. Does not invent cells."""
    grouped: Dict[str, Dict[str, Any]] = {}
    headers = []
    for field in payload.get("fields") or []:
        field_id = str(field.get("field_id") or "")
        if not field.get("source_verified") or not field.get("verified"):
            continue
        if field_id.startswith("header."):
            headers.append(
                {
                    "field_id": field_id,
                    "raw": field.get("expected_raw"),
                    "normalized": field.get("expected_normalized"),
                    "status": "verified",
                }
            )
            continue
        if "." not in field_id:
            continue
        row_id, name = field_id.split(".", 1)
        row = grouped.setdefault(
            row_id,
            {
                "row_id": row_id,
                "row_index": int(row_id[1:]) if row_id[1:].isdigit() else len(grouped) + 1,
                "product_name": _absent(),
                "packing": _absent(),
                **{qty: _absent() for qty in QTY_FIELDS},
            },
        )
        status = "verified"
        cell = _field(field.get("expected_raw"), field.get("expected_normalized"), status)
        if name == "product_name":
            row["product_name"] = cell
        elif name == "packing":
            row["packing"] = cell
        elif name == "out_qty":
            row["sales_qty"] = cell
        elif name in QTY_FIELDS:
            row[name] = cell
    rows = [grouped[key] for key in sorted(grouped, key=lambda item: grouped[item]["row_index"])]
    return rows, headers


def empty_image(document_id: str) -> Dict[str, Any]:
    source = IMAGE_ROOT / document_id
    return {
        "document_id": document_id,
        "source_file": str(source),
        "source_file_exists": source.is_file(),
        "verification_status": "not_annotated",
        "annotation_origin": "none",
        "ai_generated": False,
        "human_verified": False,
        "rows": [],
        "headers": [],
        "notes": [
            "No source transcription is stored for this image. "
            "Tesseract and PaddleOCR-VL text were not used as labels. "
            "Blank cells were not filled with zero."
        ],
    }


def verified_image(payload: Dict[str, Any]) -> Dict[str, Any]:
    rows, headers = rows_from_verified_document(payload)
    source = (payload.get("source") or {}).get("file") or str(IMAGE_ROOT / payload["document_id"])
    return {
        "document_id": payload["document_id"],
        "source_file": source,
        "source_file_exists": Path(source).is_file(),
        "verification_status": "verified",
        "annotation_origin": "existing_source_verified",
        "ai_generated": False,
        "human_verified": True,
        "verified_by": "existing_phase4_za_ground_truth",
        "existing_provenance": payload.get("provenance"),
        "existing_verification_method": payload.get("verification_method"),
        "rows": rows,
        "headers": headers,
        "notes": [
            "Copied from the existing source-verified ZA labels. "
            "This workflow did not edit those labels."
        ],
    }


def build_dataset(image_ids: Sequence[str]) -> Dict[str, Any]:
    by_id = {}
    for path in sorted(EXISTING_GT.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        by_id[payload["document_id"]] = verified_image(payload)
    images = []
    for document_id in image_ids:
        if document_id in by_id:
            images.append(by_id[document_id])
        else:
            images.append(empty_image(document_id))
    return {
        "dataset": "za_181_source_ground_truth",
        "rule": (
            "Only human source-verified rows are authoritative. "
            "AI proposals must stay unverified. OCR output is not ground truth."
        ),
        "images": images,
    }


def _blank_row(index: int) -> Dict[str, Any]:
    return {
        "row_id": f"r{index:02d}",
        "row_index": index,
        "product_name": _absent(),
        "packing": _absent(),
        **{qty: _absent() for qty in QTY_FIELDS},
        "columns": [],
    }


def apply_vision_proposal(parsed_rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map a vision transcript into the ground-truth schema.

    A quantity field is named only when the model copied a printed header.
    Blank cells stay blank. They are not stored as zero.
    """
    rows = []
    for index, raw in enumerate(parsed_rows, start=1):
        if not isinstance(raw, dict):
            continue
        row = _blank_row(index)
        product = str(raw.get("product_name") or "").strip()
        if product:
            row["product_name"] = _field(product, product, "ai_proposed")
        else:
            row["product_name"] = _field(None, None, "unreadable")
        packing = str(raw.get("packing") or "").strip()
        if packing:
            row["packing"] = _field(packing, packing, "ai_proposed")
        used: Dict[str, int] = {}
        columns = []
        for column in raw.get("columns") or []:
            if not isinstance(column, dict):
                continue
            header = str(column.get("printed_header") or "").strip()
            semantic = str(column.get("semantic") or "unknown")
            if semantic not in KNOWN_SEMANTICS:
                semantic = "unknown"
            if not header or re.search(r"value|amount|amt|rate|price", header, re.I):
                semantic = "unknown" if not header else "not_quantity"
            status = str(column.get("status") or "ai_proposed")
            if status not in {"ai_proposed", "uncertain", "unreadable", "blank"}:
                status = "uncertain"
            raw_value = column.get("raw")
            if raw_value is None or str(raw_value).strip() == "":
                normalized = None
                status = "blank" if status == "ai_proposed" else status
            else:
                normalized = parse_number(raw_value)
                if normalized is None and status == "ai_proposed":
                    status = "uncertain"
            position = column.get("position") or (len(columns) + 1)
            entry = {
                "position": position,
                "printed_header": header or None,
                "semantic": semantic,
                "raw": None if raw_value is None else str(raw_value),
                "normalized": normalized,
                "status": status,
            }
            columns.append(entry)
            if semantic in KNOWN_SEMANTICS and status == "ai_proposed" and normalized is not None:
                if semantic in used:
                    row[semantic] = _field(row[semantic].get("raw"), None, "uncertain")
                    columns[used[semantic]]["status"] = "uncertain"
                    columns[used[semantic]]["semantic"] = "unknown"
                    entry["status"] = "uncertain"
                    entry["semantic"] = "unknown"
                else:
                    used[semantic] = len(columns) - 1
                    row[semantic] = _field(str(raw_value), normalized, "ai_proposed")
        row["columns"] = columns
        rows.append(row)
    return rows


def progress_from(dataset: Dict[str, Any]) -> Dict[str, Any]:
    images = dataset["images"]
    buckets: Dict[str, List[str]] = {}
    for image in images:
        buckets.setdefault(image["verification_status"], []).append(image["document_id"])
    uncertain_fields = 0
    verified_rows = 0
    for image in images:
        if image["verification_status"] == "verified":
            verified_rows += len(image["rows"])
        for row in image["rows"]:
            for key, cell in row.items():
                if isinstance(cell, dict) and cell.get("status") in {"uncertain", "unreadable"}:
                    uncertain_fields += 1
            for column in row.get("columns") or []:
                if isinstance(column, dict) and column.get("status") in {"uncertain", "unreadable"}:
                    uncertain_fields += 1
    proposed = [
        image["document_id"]
        for image in images
        if image.get("annotation_origin") == "AI_PROPOSED" and image.get("rows")
    ]
    failed = [
        image["document_id"]
        for image in images
        if image.get("verification_status") == "proposal_failed"
    ]
    return {
        "images_total": len(images),
        "unique_images": len({image["document_id"] for image in images}),
        "verified_images": len(buckets.get("verified", [])),
        "unverified_images": len(buckets.get("unverified", [])),
        "not_annotated_images": len(buckets.get("not_annotated", [])),
        "ai_proposed_images": len(proposed),
        "proposal_failed_images": len(failed),
        "verified_rows": verified_rows,
        "uncertain_or_unreadable_fields": uncertain_fields,
        "ai_proposed_document_ids": proposed,
        "proposal_failed_document_ids": failed,
        "verified_document_ids": buckets.get("verified", []),
        "unverified_document_ids": buckets.get("unverified", []),
        "not_annotated_document_ids": buckets.get("not_annotated", []),
        "source_files_missing": [
            image["document_id"] for image in images if not image.get("source_file_exists")
        ],
    }


def write_review_csv(dataset: Dict[str, Any], path: Path) -> None:
    fields = [
        "document_id",
        "row_id",
        "field",
        "column_position",
        "printed_header",
        "semantic",
        "proposed_value",
        "normalized_value",
        "confidence",
        "verification_status",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for image in dataset["images"]:
            if not image["rows"]:
                writer.writerow(
                    {
                        "document_id": image["document_id"],
                        "row_id": "",
                        "field": "",
                        "column_position": "",
                        "printed_header": "",
                        "semantic": "",
                        "proposed_value": "",
                        "normalized_value": "",
                        "confidence": image.get("failure") or "",
                        "verification_status": image["verification_status"],
                    }
                )
                continue
            for row in image["rows"]:
                for name, cell in row.items():
                    if name in {"row_id", "row_index"} or not isinstance(cell, dict):
                        continue
                    writer.writerow(
                        {
                            "document_id": image["document_id"],
                            "row_id": row["row_id"],
                            "field": name,
                            "column_position": "",
                            "printed_header": "",
                            "semantic": name if name in QTY_FIELDS else "",
                            "proposed_value": "" if cell.get("raw") is None else cell.get("raw"),
                            "normalized_value": "" if cell.get("normalized") is None else cell.get("normalized"),
                            "confidence": "source_verified_existing" if cell.get("status") == "verified" else cell.get("status"),
                            "verification_status": cell.get("status"),
                        }
                    )
                for column in row.get("columns") or []:
                    writer.writerow(
                        {
                            "document_id": image["document_id"],
                            "row_id": row["row_id"],
                            "field": "column",
                            "column_position": column.get("position"),
                            "printed_header": column.get("printed_header") or "",
                            "semantic": column.get("semantic") or "",
                            "proposed_value": "" if column.get("raw") is None else column.get("raw"),
                            "normalized_value": "" if column.get("normalized") is None else column.get("normalized"),
                            "confidence": column.get("status"),
                            "verification_status": image["verification_status"],
                        }
                    )


def match_rows(
    gt_rows: Sequence[Dict[str, Any]], pred_rows: Sequence[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Match repeated product names in order. Do not collapse duplicates."""
    used = [False] * len(pred_rows)
    pairs = []
    for gt in gt_rows:
        product = gt.get("product_name") or {}
        if product.get("status") in UNSCORED_STATUSES:
            pairs.append({"gt": gt, "pred": None, "name_result": "unscored"})
            continue
        key = normalize_name(product.get("raw"))
        hit = None
        for index, pred in enumerate(pred_rows):
            if used[index]:
                continue
            if key and normalize_name(pred.get("product")) == key:
                hit = index
                break
        if hit is None:
            pairs.append({"gt": gt, "pred": None, "name_result": "missing"})
        else:
            used[hit] = True
            pairs.append({"gt": gt, "pred": pred_rows[hit], "name_result": "correct"})
    extras = [pred_rows[index] for index, flag in enumerate(used) if not flag and normalize_name(pred_rows[index].get("product"))]
    return pairs, extras


def score_quantity_value(cell: Dict[str, Any], predicted_value: Optional[float], present: bool) -> str:
    if cell.get("status") in UNSCORED_STATUSES or cell.get("status") != "verified":
        return "unscored"
    if cell.get("normalized") is None or isinstance(cell.get("normalized"), bool):
        return "unscored"
    if not present:
        return "missing"
    if numbers_equal(float(predicted_value), float(cell["normalized"])):
        return "correct"
    return "incorrect"


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return numerator / denominator


def _f1(precision: Optional[float], recall: Optional[float]) -> Optional[float]:
    if precision is None or recall is None or precision + recall == 0:
        return None
    return 2 * precision * recall / (precision + recall)


def _consume_name(haystack: str, name: str) -> Optional[str]:
    needle = normalize_text(name)
    if not needle:
        return None
    index = haystack.find(needle)
    if index < 0:
        return None
    return haystack[:index] + ("#" * len(needle)) + haystack[index + len(needle) :]


def score_document(
    gt_image: Dict[str, Any], pred_rows: Sequence[Dict[str, Any]], ocr_text: str
) -> Dict[str, Any]:
    """Score the OCR text. A full printed name must occur in that text.

    A shorter extracted fragment of a labeled name is not an extra product and
    is not a correct name. Duplicate labels consume one occurrence each.
    """
    haystack = normalize_text(ocr_text)
    gt_names = []
    name_correct = 0
    name_missing = 0
    name_unscored = 0
    for row in gt_image.get("rows") or []:
        product = row.get("product_name") or {}
        if product.get("status") in UNSCORED_STATUSES:
            name_unscored += 1
            continue
        updated = _consume_name(haystack, str(product.get("raw") or ""))
        if updated is None:
            name_missing += 1
        else:
            name_correct += 1
            haystack = updated
            gt_names.append(normalize_text(product.get("raw")))
    name_extra = 0
    for pred in pred_rows:
        pred_name = normalize_text(pred.get("product"))
        if not pred_name:
            continue
        if any(name and (name in pred_name or pred_name in name) for name in gt_names):
            continue
        name_extra += 1
    precision = _rate(name_correct, name_correct + name_extra)
    recall = _rate(name_correct, name_correct + name_missing)
    qty_counts = {
        field: {"correct": 0, "incorrect": 0, "missing": 0, "unscored": 0, "abs_error_sum": 0.0, "both_present": 0}
        for field in QTY_FIELDS
    }
    field_rows = []
    gt_rows = list(gt_image.get("rows") or [])
    names = [str((row.get("product_name") or {}).get("raw") or "") for row in gt_rows]
    for index, gt in enumerate(gt_rows):
        product = gt.get("product_name") or {}
        if product.get("status") in UNSCORED_STATUSES:
            continue
        window = _window(ocr_text, names[index], names[index + 1 :])
        for field in QTY_FIELDS:
            cell = gt.get(field) or _absent()
            if cell.get("status") in UNSCORED_STATUSES or cell.get("normalized") is None or isinstance(cell.get("normalized"), bool):
                qty_counts[field]["unscored"] += 1
                continue
            expected = float(cell["normalized"])
            predicted = None
            if window is None:
                result = "missing"
            else:
                numbers = []
                for token in _tokens(window):
                    try:
                        numbers.append(float(token))
                    except ValueError:
                        continue
                if _qty_hit(window, expected):
                    result = "correct"
                    predicted = expected
                elif numbers:
                    result = "incorrect"
                    predicted = min(numbers, key=lambda number: abs(number - expected))
                else:
                    result = "missing"
            qty_counts[field][result] += 1
            if result in {"correct", "incorrect"}:
                qty_counts[field]["both_present"] += 1
                qty_counts[field]["abs_error_sum"] += abs(float(predicted) - expected)
            if result != "unscored":
                field_rows.append(
                    {
                        "row_id": gt["row_id"],
                        "field": field,
                        "result": result,
                        "expected": expected,
                        "predicted": predicted,
                    }
                )
    qty_correct = sum(item["correct"] for item in qty_counts.values())
    qty_incorrect = sum(item["incorrect"] for item in qty_counts.values())
    qty_missing = sum(item["missing"] for item in qty_counts.values())
    qty_scored = qty_correct + qty_incorrect + qty_missing
    both_present = qty_correct + qty_incorrect
    abs_error = sum(item["abs_error_sum"] for item in qty_counts.values())
    scored_name_rows = name_correct + name_missing
    exact = (
        scored_name_rows > 0
        and name_missing == 0
        and name_extra == 0
        and qty_scored > 0
        and qty_incorrect == 0
        and qty_missing == 0
    )
    return {
        "document_id": gt_image["document_id"],
        "verification_status": gt_image["verification_status"],
        "gt_rows": len(gt_image.get("rows") or []),
        "predicted_rows": len(pred_rows),
        "product_correct": name_correct,
        "product_incorrect": 0,
        "product_missing": name_missing,
        "product_extra": name_extra,
        "product_unscored": name_unscored,
        "product_precision": precision,
        "product_recall": recall,
        "product_f1": _f1(precision, recall),
        "quantity_fields": qty_counts,
        "quantity_correct": qty_correct,
        "quantity_incorrect": qty_incorrect,
        "quantity_missing": qty_missing,
        "quantity_scored": qty_scored,
        "quantity_accuracy": _rate(qty_correct, qty_scored),
        "quantity_both_present": both_present,
        "quantity_both_present_accuracy": _rate(qty_correct, both_present),
        "quantity_mae": (abs_error / both_present) if both_present else None,
        "silent_wrong": qty_incorrect,
        "silent_wrong_rate": _rate(qty_incorrect, both_present),
        "image_exact_match": exact,
        "fields": field_rows,
        "name_note": (
            "A product is correct when the full printed name occurs in the OCR text. "
            "One occurrence is consumed per ground-truth row. "
            "An extracted fragment of a labeled name is not an extra product."
        ),
    }


def _load_predictions(run: Path, document_id: str) -> Tuple[str, List[Dict[str, Any]], str]:
    stem = Path(document_id).stem
    cache = json.loads((run / "primary_ocr_cache" / f"{stem}.json").read_text(encoding="utf-8"))
    engine = str(cache.get("engine") or "")
    text = cache.get("text") or ""
    rows, _shifted = extract_rows(text)
    return engine, rows, text


def _legacy_window(document_id: str) -> Dict[str, Any]:
    payload = json.loads((EXISTING_GT / f"{document_id}.json").read_text(encoding="utf-8"))
    out = {}
    for name, run in (("tesseract", TESS_RUN), ("paddleocr-vl", VL_RUN)):
        cache = json.loads((run / "primary_ocr_cache" / f"{Path(document_id).stem}.json").read_text(encoding="utf-8"))
        out[name] = _summarize(_score_doc(payload, cache.get("text") or ""))
    return out


def evaluate_split(dataset: Dict[str, Any], split: str) -> Dict[str, Any]:
    if split == "verified":
        images = [image for image in dataset["images"] if image["verification_status"] == "verified" and image["rows"]]
    elif split == "preliminary":
        images = [
            image
            for image in dataset["images"]
            if image.get("annotation_origin") == "AI_PROPOSED"
            and image.get("ai_generated")
            and not image.get("human_verified")
            and image.get("rows")
        ]
    else:
        raise ValueError(split)
    engines = {"tesseract": TESS_RUN, "paddleocr-vl": VL_RUN}
    per_engine: Dict[str, Any] = {}
    for engine_name, run in engines.items():
        documents = []
        for image in images:
            cache_engine, rows, text = _load_predictions(run, image["document_id"])
            if engine_name == "tesseract" and cache_engine != "tesseract":
                raise RuntimeError(f"{image['document_id']} tesseract cache engine {cache_engine}")
            if engine_name == "paddleocr-vl" and "paddleocr-vl" not in cache_engine:
                raise RuntimeError(f"{image['document_id']} VL cache engine {cache_engine}")
            scored = score_document(image, rows, text)
            scored["cache_engine"] = cache_engine
            documents.append(scored)
        per_engine[engine_name] = _aggregate_engine(documents)
    legacy = {}
    if split == "verified":
        for image in images:
            legacy[image["document_id"]] = _legacy_window(image["document_id"])
    return {
        "split": split,
        "authoritative": split == "verified",
        "images_evaluated": len(images),
        "document_ids": [image["document_id"] for image in images],
        "engines": per_engine,
        "legacy_window_score_on_existing_files": legacy,
        "notes": [
            "Uncertain, unreadable, absent, and blank cells are not scored.",
            "A missing OCR quantity is not treated as zero.",
            "Duplicate ground-truth rows are matched separately, in order.",
            "Preliminary AI annotations are not included in the verified split.",
        ],
    }


def _aggregate_engine(documents: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    correct = sum(doc["product_correct"] for doc in documents)
    missing = sum(doc["product_missing"] for doc in documents)
    extra = sum(doc["product_extra"] for doc in documents)
    precision = _rate(correct, correct + extra)
    recall = _rate(correct, correct + missing)
    qty_correct = sum(doc["quantity_correct"] for doc in documents)
    qty_incorrect = sum(doc["quantity_incorrect"] for doc in documents)
    qty_missing = sum(doc["quantity_missing"] for doc in documents)
    qty_scored = qty_correct + qty_incorrect + qty_missing
    both = sum(doc["quantity_both_present"] for doc in documents)
    abs_error = 0.0
    field_totals = {
        field: {"correct": 0, "incorrect": 0, "missing": 0, "unscored": 0, "abs_error_sum": 0.0, "both_present": 0}
        for field in QTY_FIELDS
    }
    for doc in documents:
        for field, counts in doc["quantity_fields"].items():
            for key, value in counts.items():
                field_totals[field][key] += value
    for counts in field_totals.values():
        abs_error += counts["abs_error_sum"]
        counts["accuracy"] = _rate(counts["correct"], counts["correct"] + counts["incorrect"] + counts["missing"])
        counts["mae"] = (counts["abs_error_sum"] / counts["both_present"]) if counts["both_present"] else None
    exact_images = sum(1 for doc in documents if doc["image_exact_match"])
    return {
        "images": len(documents),
        "verified_rows": sum(doc["gt_rows"] for doc in documents),
        "product_correct": correct,
        "product_incorrect": 0,
        "product_missing": missing,
        "product_extra": extra,
        "product_precision": precision,
        "product_recall": recall,
        "product_f1": _f1(precision, recall),
        "quantity_correct": qty_correct,
        "quantity_incorrect": qty_incorrect,
        "quantity_missing": qty_missing,
        "quantity_scored": qty_scored,
        "quantity_accuracy": _rate(qty_correct, qty_scored),
        "quantity_both_present_accuracy": _rate(qty_correct, both),
        "quantity_mae": (abs_error / both) if both else None,
        "silent_wrong": qty_incorrect,
        "silent_wrong_rate": _rate(qty_incorrect, both),
        "image_exact_match": exact_images,
        "image_exact_match_rate": _rate(exact_images, len(documents)),
        "by_field": field_totals,
        "documents": [{key: value for key, value in doc.items() if key != "fields"} for doc in documents],
        "field_rows": [row for doc in documents for row in doc["fields"]],
    }


def _fmt(rate: Optional[float]) -> str:
    if rate is None:
        return "n/a"
    return f"{rate * 100:.1f}%"


def _metric_rows(split: str, evaluation: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for engine, summary in evaluation["engines"].items():
        specs = [
            ("product_precision", summary["product_correct"], summary["product_correct"] + summary["product_extra"], summary["product_precision"]),
            ("product_recall", summary["product_correct"], summary["product_correct"] + summary["product_missing"], summary["product_recall"]),
            ("product_f1", None, None, summary["product_f1"]),
            ("quantity_accuracy", summary["quantity_correct"], summary["quantity_scored"], summary["quantity_accuracy"]),
            ("silent_wrong_rate", summary["silent_wrong"], summary["quantity_correct"] + summary["quantity_incorrect"], summary["silent_wrong_rate"]),
            ("image_exact_match", summary["image_exact_match"], summary["images"], summary["image_exact_match_rate"]),
        ]
        for metric, numerator, denominator, rate in specs:
            rows.append(
                {
                    "split": split,
                    "engine": engine,
                    "metric": metric,
                    "numerator": "" if numerator is None else numerator,
                    "denominator": "" if denominator is None else denominator,
                    "rate": "" if rate is None else f"{rate:.6f}",
                }
            )
        for field, counts in summary["by_field"].items():
            scored = counts["correct"] + counts["incorrect"] + counts["missing"]
            rows.append(
                {
                    "split": split,
                    "engine": engine,
                    "metric": f"{field}_accuracy",
                    "numerator": counts["correct"],
                    "denominator": scored,
                    "rate": "" if counts["accuracy"] is None else f"{counts['accuracy']:.6f}",
                }
            )
    return rows


def write_reports(dataset: Dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    verified = evaluate_split(dataset, "verified")
    preliminary = evaluate_split(dataset, "preliminary")
    progress = progress_from(dataset)
    (OUT_DIR / "ground_truth.json").write_text(json.dumps(dataset, indent=2), encoding="utf-8")
    (OUT_DIR / "ground_truth_progress.json").write_text(json.dumps(progress, indent=2), encoding="utf-8")
    write_review_csv(dataset, OUT_DIR / "ground_truth_review.csv")
    (OUT_DIR / "tesseract_evaluation.json").write_text(
        json.dumps(
            {
                "authoritative_split": "verified",
                "warning": "Verified metrics are the only accuracy result. Preliminary metrics are not authoritative.",
                "verified": verified["engines"]["tesseract"],
                "preliminary": preliminary["engines"]["tesseract"],
                "legacy_window_score_on_existing_files": {
                    doc: scores["tesseract"] for doc, scores in verified["legacy_window_score_on_existing_files"].items()
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "paddleocr_vl_evaluation.json").write_text(
        json.dumps(
            {
                "authoritative_split": "verified",
                "warning": "Verified metrics are the only accuracy result. Preliminary metrics are not authoritative.",
                "verified": verified["engines"]["paddleocr-vl"],
                "preliminary": preliminary["engines"]["paddleocr-vl"],
                "legacy_window_score_on_existing_files": {
                    doc: scores["paddleocr-vl"] for doc, scores in verified["legacy_window_score_on_existing_files"].items()
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    comparison_rows = _metric_rows("verified", verified) + _metric_rows("preliminary", preliminary)
    with (OUT_DIR / "accuracy_comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["split", "engine", "metric", "numerator", "denominator", "rate"])
        writer.writeheader()
        writer.writerows(comparison_rows)
    (OUT_DIR / "ACCURACY_REPORT.md").write_text(_markdown(progress, verified, preliminary), encoding="utf-8")


def _engine_block(summary: Dict[str, Any]) -> List[str]:
    lines = [
        f"- Product precision: {_fmt(summary['product_precision'])} ({summary['product_correct']}/{summary['product_correct'] + summary['product_extra']}).",
        f"- Product recall: {_fmt(summary['product_recall'])} ({summary['product_correct']}/{summary['product_correct'] + summary['product_missing']}).",
        f"- Product F1: {_fmt(summary['product_f1'])}.",
        f"- Missing product rows: {summary['product_missing']}. Extra product rows: {summary['product_extra']}.",
        f"- Quantity accuracy, missing counted as not correct: {_fmt(summary['quantity_accuracy'])} ({summary['quantity_correct']}/{summary['quantity_scored']}).",
        f"- Quantity accuracy where both sides have a number: {_fmt(summary['quantity_both_present_accuracy'])} ({summary['quantity_correct']}/{summary['quantity_correct'] + summary['quantity_incorrect']}).",
        f"- Mean absolute error on both-present quantities: {summary['quantity_mae']}.",
        f"- Silent wrong quantities: {summary['silent_wrong']} ({_fmt(summary['silent_wrong_rate'])} of both-present quantities).",
        f"- Image exact match: {summary['image_exact_match']}/{summary['images']}.",
        "",
        "| Field | Correct | Incorrect | Missing | Accuracy | MAE |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for field, counts in summary["by_field"].items():
        mae = "" if counts["mae"] is None else f"{counts['mae']:.3f}"
        lines.append(
            f"| {QTY_LABELS[field]} | {counts['correct']} | {counts['incorrect']} | {counts['missing']} | {_fmt(counts['accuracy'])} | {mae} |"
        )
    return lines


def _markdown(progress: Dict[str, Any], verified: Dict[str, Any], preliminary: Dict[str, Any]) -> str:
    tess = verified["engines"]["tesseract"]
    paddle = verified["engines"]["paddleocr-vl"]
    lines = [
        "# ZA 181 accuracy report",
        "",
        "This report keeps verified and unverified annotations apart. OCR agreement is not accuracy.",
        "",
        "## Coverage",
        "",
        f"- Images accounted for: {progress['images_total']} (unique {progress['unique_images']}).",
        f"- Manually verified images: {progress['verified_images']}.",
        f"- Verified product rows: {progress['verified_rows']}.",
        f"- Images with no source annotation yet: {progress['not_annotated_images']}.",
        f"- AI-proposed images, not verified: {progress.get('ai_proposed_images', 0)}.",
        f"- Proposal failures: {progress.get('proposal_failed_images', 0)}.",
        f"- Uncertain or unreadable fields: {progress['uncertain_or_unreadable_fields']}.",
        f"- Source files missing on disk: {len(progress['source_files_missing'])}.",
        "",
        "The verified images are the two documents already stored in `phase4/za_ground_truth/per_document/`. Their files were copied, not edited.",
        "",
        "## Verified accuracy",
        "",
        "Denominator: the two source-verified images only. This is not a 181-image accuracy rate.",
        "",
        "A product name is correct only when the full printed name occurs in that engine's OCR text. Pack size stays part of the name. Repeated names consume one occurrence each. A quantity is correct when that number occurs in the text after its product name and before the next different labeled product. A missing number is not a zero.",
        "",
        "### Tesseract",
        "",
        *_engine_block(tess),
        "",
        "### PaddleOCR-VL",
        "",
        *_engine_block(paddle),
        "",
        "## Preliminary AI-assisted accuracy",
        "",
        "These scores use AI-proposed labels only. They are not verified ground truth and they are not added to the verified scores above.",
        "",
        f"Images in this split: {preliminary['images_evaluated']}.",
        "",
    ]
    if preliminary["images_evaluated"]:
        lines.extend(
            [
                "A named quantity is scored only when the proposal copied a printed column header. Columns without a visible header stay unknown and are excluded.",
                "",
                "### Tesseract, preliminary",
                "",
                *_engine_block(preliminary["engines"]["tesseract"]),
                "",
                "### PaddleOCR-VL, preliminary",
                "",
                *_engine_block(preliminary["engines"]["paddleocr-vl"]),
                "",
            ]
        )
    else:
        lines.extend(["No AI-proposed rows are stored yet.", ""])
    exact_line = (
        "Neither engine has an exact document match on the verified images "
        + f"({tess['image_exact_match']}/2 Tesseract, {paddle['image_exact_match']}/2 PaddleOCR-VL)."
    )
    lines.extend(
        [
            "## Winner",
            "",
            "AI-proposed labels do not decide the 181-image accuracy winner. Only manually verified annotations can do that.",
            "",
            (
                f"Manually verified images: {progress['verified_images']}. "
                f"AI-proposed images still awaiting review: {progress.get('ai_proposed_images', 0)}. "
                f"Proposal failures: {progress.get('proposal_failed_images', 0)}."
            ),
            "",
            "On those 2 images, PaddleOCR-VL reads more of the printed product names and more of the labeled quantities. Tesseract has fewer silent-wrong quantities. Neither image is an exact match. Labeled zeros are included in the 162-field denominator. A quantity counts as correct when the expected number appears in the text of that product row. This score does not prove which printed column the number came from. The earlier report at phase4/za_ground_truth/REPORT.md is unchanged.",
            "",
            exact_line,
            "",
            "## Resume",
            "",
            "python scripts/za_181_accuracy_workflow.py propose",
            "python scripts/za_181_accuracy_workflow.py evaluate",
            "python scripts/za_181_accuracy_workflow.py serve --port 8765",
            "",
            "Mark a row verified in the review tool only after comparing it with the original image. Saving an edit without that confirmation keeps the image unverified.",
            "",
        ]
    )
    return "\n".join(lines)


def init_dataset() -> Dict[str, Any]:
    if (OUT_DIR / "ground_truth.json").is_file():
        raise SystemExit("ground_truth.json already exists; refusing to rebuild over proposals")
    image_ids = shared_document_ids()
    if len(image_ids) != len(set(image_ids)):
        raise RuntimeError("duplicate image ids")
    dataset = build_dataset(image_ids)
    write_reports(dataset)
    return progress_from(dataset)


def load_dataset() -> Dict[str, Any]:
    return json.loads((OUT_DIR / "ground_truth.json").read_text(encoding="utf-8"))


def save_dataset(dataset: Dict[str, Any]) -> None:
    ids = [image["document_id"] for image in dataset["images"]]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate image ids")
    write_reports(dataset)


class ReviewHandler(BaseHTTPRequestHandler):
    dataset: Dict[str, Any] = {}

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, _page().encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/progress":
            body = json.dumps(progress_from(self.dataset)).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if parsed.path == "/api/dataset":
            self._send(200, json.dumps(self.dataset).encode("utf-8"), "application/json")
            return
        if parsed.path.startswith("/image/"):
            document_id = unquote(parsed.path[len("/image/") :])
            image = next((item for item in self.dataset["images"] if item["document_id"] == document_id), None)
            if image is None or not Path(image["source_file"]).is_file():
                self._send(404, b"missing image", "text/plain")
                return
            data = Path(image["source_file"]).read_bytes()
            kind = "image/jpeg"
            if document_id.lower().endswith(".png"):
                kind = "image/png"
            self._send(200, data, kind)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/save":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or "0")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        document_id = payload.get("document_id")
        image = next((item for item in self.dataset["images"] if item["document_id"] == document_id), None)
        if image is None:
            self._send(404, b"unknown image", "text/plain")
            return
        self._send(
            403,
            b"this page no longer writes ground_truth.json; use the human review server",
            "text/plain",
        )
        return

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def _page() -> str:
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>ZA 181 ground-truth review</title>
<style>
body { font-family: sans-serif; margin: 1rem; }
#layout { display: grid; grid-template-columns: 280px 1fr; gap: 1rem; }
img { max-width: 100%; border: 1px solid #ccc; }
textarea { width: 100%; min-height: 240px; font-family: monospace; }
li { cursor: pointer; margin: 0.2rem 0; }
.verified { color: #0a7; }
.not_annotated, .unverified { color: #a50; }
</style>
</head>
<body>
<h1>ZA source review</h1>
<p>Compare each row with the original image. A save without the verification box stays unverified. OCR output is not a label.</p>
<div id="layout">
  <ol id="list"></ol>
  <div>
    <div id="meta"></div>
    <img id="page" alt="original image">
    <div id="table"></div>
    <textarea id="rows"></textarea>
    <label><input id="verified" type="checkbox"> I verified these rows against the original image</label>
    <input id="note" placeholder="What you checked" style="width:100%">
    <button id="save">Save</button>
    <pre id="status"></pre>
  </div>
</div>
<script>
let dataset = null;
let current = null;
async function load() {
  dataset = await (await fetch('/api/dataset')).json();
  const list = document.getElementById('list');
  list.innerHTML = '';
  dataset.images.forEach((image, index) => {
    const item = document.createElement('li');
    item.className = image.verification_status;
    item.textContent = image.verification_status + ' ' + image.document_id;
    item.onclick = () => select(index);
    list.appendChild(item);
  });
  if (dataset.images.length) select(0);
}
function renderTable(rows) {
  if (!rows.length) {
    document.getElementById('table').innerHTML = '<p>No proposed rows.</p>';
    return;
  }
  const body = rows.map(row => {
    const cols = (row.columns || []).map(col =>
      '<td>' + (col.position ?? '') + '</td><td>' + (col.printed_header || '') + '</td><td>' + (col.semantic || '') + '</td><td>' + (col.raw ?? '') + '</td><td>' + (col.status || '') + '</td>'
    ).join('</tr><tr><td></td><td></td>');
    return '<tr><td>' + row.row_id + '</td><td>' + ((row.product_name || {}).raw || '') + '</td>' + (cols || '<td colspan="5"></td>') + '</tr>';
  }).join('');
  document.getElementById('table').innerHTML = '<table border="1" cellpadding="4"><tr><th>Row</th><th>Product</th><th>Pos</th><th>Printed header</th><th>Semantic</th><th>Value</th><th>Status</th></tr>' + body + '</table>';
}
function select(index) {
  current = dataset.images[index];
  document.getElementById('meta').textContent = (current.annotation_origin || '') + ' | ' + current.verification_status + ' | rows ' + current.rows.length + (current.failure ? ' | ' + current.failure : '');
  document.getElementById('page').src = '/image/' + encodeURIComponent(current.document_id);
  document.getElementById('rows').value = JSON.stringify(current.rows, null, 2);
  renderTable(current.rows);
  document.getElementById('verified').checked = false;
  document.getElementById('note').value = '';
}
document.getElementById('save').onclick = async () => {
  const body = {
    document_id: current.document_id,
    rows: JSON.parse(document.getElementById('rows').value || '[]'),
    human_verified: document.getElementById('verified').checked,
    reviewer_note: document.getElementById('note').value,
    confirm_edit_existing: current.annotation_origin === 'existing_source_verified'
  };
  const response = await fetch('/api/save', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  document.getElementById('status').textContent = response.status + ' ' + await response.text();
  if (response.ok) load();
};
load();
</script>
</body>
</html>
"""


VISION_PROMPT = """Transcribe this stock-statement image into JSON.
Look only at the image. Do not calculate missing numbers. Do not treat a blank cell as zero.
Keep every visible product row, in top-to-bottom order, including repeated product names.
Keep pack size and unit text in product_name. Also put a separate packing value when a pack column is printed.
For each numeric or blank cell after the product, add a column in left-to-right order.
Copy printed_header only when that header is visibly printed above the column. If no header is visible, printed_header must be null and semantic must be "unknown".
If the printed header clearly names a stock quantity, semantic may be one of: opening_qty, receipt_qty, sales_qty, sales_scheme_qty, stock_out_qty, closing_qty.
If the header says value, amount, rate, or price, semantic must be "unknown".
If a cell is blank, raw must be null and status must be "blank".
If you cannot read a cell confidently, status must be "uncertain" or "unreadable".
Return only this JSON object and no other text:
{"rows":[{"product_name":"","packing":null,"columns":[{"position":1,"printed_header":null,"semantic":"unknown","raw":null,"status":"blank"}]}]}
"""


def _vision_payload(image_path: Path) -> Tuple[bytes, str, str]:
    suffix = image_path.suffix.lower()
    raw = image_path.read_bytes()
    if len(raw) <= 4_500_000 and suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        mime = {".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/jpeg")
        return raw, mime, "original"
    from io import BytesIO

    from PIL import Image

    image = Image.open(image_path).convert("RGB")
    image.thumbnail((2048, 2048))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue(), "image/jpeg", "downscaled_2048"


def _parse_vision_json(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("vision response did not contain a JSON object")
    payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("vision JSON has no rows list")
    return payload


def _call_vision(image_path: Path) -> Tuple[List[Dict[str, Any]], str, str]:
    import base64
    import os
    import time

    from scripts.za_181_proposal_reliability import call_with_retries, generate_content_with_deadline

    data, mime, input_kind = _vision_payload(image_path)
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash").strip()
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": VISION_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode("ascii")}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 65536},
    }

    def attempt() -> Tuple[List[Dict[str, Any]], str, str]:
        response = generate_content_with_deadline(model=model, payload=payload, timeout=180)
        text = (
            ((response.json().get("candidates") or [{}])[0].get("content") or {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        parsed = _parse_vision_json(text or "")
        return apply_vision_proposal(parsed["rows"]), model, input_kind

    return call_with_retries(attempt, sleep=time.sleep)


def _save_progress_only(dataset: Dict[str, Any]) -> None:
    from scripts.za_181_proposal_reliability import atomic_write_text

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ids = [image["document_id"] for image in dataset["images"]]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate image ids")
    atomic_write_text(OUT_DIR / "ground_truth.json", json.dumps(dataset, indent=2))
    atomic_write_text(OUT_DIR / "ground_truth_progress.json", json.dumps(progress_from(dataset), indent=2))


def proposal_should_skip(image: Dict[str, Any]) -> bool:
    """Successful proposals and source-verified images are not requested again."""
    if image.get("annotation_origin") == "existing_source_verified" or image.get("human_verified"):
        return True
    if image.get("annotation_origin") == "AI_PROPOSED" and image.get("rows"):
        return True
    return False


def _apply_proposal_result(image: Dict[str, Any], value: Optional[Tuple[List[Dict[str, Any]], str, str]], exc: Optional[BaseException]) -> None:
    from scripts.za_181_proposal_reliability import atomic_write_text

    if exc is None and value is not None:
        rows, model, input_kind = value
        if not rows:
            exc = ValueError("vision returned no product rows")
        else:
            image["rows"] = rows
            image["verification_status"] = "unverified"
            image["annotation_origin"] = "AI_PROPOSED"
            image["ai_generated"] = True
            image["human_verified"] = False
            image["failure"] = None
            image["failure_class"] = None
            image["vision_model"] = model
            image["vision_input"] = input_kind
            image["ocr_used_as_label"] = False
            image["notes"] = [
                "AI_PROPOSED from the original image only. Not verified. "
                "Named quantities are set only when a printed header was copied. "
                "Blank cells were not stored as zero."
            ]
            atomic_write_text(
                OUT_DIR / "proposals" / f"{Path(image['document_id']).stem}.json",
                json.dumps(image, indent=2),
            )
            return
    image["verification_status"] = "proposal_failed"
    image["annotation_origin"] = "AI_PROPOSED_FAILED"
    image["ai_generated"] = False
    image["human_verified"] = False
    image["rows"] = []
    image["failure"] = str(exc) if exc else "vision failed"
    image["failure_class"] = getattr(exc, "failure_class", None)
    image["failure_attempts"] = getattr(exc, "attempts", None)
    image["ocr_used_as_label"] = False


def propose_annotations(limit: Optional[int] = None) -> Dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor
    import threading

    from scripts.za_181_proposal_reliability import proposal_concurrency

    dataset = load_dataset()
    verified_before = {
        image["document_id"]
        for image in dataset["images"]
        if image.get("annotation_origin") == "existing_source_verified"
    }
    (OUT_DIR / "proposals").mkdir(parents=True, exist_ok=True)
    pending = [image for image in dataset["images"] if not proposal_should_skip(image)]
    if limit is not None:
        pending = pending[:limit]

    def run_one(image: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Tuple[List[Dict[str, Any]], str, str]], Optional[BaseException]]:
        source = Path(image["source_file"])
        try:
            if not source.is_file():
                raise FileNotFoundError(str(source))
            return image, _call_vision(source), None
        except Exception as exc:
            return image, None, exc

    workers = proposal_concurrency()

    save_lock = threading.Lock()

    def finish(image: Dict[str, Any]) -> None:
        _image, value, exc = run_one(image)
        with save_lock:
            _apply_proposal_result(_image, value, exc)
            _save_progress_only(dataset)
            print(json.dumps({
                "document_id": _image["document_id"],
                "status": _image["verification_status"],
                "rows": len(_image.get("rows") or []),
                "failure_class": _image.get("failure_class"),
            }), flush=True)

    if workers == 1:
        for image in pending:
            finish(image)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(finish, pending))
    verified_after = {
        image["document_id"]
        for image in dataset["images"]
        if image.get("annotation_origin") == "existing_source_verified"
    }
    if verified_before != verified_after:
        raise RuntimeError("verified image set changed during proposal")
    return progress_from(dataset)


def serve(port: int) -> None:
    from scripts.za_181_human_review import serve_review

    serve_review(port)


def main(argv: Sequence[str]) -> int:
    command = argv[1] if len(argv) > 1 else "init"
    if command == "init":
        progress = init_dataset()
        print(json.dumps({key: progress[key] for key in (
            "images_total", "unique_images", "verified_images", "not_annotated_images",
            "unverified_images", "verified_rows", "uncertain_or_unreadable_fields",
        )}, indent=2))
        return 0
    if command == "propose":
        limit = None
        if "--limit" in argv:
            limit = int(argv[argv.index("--limit") + 1])
        progress = propose_annotations(limit=limit)
        print(json.dumps({
            "ai_proposed_images": progress.get("ai_proposed_images"),
            "proposal_failed_images": progress.get("proposal_failed_images"),
            "verified_images": progress.get("verified_images"),
            "not_annotated_images": progress.get("not_annotated_images"),
            "uncertain_or_unreadable_fields": progress.get("uncertain_or_unreadable_fields"),
        }, indent=2))
        return 0
    if command == "evaluate":
        raise SystemExit(
            "refusing to run evaluate: it rewrites ground_truth.json and the existing evaluation reports. "
            "Use review-evaluate, which writes only phase4/za_181_accuracy/human_review/."
        )
    if command in {"serve", "review"}:
        port = 8765
        if "--port" in argv:
            port = int(argv[argv.index("--port") + 1])
        serve(port)
        return 0
    if command == "review-init":
        from scripts.za_181_human_review import load_or_create_state, review_counts, review_dir

        state = load_or_create_state(review_dir(), load_dataset())
        print(json.dumps(review_counts(state), indent=2))
        return 0
    if command == "review-evaluate":
        from scripts.za_181_human_review import review_dir, run_verified_evaluation

        result = run_verified_evaluation(load_dataset(), review_dir())
        print(json.dumps({
            "images_scored": result["images_scored"],
            "source_verified_images": result["source_verified_images"],
            "human_review_images": result["human_review_images"],
            "ai_proposed_rows_excluded": result["ai_proposed_rows_excluded"],
            "sufficient_for_181_images": result["sufficient_for_181_images"],
        }, indent=2))
        return 0
    raise SystemExit(
        "usage: init | propose [--limit N] | review --port 8765 | review-init | review-evaluate"
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
