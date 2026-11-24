"""Human review store for ZA 181 accuracy.

Saves go only to phase4/za_181_accuracy/human_review/. Source-verified labels,
AI proposal files, ground_truth.json, and the existing evaluation reports are
not written. AI proposals stay PENDING until a person sets VERIFIED, CORRECTED,
or UNCERTAIN and saves that choice.
"""

from __future__ import annotations

import csv
import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
from urllib.parse import unquote, urlparse

from scripts.compare_za_181_engines import QTY_FIELDS, parse_number
from scripts.za_181_accuracy_workflow import (
    OUT_DIR,
    QTY_LABELS,
    _aggregate_engine,
    _fmt,
    _load_predictions,
    score_document,
)
from scripts.za_181_proposal_reliability import atomic_write_text

REVIEW_STATUSES = {"PENDING", "VERIFIED", "CORRECTED", "UNCERTAIN"}
SCORED_STATUSES = {"VERIFIED", "CORRECTED"}
PAGE_PATH = Path(__file__).with_name("za_181_review_page.html")
LIMITATION = (
    "Scores from the images that currently have explicit labels cannot establish "
    "which OCR engine is more accurate across all 181 images."
)

CSV_COLUMNS = [
    "image_id",
    "filename",
    "row_id",
    "proposed_product_name",
    "proposed_quantity",
    "source_proposal_file",
    "review_status",
    "corrected_product_name",
    "corrected_quantity",
    "reviewer_notes",
    "source_image_path",
    "score_columns",
]


def review_dir(root: Optional[Path] = None) -> Path:
    base = root or (OUT_DIR / "human_review")
    return Path(base)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _quantity_text(columns: Sequence[Dict[str, Any]], field: str) -> str:
    parts = []
    for column in columns:
        if column.get("semantic") == "not_quantity":
            continue
        header = column.get("printed_header") or column.get("semantic") or f"col{column.get('position')}"
        raw = column.get(field)
        shown = "" if raw is None else str(raw)
        if field == "proposed_raw":
            parts.append(f"{header}={shown} [{column.get('proposed_status') or ''}]")
        elif str(raw or "").strip():
            parts.append(f"{header}={shown}")
    return "; ".join(parts)


def _column_from_source(column: Dict[str, Any], origin: str) -> Dict[str, Any]:
    return {
        "position": column.get("position"),
        "printed_header": column.get("printed_header"),
        "semantic": column.get("semantic") or "unknown",
        "proposed_raw": column.get("raw"),
        "proposed_status": column.get("status") or "absent",
        "corrected_raw": "",
        "score_column": False,
        "origin": origin,
    }


def _review_row(source: Dict[str, Any], origin: str) -> Dict[str, Any]:
    columns = [_column_from_source(column, origin) for column in source.get("columns") or []]
    if origin == "existing_source_verified" and not columns:
        for field in QTY_FIELDS:
            cell = source.get(field) or {}
            if cell.get("raw") is None or cell.get("status") == "absent":
                continue
            columns.append(
                {
                    "position": len(columns) + 1,
                    "printed_header": QTY_LABELS.get(field, field),
                    "semantic": field,
                    "proposed_raw": cell.get("raw"),
                    "proposed_status": cell.get("status"),
                    "corrected_raw": "",
                    "score_column": False,
                    "origin": origin,
                }
            )
    product = source.get("product_name") or {}
    return {
        "row_id": source.get("row_id"),
        "proposed_product_name": product.get("raw") or "",
        "corrected_product_name": "",
        "review_status": "SOURCE_LOCKED" if origin == "existing_source_verified" else "PENDING",
        "reviewer_notes": "",
        "origin": origin,
        "columns": columns,
    }


