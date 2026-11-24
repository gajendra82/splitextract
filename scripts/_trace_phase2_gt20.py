"""Trace Phase 2 trigger/skip reasons and recovery payload evidence for GT-20.

Uses stored VL OCR + the same core path as the A/B benchmark. No accuracy claims.
Writes phase4/phase2_ab_gt20/PHASE2_TRACE.md (and .json).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

from scripts.benchmark_phase2_ab_gt20 import (  # noqa: E402
    GT_DIR,
    SEED,
    SOURCE_DIR,
    load_stored_vl_text,
)
from services.failure_taxonomy import classify_failure  # noqa: E402
from services.ocr.ocr_text_override import ocr_text_override  # noqa: E402
from services.recovery_policy import recovery_policy_for_result  # noqa: E402
from services.recovery_runner import (  # noqa: E402
    build_recovery_payload,
    build_targeted_payloads,
    phase2_recovery_enabled,
)
from services.sales_statement_extractor import (  # noqa: E402
    attach_phase1_architecture,
    extract_sales_statement_core,
)


OUT = ROOT / "phase4" / "phase2_ab_gt20"


def _part_summary(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    kinds = []
    text_chars = 0
    has_ocr_marker = False
    product_hits = 0
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("inline_data") or part.get("inlineData"):
            kinds.append("inline_data")
            continue
        text = str(part.get("text") or "")
        kinds.append("text")
        text_chars += len(text)
        if "Original OCR evidence" in text:
            has_ocr_marker = True
    return {
        "part_count": len(parts),
        "part_kinds": kinds,
        "text_chars": text_chars,
        "has_ocr_evidence_marker": has_ocr_marker,
    }


def trace_one(document_id: str) -> Dict[str, Any]:
    source = Path(SOURCE_DIR) / document_id
    ocr_text = load_stored_vl_text(document_id)
    row: Dict[str, Any] = {
        "document_id": document_id,
        "source_exists": source.is_file(),
        "stored_ocr_chars": len(ocr_text or ""),
    }
    if not source.is_file() or not ocr_text:
        row["error"] = "missing_source_or_ocr"
        return row

    file_bytes = source.read_bytes()
    os.environ["PHASE2_RECOVERY_ENABLED"] = "false"
    with ocr_text_override(ocr_text):
        result = extract_sales_statement_core(file_bytes, document_id)
    if not isinstance(result, dict):
        row["error"] = "extract_failed"
        return row
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    if isinstance(extra, dict):
        extra["di_ocr_override"] = True
        if "<table" in ocr_text.lower():
            extra.setdefault("ocr_table_html", ocr_text[:20000])
        else:
            extra.setdefault("ocr_text", ocr_text[:20000])
        extra.setdefault("ocr_source", "paddleocr-vl-stored")
    result = attach_phase1_architecture(result, file_bytes, document_id)

    phase1 = result.get("phase1") if isinstance(result.get("phase1"), dict) else {}
    validation = phase1.get("validation") if isinstance(phase1.get("validation"), dict) else {}
    route = phase1.get("route") if isinstance(phase1.get("route"), dict) else {}
    method = str(
        validation.get("parser")
        or ((result.get("totals") or {}).get("extra") or {}).get("extraction_method")
        or ""
    )
    accepted = validation.get("accepted") is True
    failure = None if accepted else classify_failure(validation, route=route, extraction_method=method)
    policy = recovery_policy_for_result(failure, result) if failure else None

    failing = []
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        extra_i = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if extra_i.get("stock_identity_ok") is False or extra_i.get("source_discrepancy"):
            failing.append(str(item.get("product_name") or ""))

    row.update(
        {
            "accepted": accepted,
            "validation_reasons": list(validation.get("reasons") or [])[:12],
            "extraction_method": method,
            "line_item_count": len(result.get("line_items") or []),
            "failing_row_count": len(failing),
            "failing_rows_sample": failing[:8],
            "failure_type": None if failure is None else failure.value,
            "policy_strategy": None if policy is None else policy.strategy,
            "policy_call_gemini": None if policy is None else policy.call_gemini,
            "policy_max_attempts": None if policy is None else policy.max_attempts,
        }
    )

    if accepted or failure is None:
        row["gemini_trigger"] = "not_needed"
        row["trigger_reason"] = "validation_accepted_or_unclassified"
        return row
    if not policy.call_gemini or policy.max_attempts < 1:
        row["gemini_trigger"] = "skipped"
        row["trigger_reason"] = f"policy_{policy.strategy}_call_gemini={policy.call_gemini}"
        return row

    payloads = build_targeted_payloads(
        result,
        strategy=policy.strategy,
        file_bytes=file_bytes,
        filename=document_id,
    )
    if not payloads:
        payloads = [
            build_recovery_payload(
                result,
                strategy=policy.strategy,
                file_bytes=file_bytes,
                filename=document_id,
            )
        ]
    payload = payloads[0]
    parts = ((payload.get("contents") or [{}])[0].get("parts") or [])
    gen = payload.get("generationConfig") or {}
    schema = gen.get("responseJsonSchema") or {}
    summary = _part_summary(parts if isinstance(parts, list) else [])
    # Count how many failing names appear in evidence text
    joined = "\n".join(
        str(p.get("text") or "") for p in parts if isinstance(p, dict) and "text" in p
    )
    names_in_evidence = [n for n in failing if n and n in joined]
    row.update(
        {
            "gemini_trigger": "would_call",
            "trigger_reason": f"failure={failure.value} strategy={policy.strategy}",
            "payload_count": len(payloads),
            "responseMimeType": gen.get("responseMimeType"),
            "schema_title": schema.get("title") if isinstance(schema, dict) else None,
            "schema_dynamic_additionalProperties": (
                ((schema.get("properties") or {}).get("dynamic") or {}).get(
                    "additionalProperties"
                )
                if isinstance(schema, dict)
                else None
            ),
            "evidence": summary,
            "failing_names_in_evidence": len(names_in_evidence),
            "failing_names_in_evidence_sample": names_in_evidence[:8],
        }
    )
    return row


def main() -> None:
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    rows = []
    for document_id in seed.get("document_ids") or []:
        print("trace", document_id, flush=True)
        rows.append(trace_one(document_id))

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "PHASE2_TRACE.json").write_text(
        json.dumps({"documents": rows, "phase2_default_env_check": phase2_recovery_enabled()}, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# Phase 2 GT-20 trigger trace",
        "",
        "Built from Phase-2-OFF extraction + recovery policy/payload inspection.",
        "Does not call Gemini.",
        "",
        "| document_id | accepted | failure | strategy | gemini | OCR evidence | failing in evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {document_id} | {accepted} | {failure_type} | {policy_strategy} | {gemini_trigger} | {ocr} | {hit} |".format(
                document_id=row.get("document_id"),
                accepted=row.get("accepted"),
                failure_type=row.get("failure_type"),
                policy_strategy=row.get("policy_strategy"),
                gemini_trigger=row.get("gemini_trigger"),
                ocr=(row.get("evidence") or {}).get("has_ocr_evidence_marker"),
                hit=row.get("failing_names_in_evidence"),
            )
        )
        lines.append("")
        lines.append(f"- reason: `{row.get('trigger_reason')}`")
        if row.get("validation_reasons"):
            lines.append(f"- validation: `{row.get('validation_reasons')}`")
    (OUT / "PHASE2_TRACE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote", OUT / "PHASE2_TRACE.md")


if __name__ == "__main__":
    main()
