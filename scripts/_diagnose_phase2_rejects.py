"""Diagnose why Phase 2 Gemini JSON stays unresolved on GT-20 cases."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from services.hybrid_schema import SalesExtractionOutput
from services.recovery_runner import (
    _STOCK_FIELDS,
    _match_row,
    _norm,
    _recoverable_product_names,
    _same_number,
    merge_stock_recovery,
    _prompt_for,
)
from services.ground_truth.schema import only_source_verified_fields

PER = ROOT / "phase4" / "phase2_ab_gt20" / "per_document"
GT = ROOT / "phase4" / "ground_truth_50" / "per_document"
OUT = ROOT / "phase4" / "phase2_ab_gt20" / "reject_diagnosis.json"


def _failing_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if extra.get("stock_identity_ok") is False or extra.get("source_discrepancy"):
            rows.append(item)
    return rows


def _qty_snapshot(item: Dict[str, Any]) -> Dict[str, Any]:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    return {
        "product_name": item.get("product_name"),
        "opening_qty": item.get("opening_qty"),
        "receipts_qty": item.get("receipts_qty"),
        "sales_qty": item.get("sales_qty"),
        "closing_qty": item.get("closing_qty"),
        "stock_out_qty": extra.get("stock_out_qty"),
        "stock_identity_ok": extra.get("stock_identity_ok"),
        "expected_closing": extra.get("expected_closing"),
        "validation_error": extra.get("validation_error") or extra.get("source_validation_error"),
    }


def _gt_qty_for(gt_fields: List[Dict[str, Any]], product: str) -> Dict[str, Any]:
    want = _norm(product)
    out: Dict[str, Any] = {}
    for field in gt_fields:
        if field.get("verification") != "SOURCE_VERIFIED":
            continue
        row_key = _norm(field.get("row_key") or field.get("product_name") or "")
        if row_key != want and want not in row_key and row_key not in want:
            continue
        name = field.get("field_name") or field.get("name")
        if name in {
            "opening_qty",
            "receipts_qty",
            "sales_qty",
            "closing_qty",
            "stock_out_qty",
            "product_name",
        }:
            out[name] = field.get("value")
    return out


def _simulate_merge(off_result: Dict[str, Any], returned_rows_text: List[str]) -> Dict[str, Any]:
    """Best-effort rebuild of Gemini SalesExtractionOutput from returned log strings is hard;
    instead inspect matchability using product names extracted from returned strings.
    """
    return {}


def diagnose(doc_id: str) -> Dict[str, Any]:
    bench = json.loads((PER / f"{doc_id}.json").read_text(encoding="utf-8"))
    gt_path = GT / f"{doc_id}.json"
    gt_fields = []
    if gt_path.is_file():
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        gt_fields = only_source_verified_fields(gt)

    off = (bench.get("off") or {}).get("result") or {}
    on = bench.get("on") or {}
    phase2 = ((on.get("result") or {}).get("phase2") or {})
    failing = _failing_rows(off)
    allowed = _recoverable_product_names(off)
    prompt = _prompt_for(off, phase2.get("strategy") or "targeted_stock_extraction")

    rows_diag = []
    for item in failing:
        snap = _qty_snapshot(item)
        gt_vals = _gt_qty_for(gt_fields, str(item.get("product_name") or ""))
        rows_diag.append({"python_failing": snap, "gt_source_verified": gt_vals})

    # Analyze recovery_log returned values vs failing names
    log_analysis = []
    for entry in phase2.get("recovery_log") or []:
        returned = entry.get("returned") or []
        matched = []
        unmatched = []
        empty_name = 0
        all_null_qty = 0
        partial_qty = 0
        for line in returned:
            # format: "NAME opening=.. receipts=.. sales=.. stock_out=.. closing=.."
            name = line.split(" opening=")[0] if " opening=" in line else line
            vals = {}
            for key in ("opening", "receipts", "sales", "stock_out", "closing"):
                token = f"{key}="
                if token in line:
                    raw = line.split(token, 1)[1].split(" ", 1)[0]
                    vals[key] = None if raw == "None" else raw
            if not str(name).strip():
                empty_name += 1
            qty_vals = [vals.get(k) for k in ("opening", "receipts", "sales", "closing")]
            if all(v is None for v in qty_vals):
                all_null_qty += 1
            elif any(v is None for v in qty_vals) and any(v is not None for v in qty_vals):
                partial_qty += 1
            n = _norm(name)
            if allowed is not None and n in allowed:
                matched.append({"name": name, "vals": vals})
            else:
                # fuzzy: any failing contains
                hit = None
                for fail in failing:
                    fn = _norm(fail.get("product_name"))
                    if n and fn and (n == fn or n in fn or fn in n):
                        hit = fail.get("product_name")
                        break
                unmatched.append({"name": name, "vals": vals, "fuzzy_failing": hit})
        log_analysis.append(
            {
                "attempt": entry.get("attempt"),
                "parsed": entry.get("parsed"),
                "accepted": entry.get("accepted"),
                "returned_count": len(returned),
                "exact_allowed_matches": len(matched),
                "non_exact": len(unmatched),
                "empty_product_name": empty_name,
                "all_null_qty_rows": all_null_qty,
                "partial_qty_rows": partial_qty,
                "matched_sample": matched[:5],
                "unmatched_sample": unmatched[:5],
            }
        )

    return {
        "document_id": doc_id,
        "phase2_status": phase2.get("status"),
        "failure_type": phase2.get("failure_type"),
        "strategy": phase2.get("strategy"),
        "failing_row_count": len(failing),
        "allowed_count": None if allowed is None else len(allowed),
        "prompt_chars": len(prompt),
        "prompt_has_product_names": any(
            str(item.get("product_name") or "") and str(item.get("product_name")) in prompt
            for item in failing[:5]
        ),
        "prompt_has_stock_words": any(
            w in prompt.lower() for w in ("opening", "closing", "sales", "receipt")
        ),
        "prompt_excerpt": prompt[:1200],
        "failing_rows": rows_diag[:8],
        "log_analysis": log_analysis,
    }


def main() -> None:
    docs = []
    for path in sorted(PER.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        status = ((d.get("on") or {}).get("telemetry") or {}).get("phase2_status")
        if status in (None, "not_needed"):
            continue
        docs.append(diagnose(path.name.replace(".json", "")))
    OUT.write_text(json.dumps({"documents": docs}, indent=2), encoding="utf-8")
    print("wrote", OUT)
    for doc in docs:
        la = (doc.get("log_analysis") or [{}])[0]
        print(
            doc["document_id"][:40],
            "fail",
            doc["failing_row_count"],
            "status",
            doc["phase2_status"],
            "exact",
            la.get("exact_allowed_matches"),
            "nonexact",
            la.get("non_exact"),
            "empty_name",
            la.get("empty_product_name"),
            "all_null",
            la.get("all_null_qty_rows"),
            "partial",
            la.get("partial_qty_rows"),
        )


if __name__ == "__main__":
    main()