def build_state(dataset: Dict[str, Any]) -> Dict[str, Any]:
    images = []
    for image in dataset.get("images") or []:
        locked = image.get("annotation_origin") == "existing_source_verified"
        if locked:
            origin = "existing_source_verified"
        elif image.get("annotation_origin") == "AI_PROPOSED" and image.get("rows"):
            origin = "ai_proposed"
        elif image.get("verification_status") == "proposal_failed":
            origin = "proposal_failed"
        else:
            origin = "not_started"
        rows = [
            _review_row(row, "existing_source_verified" if locked else "ai_proposed")
            for row in (image.get("rows") or [])
        ]
        images.append(
            {
                "document_id": image["document_id"],
                "source_file": image.get("source_file") or "",
                "label_origin": origin,
                "locked_source_verified": locked,
                "source_proposal_file": (
                    f"proposals/{Path(image['document_id']).stem}.json" if origin == "ai_proposed" else ""
                ),
                "rows": rows,
            }
        )
    return {
        "version": 1,
        "rule": (
            "PENDING, UNCERTAIN, and AI_PROPOSED rows are not verified ground truth. "
            "Only a saved VERIFIED or CORRECTED review, or an existing source-verified image, is scored."
        ),
        "images": images,
    }


def _state_path(directory: Path) -> Path:
    return directory / "review_state.json"


def load_state(directory: Path) -> Dict[str, Any]:
    return json.loads(_state_path(directory).read_text(encoding="utf-8"))


def load_or_create_state(directory: Path, dataset: Dict[str, Any]) -> Dict[str, Any]:
    path = _state_path(directory)
    if path.is_file():
        return load_state(directory)
    state = build_state(dataset)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state, indent=2))
    save_review_csv(directory / "review_working.csv", csv_rows_from_state(state))
    return state


def save_state(directory: Path, state: Dict[str, Any]) -> Path:
    """Back up the previous state, then replace it atomically. Never writes ground truth."""
    directory = review_dir(directory)
    path = _state_path(directory)
    if path.is_file():
        backup = directory / "backups" / f"review_state_{_stamp()}.json"
        atomic_write_text(backup, path.read_text(encoding="utf-8"))
    atomic_write_text(path, json.dumps(state, indent=2))
    save_review_csv(directory / "review_working.csv", csv_rows_from_state(state))
    return path


def csv_rows_from_state(state: Dict[str, Any]) -> List[Dict[str, str]]:
    rows = []
    for image in state["images"]:
        if image.get("locked_source_verified"):
            continue
        for row in image.get("rows") or []:
            scored = [
                str(column.get("position"))
                for column in row.get("columns") or []
                if column.get("score_column")
            ]
            rows.append(
                {
                    "image_id": image["document_id"],
                    "filename": image["document_id"],
                    "row_id": row.get("row_id") or "",
                    "proposed_product_name": row.get("proposed_product_name") or "",
                    "proposed_quantity": _quantity_text(row.get("columns") or [], "proposed_raw"),
                    "source_proposal_file": image.get("source_proposal_file") or "",
                    "review_status": row.get("review_status") or "PENDING",
                    "corrected_product_name": row.get("corrected_product_name") or "",
                    "corrected_quantity": _quantity_text(row.get("columns") or [], "corrected_raw"),
                    "reviewer_notes": row.get("reviewer_notes") or "",
                    "source_image_path": image.get("source_file") or "",
                    "score_columns": ",".join(scored),
                }
            )
    return rows


def save_review_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in CSV_COLUMNS})


def load_review_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _same_proposed(left: Any, right: Any) -> bool:
    if left is None or str(left).strip() == "":
        return right is None or str(right).strip() == ""
    return str(left) == str(right)


