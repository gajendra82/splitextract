"""Phase 3 markdown report from inventory and comparison records."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List

from phase3.metrics import aggregate, recommend


_FAILURE_BUCKET = {
    "EMPTY_OUTPUT": "parser problem",
    "SCHEMA_INVALID": "validation problem",
    "STOCK_IDENTITY_FAILURE": "identity-profile problem",
    "DUPLICATE_ROWS": "parser problem",
    "MISSING_FIELDS": "parser problem",
    "LOW_TEXT_EVIDENCE": "OCR problem",
    "OCR_REQUIRED": "OCR problem",
    "GENERIC_PARSER_FAILURE": "parser problem",
    "LLM_EXTRACTION_FAILURE": "recovery problem",
    "PROVIDER_ERROR": "provider problem",
}


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _example(records: Iterable[Dict[str, Any]], predicate) -> str:
    for record in records:
        if predicate(record):
            return (
                f"- `{record.get('document_id')}` · {record.get('transition')} · "
                f"profile `{record.get('profile')}` · failure `{record.get('failure_type')}` · "
                f"OCR `{record.get('ocr_path')}` · verification `{record.get('verification')}`"
            )
    return "- none in this batch"


def _breakdown_lines(bucket: Dict[str, Dict[str, int]]) -> List[str]:
    lines = []
    for name in sorted(bucket):
        row = bucket[name]
        lines.append(
            f"- {name}: documents {row.get('documents', 0)}, "
            f"PASS → PASS {row.get('PASS → PASS', 0)}, "
            f"FAIL → RECOVERED {row.get('FAIL → RECOVERED', 0)}, "
            f"FAIL → UNRESOLVED {row.get('FAIL → UNRESOLVED', 0)}, "
            f"FAIL → WRONG {row.get('FAIL → WRONG', 0)}, "
            f"PASS → WRONG {row.get('PASS → WRONG', 0)}"
        )
    return lines or ["- none"]


def render_report(
    inventory: Dict[str, Any],
    records: List[Dict[str, Any]],
    *,
    batch_size: int,
) -> str:
    metrics = aggregate(records)
    decision = recommend(metrics)
    baseline = metrics["baseline"]
    phase2 = metrics["phase2"]
    safety = metrics["safety"]
    quality = metrics["quality"]
    averages = metrics["averages"]
    extensions = inventory.get("extensions") or {}
    failure_counts = Counter(
        str(record.get("failure_type") or "none")
        for record in records
        if record.get("baseline_outcome") == "FAIL"
    )
    failure_lines = [
        f"- {name}: {count} ({_FAILURE_BUCKET.get(name, 'unknown')})"
        for name, count in failure_counts.most_common()
    ] or ["- none"]
    duplicate_count = len(inventory.get("duplicate_groups") or [])
    lines = [
        "# Dataset",
        "",
        f"Path: {inventory.get('dataset_path')}",
        f"Total files: {inventory.get('total_files')}",
        f"Initial batch: {batch_size}",
        "Formats: " + ", ".join(f"{name} {count}" for name, count in sorted(extensions.items())),
        (
            "Layouts: router pdf kinds "
            + ", ".join(
                f"{name} {count}" for name, count in sorted((inventory.get("pdf_kinds") or {}).items())
            )
        ),
        f"PDF count: {inventory.get('pdf_count')}",
        f"Image count: {inventory.get('image_count')}",
        f"Excel count: {inventory.get('excel_count')}",
        f"Other count: {inventory.get('other_count')}",
        f"OCR-required files in the full dataset: {inventory.get('ocr_required')}",
        f"Digital-text PDF candidates: {inventory.get('digital_text_candidates')}",
        f"Scanned PDF candidates: {inventory.get('scanned_candidates')}",
        (
            "PDF page counts: "
            f"min {_fmt(inventory.get('page_count_min'))}, "
            f"max {_fmt(inventory.get('page_count_max'))}, "
            f"mean {_fmt(inventory.get('page_count_mean'))}"
        ),
        f"Duplicate content groups: {duplicate_count}",
        f"Previously processed by existing QA checkpoints: {inventory.get('previously_processed')}",
        "",
        "# Baseline",
        "",
        f"PASS: {baseline['PASS']}",
        f"FAIL: {baseline['FAIL']}",
        f"Provider errors: {baseline['provider_error']}",
        f"OCR required: {baseline['ocr_required']}",
        f"Extractor errors: {baseline['ERROR']}",
        "",
        "# Phase 2",
        "",
        f"Recovery attempted: {phase2['recovery_attempted']}",
        f"Recovery accepted: {phase2['recovery_accepted']}",
        f"Recovery rejected: {phase2['recovery_rejected']}",
        f"Recovery unresolved: {phase2['recovery_unresolved']}",
        f"Provider errors: {phase2['provider_error']}",
        f"Verified correct: {phase2['verified_correct']}",
        f"Verified wrong: {phase2['verified_wrong']}",
        f"Unverified: {phase2['unverified']}",
        "",
        "# SAFETY",
        "",
        f"PASS → WRONG: {safety['pass_to_wrong']}",
        f"FAIL → WRONG: {safety['fail_to_wrong']}",
        f"null became zero: {safety['null_became_zero']}",
        f"unknown layout accepted: {safety['unknown_layout_accepted']}",
        f"dynamic field changed: {safety['dynamic_field_changed']}",
        "",
        "# Quality",
        "",
        f"Verified recovery precision: {_fmt(quality['verified_recovery_precision'])}",
        f"Recovery acceptance rate: {_fmt(quality['recovery_acceptance_rate'])}",
        f"Recovery verification rate: {_fmt(quality['recovery_verification_rate'])}",
        f"Average recovery attempts: {_fmt(averages['recovery_attempts'])}",
        f"Average provider calls: {_fmt(averages['provider_calls'])}",
        f"Average processing time ms: {_fmt(averages['processing_time_ms'])}",
        "",
        "# Breakdown",
        "",
        "## By format",
        *_breakdown_lines(metrics["breakdown"]["file_type"]),
        "",
        "## By layout",
        *_breakdown_lines(metrics["breakdown"]["layout"]),
        "",
        "## By recovery strategy",
        *_breakdown_lines(metrics["breakdown"]["recovery_strategy"]),
        "",
        "## By failure type",
        *_breakdown_lines(metrics["breakdown"]["failure_type"]),
        "",
        "## By OCR",
        *_breakdown_lines(metrics["breakdown"]["ocr"]),
        "",
        "# OCR",
        "",
        "The current OCR architecture was not changed.",
        "PaddleOCR / PP-OCR is not called anywhere in this repository.",
        "Legacy Tesseract remains in the existing parser, the stock-row crop helper, and the independent QA reader. Phase 3 did not switch engines.",
        "PyMuPDF remains the digital-text and embedded-sparse path reported by the existing router.",
        "",
        "# ZL observations",
        "",
        "No filename, product, invoice, quantity, or coordinate rules were added.",
        "Recurring baseline failures in this batch, classified by the existing taxonomy:",
        *failure_lines,
        "",
        "# Representative Cases",
        "",
        "## Successful baseline",
        _example(records, lambda row: row.get("transition") == "PASS → PASS"),
        "",
        "## Successful recovery",
        _example(
            records,
            lambda row: row.get("transition") == "FAIL → RECOVERED"
            and row.get("verification") == "RECOVERY_VERIFIED_CORRECT",
        ),
        "",
        "## Accepted but unverified recovery",
        _example(
            records,
            lambda row: row.get("transition") == "FAIL → RECOVERED"
            and row.get("verification") == "RECOVERY_UNVERIFIED",
        ),
        "",
        "## Unresolved recovery",
        _example(records, lambda row: row.get("recovery_state") == "RECOVERY_UNRESOLVED"),
        "",
        "## Rejected recovery",
        _example(records, lambda row: row.get("recovery_state") == "RECOVERY_REJECTED"),
        "",
        "## Provider error",
        _example(records, lambda row: row.get("transition") == "FAIL → PROVIDER_ERROR"),
        "",
        "## Suspicious case",
        _example(
            records,
            lambda row: row.get("transition") in {"PASS → WRONG", "PASS → CHANGED", "FAIL → WRONG"}
            or "null_became_zero" in (row.get("safety_flags") or [])
            or "unknown_layout_accepted" in (row.get("safety_flags") or []),
        ),
        "",
        "# Recommendation",
        "",
        decision["recommendation"],
        "",
        decision["reason"],
        "",
        "This batch stopped after the initial representative sample. The remaining ZL August files were not processed.",
        "",
    ]
    return "\n".join(lines)
