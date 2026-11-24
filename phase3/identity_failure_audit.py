"""Read-only audit of the Phase 3 STOCK_IDENTITY_FAILURE documents.

Does not extract, does not call OCR, and does not change validation.
Uses a saved QA extract only when the row count and extraction method match
the Phase 3 record.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.stock_identity_profiles import (
    TOLERANCE,
    generic_expected_closing,
    issues_expected_closing,
    saleret_expected_closing,
    select_profile,
)

ROOT = Path(__file__).resolve().parents[1]
DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
QA_DIRS = (
    ROOT / "qa_results_images" / "extracted",
    ROOT / "qa_results" / "extracted",
)


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extra(item: Dict[str, Any]) -> Dict[str, Any]:
    extra = item.get("extra")
    return extra if isinstance(extra, dict) else {}


def _find_qa(document_id: str) -> Optional[Path]:
    stem = Path(document_id).stem
    for folder in QA_DIRS:
        candidate = folder / f"{stem}.json"
        if candidate.is_file():
            return candidate
    return None


def _expected(profile_id: str, item: Dict[str, Any]) -> float:
    if profile_id in {"product_wise", "product_wise_image"}:
        return issues_expected_closing(item)
    if profile_id == "saleret":
        return saleret_expected_closing(item)
    return generic_expected_closing(item)


def _row_view(profile_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
    extra = _extra(item)
    opening = _num(item.get("opening_qty"))
    receipts = _num(item.get("receipts_qty"))
    sales = _num(item.get("sales_qty"))
    closing = _num(item.get("closing_qty"))
    scheme = _num(extra.get("sales_scheme_qty") or extra.get("sales_scheme"))
    stock_out = _num(extra.get("stock_out_qty"))
    expected = _expected(profile_id, item)
    delta = None if closing is None else round(closing - expected, 2)
    movement_keys = {
        key: extra.get(key)
        for key in extra
        if any(token in key for token in ("free", "scheme", "return", "issue", "shortage", "sample", "exp"))
        and not isinstance(extra.get(key), (dict, list))
    }
    return {
        "opening": opening,
        "receipts": receipts,
        "sales": sales,
        "scheme": scheme,
        "stock_out": stock_out,
        "closing": closing,
        "expected_closing": expected,
        "delta": delta,
        "extra_movements": movement_keys,
        "parser_expected": extra.get("expected_closing"),
        "parser_identity_ok": extra.get("stock_identity_ok"),
    }


def _classify_row(view: Dict[str, Any]) -> str:
    delta = view["delta"]
    if delta is None:
        return "closing_missing"
    if abs(delta) <= TOLERANCE:
        return "equation_holds"
    extras = [
        _num(value)
        for value in (view["extra_movements"] or {}).values()
        if _num(value) not in (None, 0.0)
    ]
    if any(abs(abs(delta) - abs(value)) <= TOLERANCE for value in extras):
        return "delta_equals_unapplied_column"
    if view["opening"] is None or view["receipts"] is None or view["sales"] is None:
        return "canonical_null_in_equation"
    return "equation_mismatch"


def audit() -> Dict[str, Any]:
    documents: List[Dict[str, Any]] = []
    for path in sorted((ROOT / "phase3_eval" / "records").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("failure_type") != "STOCK_IDENTITY_FAILURE":
            continue
        qa_path = _find_qa(record["document_id"])
        entry: Dict[str, Any] = {
            "document_id": record["document_id"],
            "file_type": record.get("file_type"),
            "profile": record.get("profile"),
            "extraction_method": record.get("extraction_method"),
            "ocr_required": record.get("ocr_required"),
            "ocr_path": record.get("ocr_path"),
            "ocr_quality": record.get("ocr_quality"),
            "recovery_strategy": record.get("recovery_strategy"),
            "recovery_state": record.get("recovery_state"),
            "attempts": record.get("attempts"),
            "baseline_row_count": record.get("baseline_row_count"),
            "qa_extract": None,
            "qa_status": "missing",
        }
        if qa_path is not None:
            extracted = json.loads(qa_path.read_text(encoding="utf-8"))
            rows = [item for item in (extracted.get("line_items") or []) if isinstance(item, dict)]
            method = str(((extracted.get("totals") or {}).get("extra") or {}).get("extraction_method") or "")
            same_rows = len(rows) == record.get("baseline_row_count")
            same_method = (not method) or method == record.get("extraction_method")
            if same_rows and same_method:
                profile = select_profile(extracted)
                views = [_row_view(profile.profile_id, item) for item in rows]
                classes = Counter(_classify_row(view) for view in views)
                failing = [view for view in views if view["delta"] is None or abs(view["delta"]) > TOLERANCE]
                entry.update(
                    {
                        "qa_extract": str(qa_path.relative_to(ROOT)),
                        "qa_status": "matched",
                        "profile_on_extract": profile.profile_id,
                        "row_classes": dict(classes),
                        "failing_rows": len(failing),
                        "sample_failures": failing[:3],
                    }
                )
            else:
                entry["qa_status"] = "stale"
                entry["qa_rows"] = len(rows)
                entry["qa_method"] = method
        documents.append(entry)
    return {"documents": documents, "count": len(documents)}


if __name__ == "__main__":
    payload = audit()
    out = ROOT / "phase3_eval" / "identity_failure_audit.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(out)
    print("matched", sum(1 for item in payload["documents"] if item["qa_status"] == "matched"))
    print("stale", sum(1 for item in payload["documents"] if item["qa_status"] == "stale"))
    print("missing", sum(1 for item in payload["documents"] if item["qa_status"] == "missing"))