def validate_review_row(row: Dict[str, Any]) -> None:
    status = row.get("review_status")
    if status not in REVIEW_STATUSES:
        raise ValueError(f"review status must be one of {sorted(REVIEW_STATUSES)}")
    notes = str(row.get("reviewer_notes") or "").strip()
    if status in SCORED_STATUSES and not notes:
        raise ValueError(f"{status} requires reviewer notes")
    if row.get("origin") == "human_added" and status == "VERIFIED":
        raise ValueError("a row with no AI proposal cannot be VERIFIED; use CORRECTED")
    corrected_name = str(row.get("corrected_product_name") or "").strip()
    if status == "VERIFIED" and corrected_name:
        raise ValueError("VERIFIED cannot change the product name; use CORRECTED")
    included = []
    for column in row.get("columns") or []:
        if status in {"PENDING", "UNCERTAIN"}:
            column["score_column"] = False
        semantic = column.get("semantic") or "unknown"
        corrected = str(column.get("corrected_raw") or "").strip()
        if status == "VERIFIED" and corrected:
            raise ValueError("VERIFIED cannot change a quantity; use CORRECTED")
        wants_score = bool(column.get("score_column")) or (status == "CORRECTED" and bool(corrected))
        if wants_score and semantic not in QTY_FIELDS:
            header = column.get("printed_header") or semantic
            raise ValueError(
                f"column {header!r} is not a named stock quantity and cannot be scored"
            )
        if status == "VERIFIED" and column.get("score_column"):
            if parse_number(column.get("proposed_raw")) is None:
                raise ValueError("a scored quantity must be a number")
            included.append(semantic)
        if status == "CORRECTED" and wants_score:
            raw = corrected if corrected else column.get("proposed_raw")
            if parse_number(raw) is None:
                raise ValueError("a scored quantity must be a number")
            included.append(semantic)
    if status == "CORRECTED":
        name_changed = bool(corrected_name) and corrected_name != str(row.get("proposed_product_name") or "").strip()
        quantity_changed = any(str(column.get("corrected_raw") or "").strip() for column in row.get("columns") or [])
        if not name_changed and not quantity_changed and row.get("origin") != "human_added":
            if not any(column.get("score_column") for column in row.get("columns") or []):
                raise ValueError("CORRECTED requires a corrected name or quantity")
        if row.get("origin") == "human_added" and not corrected_name:
            raise ValueError("CORRECTED requires the product name read from the image")
    duplicates = {item for item in included if included.count(item) > 1}
    if duplicates:
        raise ValueError(
            "ambiguous quantity columns were not saved for scoring: " + ", ".join(sorted(duplicates))
        )


def _position(value: Any) -> Any:
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value


def _proposal_columns(row: Dict[str, Any]) -> Dict[Any, Dict[str, Any]]:
    return {
        _position(column.get("position")): column
        for column in row.get("columns") or []
        if column.get("origin") != "human_added"
    }


def _apply_existing_row(current: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    updated = deepcopy(current)
    if not _same_proposed(current.get("proposed_product_name"), incoming.get("proposed_product_name")):
        raise ValueError("proposed product name cannot be changed")
    current_columns = _proposal_columns(current)
    seen = set()
    merged = []
    for column in incoming.get("columns") or []:
        position = _position(column.get("position"))
        if column.get("origin") == "human_added" or position not in current_columns:
            if column.get("origin") != "human_added" and position in current_columns:
                pass
            added = {
                "position": position,
                "printed_header": column.get("printed_header"),
                "semantic": column.get("semantic") or "unknown",
                "proposed_raw": None,
                "proposed_status": "absent",
                "corrected_raw": column.get("corrected_raw") or "",
                "score_column": bool(column.get("score_column")),
                "origin": "human_added",
            }
            if position in current_columns:
                raise ValueError("proposed quantity columns cannot be replaced")
            merged.append(added)
            continue
        original = current_columns[position]
        seen.add(position)
        if not _same_proposed(original.get("proposed_raw"), column.get("proposed_raw")):
            raise ValueError("proposed quantity cannot be changed")
        if (original.get("semantic") or "unknown") != (column.get("semantic") or "unknown"):
            raise ValueError("proposed column meaning cannot be changed")
        if (original.get("printed_header") or None) != (column.get("printed_header") or None):
            raise ValueError("proposed column header cannot be changed")
        copied = deepcopy(original)
        copied["corrected_raw"] = column.get("corrected_raw") or ""
        copied["score_column"] = bool(column.get("score_column"))
        merged.append(copied)
    if set(current_columns) - seen:
        raise ValueError("proposed quantity columns cannot be removed")
    updated["columns"] = merged
    updated["corrected_product_name"] = incoming.get("corrected_product_name") or ""
    updated["review_status"] = incoming.get("review_status") or "PENDING"
    updated["reviewer_notes"] = str(incoming.get("reviewer_notes") or "").strip()
    validate_review_row(updated)
    return updated


def _human_row(incoming: Dict[str, Any], existing_ids: set) -> Dict[str, Any]:
    row_id = str(incoming.get("row_id") or "").strip()
    if not row_id or row_id in existing_ids:
        number = 1
        while f"h{number:02d}" in existing_ids:
            number += 1
        row_id = f"h{number:02d}"
    columns = []
    for index, column in enumerate(incoming.get("columns") or [], start=1):
        columns.append(
            {
                "position": column.get("position") or index,
                "printed_header": column.get("printed_header"),
                "semantic": column.get("semantic") or "unknown",
                "proposed_raw": None,
                "proposed_status": "absent",
                "corrected_raw": column.get("corrected_raw") or "",
                "score_column": bool(column.get("score_column")),
                "origin": "human_added",
            }
        )
    row = {
        "row_id": row_id,
        "proposed_product_name": "",
        "corrected_product_name": incoming.get("corrected_product_name") or "",
        "review_status": incoming.get("review_status") or "PENDING",
        "reviewer_notes": str(incoming.get("reviewer_notes") or "").strip(),
        "origin": "human_added",
        "columns": columns,
    }
    validate_review_row(row)
    return row


def apply_image_update(state: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    document_id = payload.get("document_id")
    image = next((item for item in state["images"] if item["document_id"] == document_id), None)
    if image is None:
        raise ValueError("unknown image")
    if image.get("locked_source_verified"):
        raise ValueError("source-verified labels are locked")
    incoming_rows = payload.get("rows") or []
    current = {row["row_id"]: row for row in image.get("rows") or []}
    proposal_ids = {row_id for row_id, row in current.items() if row.get("origin") != "human_added"}
    incoming_ids = {str(row.get("row_id") or "") for row in incoming_rows}
    if proposal_ids - incoming_ids:
        raise ValueError("proposal rows cannot be deleted")
    updated_rows = []
    used = set()
    for incoming in incoming_rows:
        row_id = str(incoming.get("row_id") or "")
        if row_id in current and current[row_id].get("origin") != "human_added":
            updated_rows.append(_apply_existing_row(current[row_id], incoming))
            used.add(row_id)
        else:
            created = _human_row(incoming, set(current) | used)
            updated_rows.append(created)
            used.add(created["row_id"])
    image["rows"] = updated_rows
    return image


def _status_rank(status: str) -> int:
    return {"PENDING": 0, "UNCERTAIN": 1, "VERIFIED": 2, "CORRECTED": 2}.get(status, -1)


def import_review_csv(state: Dict[str, Any], rows: Sequence[Dict[str, str]], *, allow_downgrade: bool = False) -> int:
    """Apply review fields from a CSV. Proposed names and quantities stay as stored."""
    updated = 0
    images = {image["document_id"]: image for image in state["images"]}
    for csv_row in rows:
        image = images.get(csv_row.get("image_id") or "")
        if image is None or image.get("locked_source_verified"):
            raise ValueError("CSV refers to an unknown or locked image")
        current = next((row for row in image["rows"] if row["row_id"] == csv_row.get("row_id")), None)
        if current is None:
            raise ValueError(f"unknown review row {csv_row.get('row_id')}")
        if not _same_proposed(current.get("proposed_product_name"), csv_row.get("proposed_product_name")):
            raise ValueError("CSV cannot change the proposed product name")
        new_status = csv_row.get("review_status") or "PENDING"
        if not allow_downgrade and _status_rank(new_status) < _status_rank(current.get("review_status")):
            raise ValueError("refusing to downgrade a reviewed row")
        selected = {
            int(part.strip())
            for part in str(csv_row.get("score_columns") or "").split(",")
            if part.strip().isdigit()
        }
        incoming = deepcopy(current)
        incoming["review_status"] = new_status
        incoming["corrected_product_name"] = csv_row.get("corrected_product_name") or ""
        incoming["reviewer_notes"] = csv_row.get("reviewer_notes") or ""
        for column in incoming["columns"]:
            column["score_column"] = column.get("position") in selected
        replaced = _apply_existing_row(current, incoming)
        image["rows"][image["rows"].index(current)] = replaced
        updated += 1
    return updated


def review_counts(state: Dict[str, Any]) -> Dict[str, int]:
    editable = [row for image in state["images"] if not image.get("locked_source_verified") for row in image["rows"]]
    return {
        "images": len(state["images"]),
        "source_verified_images": sum(1 for image in state["images"] if image.get("locked_source_verified")),
        "ai_proposed_images": sum(1 for image in state["images"] if image.get("label_origin") == "ai_proposed"),
        "proposal_failed_images": sum(1 for image in state["images"] if image.get("label_origin") == "proposal_failed"),
        "not_started_images": sum(1 for image in state["images"] if image.get("label_origin") == "not_started"),
        "pending_rows": sum(1 for row in editable if row.get("review_status") == "PENDING"),
        "verified_rows": sum(1 for row in editable if row.get("review_status") == "VERIFIED"),
        "corrected_rows": sum(1 for row in editable if row.get("review_status") == "CORRECTED"),
        "uncertain_rows": sum(1 for row in editable if row.get("review_status") == "UNCERTAIN"),
    }


def _blank_scored_row(row_id: str, index: int, name: str) -> Dict[str, Any]:
    absent = {"raw": None, "normalized": None, "status": "absent"}
    return {
        "row_id": row_id,
        "row_index": index,
        "product_name": {"raw": name, "normalized": name, "status": "verified" if name else "unreadable"},
        "packing": dict(absent),
        **{field: dict(absent) for field in QTY_FIELDS},
    }


def scored_rows_from_review(image: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return only rows a person explicitly saved as VERIFIED or CORRECTED."""
    scored = []
    for index, row in enumerate(image.get("rows") or [], start=1):
        status = row.get("review_status")
        if status not in SCORED_STATUSES:
            continue
        if not str(row.get("reviewer_notes") or "").strip():
            continue
        if status == "CORRECTED" and str(row.get("corrected_product_name") or "").strip():
            name = str(row["corrected_product_name"]).strip()
        else:
            name = str(row.get("proposed_product_name") or "").strip()
        built = _blank_scored_row(str(row.get("row_id") or f"r{index:02d}"), index, name)
        for column in row.get("columns") or []:
            semantic = column.get("semantic") or "unknown"
            corrected = str(column.get("corrected_raw") or "").strip()
            include = bool(column.get("score_column")) or (status == "CORRECTED" and bool(corrected))
            if not include or semantic not in QTY_FIELDS:
                continue
            raw = corrected if corrected else column.get("proposed_raw")
            number = parse_number(raw)
            if number is None:
                continue
            if built[semantic]["status"] == "verified":
                built[semantic] = {"raw": None, "normalized": None, "status": "absent"}
                continue
            built[semantic] = {"raw": raw, "normalized": number, "status": "verified"}
        scored.append(built)
    return scored


def evaluate_verified(
    dataset: Dict[str, Any],
    review_state: Optional[Dict[str, Any]],
    load_prediction: Callable[[str, str], tuple],
) -> Dict[str, Any]:
    """Score source-verified images plus explicitly saved human review rows."""
    source_images = [
        image
        for image in dataset.get("images") or []
        if image.get("annotation_origin") == "existing_source_verified"
        and image.get("verification_status") == "verified"
        and image.get("rows")
    ]
    human_images = []
    counts = review_counts(review_state) if review_state else {
        "pending_rows": 0,
        "verified_rows": 0,
        "corrected_rows": 0,
        "uncertain_rows": 0,
        "ai_proposed_images": 0,
        "proposal_failed_images": 0,
        "not_started_images": 0,
    }
    if review_state:
        for image in review_state["images"]:
            if image.get("locked_source_verified"):
                continue
            rows = scored_rows_from_review(image)
            if not rows:
                continue
            human_images.append(
                {
                    "document_id": image["document_id"],
                    "verification_status": "verified",
                    "rows": rows,
                    "label_origin": "human_review",
                }
            )
    scored_images = list(source_images) + human_images
    engines = {}
    for engine_name in ("tesseract", "paddleocr-vl"):
        documents = []
        for image in scored_images:
            cache_engine, rows, text = load_prediction(engine_name, image["document_id"])
            if engine_name == "tesseract" and cache_engine != "tesseract":
                raise RuntimeError(f"{image['document_id']} tesseract cache engine {cache_engine}")
            if engine_name == "paddleocr-vl" and "paddleocr-vl" not in str(cache_engine):
                raise RuntimeError(f"{image['document_id']} paddle cache engine {cache_engine}")
            scored = score_document(image, rows, text)
            scored["cache_engine"] = cache_engine
            scored["label_origin"] = image.get("annotation_origin") or image.get("label_origin")
            documents.append(scored)
        engines[engine_name] = _aggregate_engine(documents)
    ai_rows = sum(
        len(image.get("rows") or [])
        for image in dataset.get("images") or []
        if image.get("annotation_origin") == "AI_PROPOSED" and not image.get("human_verified")
    )
    return {
        "authoritative": True,
        "sufficient_for_181_images": len(scored_images) >= 181,
        "limitation": LIMITATION,
        "images_scored": len(scored_images),
        "source_verified_images": len(source_images),
        "source_verified_rows": sum(len(image.get("rows") or []) for image in source_images),
        "human_review_images": len(human_images),
        "human_review_rows": sum(len(image["rows"]) for image in human_images),
        "ai_proposed_rows_excluded": ai_rows,
        "pending_rows_excluded": counts["pending_rows"],
        "uncertain_rows_excluded": counts["uncertain_rows"],
        "document_ids": [image["document_id"] for image in scored_images],
        "engines": engines,
        "rules": [
            "Source-verified images are scored from the existing labels.",
            "A review row is scored only when its saved status is VERIFIED or CORRECTED and it has reviewer notes.",
            "PENDING, UNCERTAIN, and AI_PROPOSED rows are excluded. They are not counted as missing products.",
            "A product is correct only when the full printed name occurs in that engine's OCR text. One occurrence is consumed per scored row.",
            "A shorter fragment of a scored name is neither a correct product nor an extra product.",
            "A quantity is correct when the expected number appears in the text window after that product and before the next different scored product.",
            "That window does not prove which printed column the number came from.",
            "A missing number is not treated as zero. A different number in the window is a silent quantity error.",
            "Unknown, amount, and price columns are not mapped to opening, receipt, sales, or closing.",
            "Duplicate meanings for the same quantity are rejected before they can be scored.",
        ],
    }


def _markdown(result: Dict[str, Any]) -> str:
    lines = [
        "# Verified OCR accuracy",
        "",
        result["limitation"],
        "",
        f"Images scored: {result['images_scored']}.",
        f"Source-verified images: {result['source_verified_images']} ({result['source_verified_rows']} rows).",
        f"Human-reviewed images added: {result['human_review_images']} ({result['human_review_rows']} rows).",
        f"AI-proposed rows excluded: {result['ai_proposed_rows_excluded']}.",
        f"Pending review rows excluded: {result['pending_rows_excluded']}.",
        f"Uncertain review rows excluded: {result['uncertain_rows_excluded']}.",
        "",
        "## Rules",
        "",
    ]
    lines.extend(f"- {rule}" for rule in result["rules"])
    lines.extend(["", "## Engines", ""])
    for engine, summary in result["engines"].items():
        lines.append(f"### {engine}")
        lines.append("")
        lines.append(
            f"Product precision {_fmt(summary['product_precision'])} "
            f"({summary['product_correct']}/{summary['product_correct'] + summary['product_extra']}), "
            f"recall {_fmt(summary['product_recall'])} "
            f"({summary['product_correct']}/{summary['product_correct'] + summary['product_missing']}), "
            f"F1 {_fmt(summary['product_f1'])}."
        )
        lines.append(
            f"Quantity accuracy {_fmt(summary['quantity_accuracy'])} "
            f"({summary['quantity_correct']}/{summary['quantity_scored']}). "
            f"Silent quantity errors {summary['silent_wrong']} "
            f"({_fmt(summary['silent_wrong_rate'])} of both-present quantities)."
        )
        lines.append(
            f"Missing product rows {summary['product_missing']}. "
            f"Extra product rows {summary['product_extra']}."
        )
        lines.append("")
        lines.append("| Image | Product precision | Product recall | Product F1 | Quantity accuracy | Silent quantity errors | Missing products | Extra products |")
        lines.append("| --- | --- | --- | --- | --- | ---: | ---: | ---: |")
        for doc in summary["documents"]:
            lines.append(
                "| {document_id} | {pp} | {pr} | {pf} | {qa} | {silent} | {missing} | {extra} |".format(
                    document_id=doc["document_id"],
                    pp=_fmt(doc["product_precision"]),
                    pr=_fmt(doc["product_recall"]),
                    pf=_fmt(doc["product_f1"]),
                    qa=_fmt(doc["quantity_accuracy"]),
                    silent=doc["silent_wrong"],
                    missing=doc["product_missing"],
                    extra=doc["product_extra"],
                )
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def write_verified_evaluation(directory: Path, result: Dict[str, Any]) -> None:
    directory = review_dir(directory)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / "verified_accuracy.json", json.dumps(result, indent=2))
    atomic_write_text(directory / "VERIFIED_ACCURACY.md", _markdown(result))


def real_prediction(engine_name: str, document_id: str):
    from scripts.za_181_accuracy_workflow import TESS_RUN, VL_RUN

    run = {"tesseract": TESS_RUN, "paddleocr-vl": VL_RUN}[engine_name]
    return _load_predictions(run, document_id)


def run_verified_evaluation(dataset: Dict[str, Any], directory: Path) -> Dict[str, Any]:
    state = load_state(directory) if _state_path(directory).is_file() else None
    result = evaluate_verified(dataset, state, real_prediction)
    write_verified_evaluation(directory, result)
    return result


class HumanReviewHandler(BaseHTTPRequestHandler):
    directory: Path = OUT_DIR / "human_review"
    dataset: Dict[str, Any] = {}
    state: Dict[str, Any] = {}
    lock = threading.Lock()

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Dict[str, Any]) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE_PATH.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/index":
            counts = review_counts(self.state)
            images = []
            for image in self.state["images"]:
                statuses = [row.get("review_status") for row in image.get("rows") or []]
                images.append(
                    {
                        "document_id": image["document_id"],
                        "label_origin": image.get("label_origin"),
                        "locked_source_verified": bool(image.get("locked_source_verified")),
                        "row_count": len(image.get("rows") or []),
                        "pending": statuses.count("PENDING"),
                        "verified": statuses.count("VERIFIED"),
                        "corrected": statuses.count("CORRECTED"),
                        "uncertain": statuses.count("UNCERTAIN"),
                    }
                )
            self._json(200, {"counts": counts, "images": images, "limitation": LIMITATION})
            return
        if path.startswith("/api/review/"):
            document_id = unquote(path[len("/api/review/") :])
            image = next((item for item in self.state["images"] if item["document_id"] == document_id), None)
            if image is None:
                self._json(404, {"error": "unknown image"})
                return
            self._json(200, image)
            return
        if path.startswith("/api/ocr/"):
            document_id = unquote(path[len("/api/ocr/") :])
            payload = {}
            for engine_name in ("tesseract", "paddleocr-vl"):
                try:
                    cache_engine, rows, text = real_prediction(engine_name, document_id)
                    payload[engine_name] = {
                        "engine": cache_engine,
                        "text": text,
                        "products": [row.get("product") for row in rows if row.get("product")],
                    }
                except Exception as exc:  # noqa: BLE001
                    payload[engine_name] = {"error": str(exc), "text": "", "products": []}
            self._json(200, payload)
            return
        if path.startswith("/image/"):
            document_id = unquote(path[len("/image/") :])
            image = next((item for item in self.state["images"] if item["document_id"] == document_id), None)
            source = Path(image["source_file"]) if image else None
            if source is None or not source.is_file():
                self._send(404, b"missing image", "text/plain")
                return
            kind = "image/jpeg"
            suffix = source.suffix.lower()
            if suffix == ".png":
                kind = "image/png"
            elif suffix == ".webp":
                kind = "image/webp"
            self._send(200, source.read_bytes(), kind)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/save":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or "0")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            with self.lock:
                self.state = load_state(self.directory)
                apply_image_update(self.state, payload)
                save_state(self.directory, self.state)
            self._json(200, {"ok": True, "counts": review_counts(self.state)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve_review(port: int, directory: Optional[Path] = None, dataset: Optional[Dict[str, Any]] = None) -> None:
    from scripts.za_181_accuracy_workflow import load_dataset

    HumanReviewHandler.directory = review_dir(directory)
    HumanReviewHandler.dataset = dataset if dataset is not None else load_dataset()
    HumanReviewHandler.state = load_or_create_state(HumanReviewHandler.directory, HumanReviewHandler.dataset)
    server = ThreadingHTTPServer(("127.0.0.1", port), HumanReviewHandler)
    print(f"http://127.0.0.1:{port}")
    server.serve_forever()
