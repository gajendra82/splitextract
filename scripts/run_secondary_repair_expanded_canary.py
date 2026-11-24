"""Expanded controlled canary: OCR_SECONDARY_REPAIR OFF vs ON on all GT-20 docs.

Baseline: phase4/secondary_repair_canary/CANARY_REPORT.md
Outputs:  phase4/secondary_repair_canary/expanded/
Does not run the 385-image batch. Restores flag OFF after run.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ground_truth.matching import match_field
from services.ground_truth.schema import only_source_verified_fields, validate_ground_truth_document
from services.ocr import secondary_repair as sr
from services.ocr.base import OCRResult
from services.ocr.orchestrator import OCRBundle

GT_DIR = ROOT / "phase4" / "ground_truth_50" / "per_document"
VL_DIR = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"
OUT_DIR = ROOT / "phase4" / "secondary_repair_canary" / "expanded"
BASELINE = ROOT / "phase4" / "secondary_repair_canary" / "CANARY_REPORT.md"

# Docs where corrected secondary repairs are expected (from first canary).
EXPECTED_REPAIR_DOCS = {
    "0000701078_2026_08_ZL_03_362_05092026080346.jpeg": {
        "himalaya_row_anchor_ocr"
    },
    "0000701151_2026_08_ZL_14_361_03092026025650.jpg": {"total_footer_ocr"},
    "0000700275_2026_08_ZL_18_374_04092026171615.pdf": {
        "liv_printed_closing_ocr"
    },
}

# Fields that must stay unresolved/missing under ON.
MUST_REMAIN_MISSING = {
    "0000700209_2026_08_ZL_18_374_04092026060313.pdf": ["aact_op", "aact_cl"],
    "0000700124_2026_08_ZL_06_521_02092026204703.pdf": ["h_op"],
}


def _role_for(doc: str, text: str) -> str:
    if doc in EXPECTED_REPAIR_DOCS:
        if "himalaya" in str(EXPECTED_REPAIR_DOCS[doc]):
            return "repair_himalaya"
        if "total_footer" in str(EXPECTED_REPAIR_DOCS[doc]):
            return "repair_total_footer"
        if "liv" in str(EXPECTED_REPAIR_DOCS[doc]):
            return "repair_liv"
    if doc in MUST_REMAIN_MISSING:
        return "negative_unresolved"
    tags = []
    if "Expiry Breakage" in text or ("Expiry" in text and "Break" in text):
        tags.append("expiry_breakage")
    if "Op.Bal" in text:
        tags.append("himalaya_qty")
    if "Opening" in text and ("Issues" in text or "Closing" in text):
        tags.append("ssa_like")
    if not tags:
        return "ordinary"
    return "compatible_" + "+".join(tags)


def _load_vl(doc: str) -> Dict[str, Any]:
    return json.loads((VL_DIR / f"{doc}.json").read_text(encoding="utf-8"))["pages"][0]


def _load_fields(doc: str) -> List[Dict[str, Any]]:
    gt = json.loads((GT_DIR / f"{doc}.json").read_text(encoding="utf-8"))
    ok, errors = validate_ground_truth_document(gt)
    if not ok:
        raise RuntimeError(f"invalid GT {doc}: {errors}")
    return only_source_verified_fields(gt)


def _image_bytes(path: Path) -> bytes:
    if path.suffix.lower() == ".pdf":
        image = sr.load_source_image(path, scale=2.5)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    return path.read_bytes()


def _match_all(fields, text, cells, tokens):
    out = []
    for f in fields:
        m = match_field(f, ocr_text=text, table_cells=cells, numeric_tokens=tokens)
        out.append(
            {
                "field_id": f.get("field_id"),
                "expected_raw": f.get("expected_raw"),
                "predicted_raw": m.get("predicted_raw"),
                "correct": bool(m.get("correct")),
                "error_type": m.get("error_type"),
                "silent_wrong": bool(m.get("silent_wrong")),
            }
        )
    return out


def _run_mode(mode: str, doc: str, vl_text: str, image_bytes: bytes, fields, tokens):
    os.environ["OCR_SECONDARY_REPAIR"] = "true" if mode == "on" else "false"
    t0 = time.perf_counter()
    text, cells, report = sr.maybe_apply_secondary_repairs(
        doc, vl_text, image_bytes=image_bytes
    )
    secondary_ms = (time.perf_counter() - t0) * 1000.0

    # DI helper path (mutation) — timed separately, not double-counted as primary.
    from services.document_intelligence.pipeline import _maybe_secondary_repair_bundle

    t1 = time.perf_counter()
    bundle = OCRBundle(
        primary=OCRResult(text=vl_text, engine="expanded_canary", metadata={}),
        metadata={},
    )
    _maybe_secondary_repair_bundle(bundle, document_id=doc, image_bytes=image_bytes)
    pipeline_helper_ms = (time.perf_counter() - t1) * 1000.0

    matches = _match_all(fields, text, cells, tokens)
    return {
        "mode": mode,
        "secondary_ocr_ms": round(secondary_ms, 1),
        "pipeline_helper_ms": round(pipeline_helper_ms, 1),
        "text_sha": hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16],
        "pipeline_text_matches": (bundle.primary.text or "") == (text or ""),
        "repair_report": report.to_dict(),
        "matches": matches,
        "correct_count": sum(1 for m in matches if m["correct"]),
        "field_count": len(matches),
        "silent_wrong_count": sum(1 for m in matches if m["silent_wrong"]),
        "missing_count": sum(1 for m in matches if m.get("error_type") == "MISSING"),
    }


def _preflight() -> Dict[str, Any]:
    old = os.environ.pop("OCR_SECONDARY_REPAIR", None)
    try:
        unset = sr.secondary_repair_enabled()
        os.environ["OCR_SECONDARY_REPAIR"] = "false"
        false_on = sr.secondary_repair_enabled()
        html = "<table><tr><td>A</td><td>1</td></tr></table>"
        text, _, report = sr.maybe_apply_secondary_repairs("x.pdf", html)
        noop = text == html and report.unresolved[0]["reason"] == "secondary_repair_disabled"
        return {
            "unset_enabled": unset,
            "false_enabled": false_on,
            "noop_when_off": noop,
            "baseline_report_exists": BASELINE.is_file(),
            "pass": (unset is False and false_on is False and noop and BASELINE.is_file()),
        }
    finally:
        if old is None:
            os.environ.pop("OCR_SECONDARY_REPAIR", None)
        else:
            os.environ["OCR_SECONDARY_REPAIR"] = old


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prior = os.environ.get("OCR_SECONDARY_REPAIR")
    preflight = _preflight()
    (OUT_DIR / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
    if not preflight["pass"]:
        _finalize(
            decision="BLOCKED",
            preflight=preflight,
            docs=[],
            stop_reasons=["preflight_failed"],
            totals={},
        )
        return 2

    gt_docs = sorted(
        p.name[: -len(".json")]
        for p in GT_DIR.glob("*.json")
        if only_source_verified_fields(json.loads(p.read_text(encoding="utf-8")))
    )
    # Keep only docs with VL success + source file.
    selected = []
    for doc in gt_docs:
        if not (VL_DIR / f"{doc}.json").is_file():
            continue
        if sr.resolve_source_path(doc) is None:
            continue
        selected.append(doc)

    (OUT_DIR / "document_list.json").write_text(
        json.dumps({"count": len(selected), "document_ids": selected}, indent=2),
        encoding="utf-8",
    )

    docs_out: List[Dict[str, Any]] = []
    stop_reasons: List[str] = []
    blocked = False
    totals = {
        "fields_off_correct": 0,
        "fields_on_correct": 0,
        "fields_total": 0,
        "verified_corrections": 0,
        "incorrect_repairs": 0,
        "regressions": 0,
        "unchanged_fields": 0,
        "unresolved_missing_on": 0,
        "docs_with_repairs": 0,
        "secondary_ocr_ms_sum_on": 0.0,
        "secondary_ocr_ms_sum_off": 0.0,
    }

    try:
        os.environ["OCR_SECONDARY_REPAIR"] = "false"
        for doc in selected:
            print("=" * 60, doc[:48])
            path = sr.resolve_source_path(doc)
            assert path is not None
            vl = _load_vl(doc)
            if vl.get("status") != "success":
                stop_reasons.append(f"vl_unavailable:{doc}")
                blocked = True
                break
            vl_text = vl.get("text") or ""
            tokens = vl.get("numeric_tokens") or []
            fields = _load_fields(doc)
            role = _role_for(doc, vl_text)
            image_bytes = _image_bytes(path)

            off = _run_mode("off", doc, vl_text, image_bytes, fields, tokens)
            on = _run_mode("on", doc, vl_text, image_bytes, fields, tokens)
            os.environ["OCR_SECONDARY_REPAIR"] = "false"

            off_m = {m["field_id"]: m for m in off["matches"]}
            on_m = {m["field_id"]: m for m in on["matches"]}
            deltas = []
            for fid in sorted(set(off_m) | set(on_m)):
                a, b = off_m[fid], on_m[fid]
                if a["predicted_raw"] != b["predicted_raw"] or a["correct"] != b["correct"]:
                    deltas.append(
                        {
                            "field_id": fid,
                            "expected": a["expected_raw"],
                            "off_pred": a["predicted_raw"],
                            "on_pred": b["predicted_raw"],
                            "off_correct": a["correct"],
                            "on_correct": b["correct"],
                            "off_error": a["error_type"],
                            "on_error": b["error_type"],
                        }
                    )
                else:
                    totals["unchanged_fields"] += 1

            repair_types = {
                r.get("type")
                for r in (on["repair_report"].get("repairs") or [])
                if r.get("corrected")
            }
            if repair_types:
                totals["docs_with_repairs"] += 1

            for d in deltas:
                if (not d["off_correct"]) and d["on_correct"]:
                    totals["verified_corrections"] += 1
                if d["off_correct"] and not d["on_correct"]:
                    totals["regressions"] += 1
                    stop_reasons.append(f"regression:{doc}:{d['field_id']}")
                    blocked = True
                if d["on_pred"] not in (None, "") and not d["on_correct"]:
                    # Changed/populated to a wrong value
                    if d["off_pred"] != d["on_pred"]:
                        totals["incorrect_repairs"] += 1
                        stop_reasons.append(
                            f"incorrect_repair:{doc}:{d['field_id']}"
                        )
                        blocked = True

            if on["silent_wrong_count"] > 0:
                # Silent wrong remaining after ON is not an automatic block if
                # unchanged from OFF; block only if ON introduced new silent wrongs.
                for fid, b in on_m.items():
                    a = off_m[fid]
                    if b["silent_wrong"] and not a["silent_wrong"]:
                        stop_reasons.append(f"new_silent_wrong:{doc}:{fid}")
                        blocked = True

            for fid in MUST_REMAIN_MISSING.get(doc, []):
                pred = (on_m.get(fid) or {}).get("predicted_raw")
                if pred not in (None, ""):
                    stop_reasons.append(f"unresolved_populated:{doc}:{fid}")
                    blocked = True

            expected_types = EXPECTED_REPAIR_DOCS.get(doc)
            if expected_types is not None:
                if not expected_types.issubset(repair_types):
                    stop_reasons.append(
                        f"missing_expected_repair:{doc}:{sorted(expected_types - repair_types)}"
                    )
                    blocked = True
            elif repair_types:
                # Any corrected repair on a non-target doc must only change fields
                # that become correct; already covered by incorrect_repair/regression.
                # Still flag unexpected repair activity for review (not auto-block
                # if all deltas are verified corrections).
                pass

            totals["fields_off_correct"] += off["correct_count"]
            totals["fields_on_correct"] += on["correct_count"]
            totals["fields_total"] += on["field_count"]
            totals["unresolved_missing_on"] += on["missing_count"]
            totals["secondary_ocr_ms_sum_on"] += on["secondary_ocr_ms"]
            totals["secondary_ocr_ms_sum_off"] += off["secondary_ocr_ms"]

            row = {
                "document_id": doc,
                "role": role,
                "source_path": str(path),
                "off": {
                    "correct_count": off["correct_count"],
                    "field_count": off["field_count"],
                    "silent_wrong_count": off["silent_wrong_count"],
                    "missing_count": off["missing_count"],
                    "secondary_ocr_ms": off["secondary_ocr_ms"],
                    "pipeline_helper_ms": off["pipeline_helper_ms"],
                    "text_sha": off["text_sha"],
                    "repair_report": off["repair_report"],
                },
                "on": {
                    "correct_count": on["correct_count"],
                    "field_count": on["field_count"],
                    "silent_wrong_count": on["silent_wrong_count"],
                    "missing_count": on["missing_count"],
                    "secondary_ocr_ms": on["secondary_ocr_ms"],
                    "pipeline_helper_ms": on["pipeline_helper_ms"],
                    "text_sha": on["text_sha"],
                    "repair_report": on["repair_report"],
                    "pipeline_text_matches": on["pipeline_text_matches"],
                },
                "deltas": deltas,
                "repair_types_on": sorted(repair_types),
                "matches_off": off["matches"],
                "matches_on": on["matches"],
            }
            docs_out.append(row)
            (OUT_DIR / "per_document" ).mkdir(exist_ok=True)
            (OUT_DIR / "per_document" / f"{Path(doc).stem}.json").write_text(
                json.dumps(row, indent=2), encoding="utf-8"
            )
            print(
                f"  {role} OFF {off['correct_count']}/{off['field_count']} "
                f"ON {on['correct_count']}/{on['field_count']} "
                f"deltas={len(deltas)} repairs={sorted(repair_types)} "
                f"sec_ms_off={off['secondary_ocr_ms']} sec_ms_on={on['secondary_ocr_ms']}"
            )
            if blocked:
                print("STOP", stop_reasons[-3:])
                break
    finally:
        if prior is None:
            os.environ.pop("OCR_SECONDARY_REPAIR", None)
        else:
            os.environ["OCR_SECONDARY_REPAIR"] = prior

    # Confirm flag off
    os.environ.pop("OCR_SECONDARY_REPAIR", None)
    flag_off = sr.secondary_repair_enabled() is False

    if blocked or stop_reasons:
        decision = "BLOCKED"
    elif len(docs_out) < 10:
        decision = "INCONCLUSIVE"
        stop_reasons.append(f"too_few_docs:{len(docs_out)}")
    elif not flag_off:
        decision = "BLOCKED"
        stop_reasons.append("flag_not_restored_off")
    else:
        # Require the three known repair docs present and corrected.
        seen_roles = {d["role"] for d in docs_out}
        needed = {"repair_himalaya", "repair_total_footer", "repair_liv", "ordinary", "negative_unresolved"}
        # roles use repair_* for expected docs
        ok_repairs = all(
            any(d["document_id"] == doc and d["on"]["correct_count"] > d["off"]["correct_count"] for d in docs_out)
            for doc in EXPECTED_REPAIR_DOCS
        )
        if not ok_repairs:
            decision = "INCONCLUSIVE"
            stop_reasons.append("expected_repair_docs_not_improved")
        else:
            decision = "EXPANDED CANARY PASS"

    totals["docs_evaluated"] = len(docs_out)
    totals["flag_restored_off"] = flag_off
    _finalize(
        decision=decision,
        preflight=preflight,
        docs=docs_out,
        stop_reasons=stop_reasons,
        totals=totals,
    )
    print("DECISION:", decision)
    print("TOTALS:", json.dumps(totals))
    return 0 if decision == "EXPANDED CANARY PASS" else 1


def _finalize(*, decision, preflight, docs, stop_reasons, totals):
    summary = {
        "decision": decision,
        "baseline": str(BASELINE),
        "preflight": preflight,
        "stop_reasons": stop_reasons,
        "totals": totals,
        "document_ids": [d["document_id"] for d in docs],
        "documents": [
            {
                "document_id": d["document_id"],
                "role": d["role"],
                "off_correct": f"{d['off']['correct_count']}/{d['off']['field_count']}",
                "on_correct": f"{d['on']['correct_count']}/{d['on']['field_count']}",
                "deltas": d["deltas"],
                "repair_types_on": d["repair_types_on"],
                "secondary_ocr_ms_off": d["off"]["secondary_ocr_ms"],
                "secondary_ocr_ms_on": d["on"]["secondary_ocr_ms"],
                "pipeline_helper_ms_on": d["on"]["pipeline_helper_ms"],
            }
            for d in docs
        ],
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [
        "# Expanded secondary OCR repair canary",
        "",
        f"**Decision:** `{decision}`",
        "",
        f"Baseline: `{BASELINE.as_posix()}`",
        "",
        "## Preflight",
        "",
        "```json",
        json.dumps(preflight, indent=2),
        "```",
        "",
        "## Scope",
        "",
        f"- Source-verified GT documents evaluated: **{len(docs)}**",
        "- Not the 385-image batch; not a general production accuracy claim.",
        "- Flag default remains OFF; environment restored OFF after the run.",
        "",
        "## Totals",
        "",
        "```json",
        json.dumps(totals, indent=2),
        "```",
        "",
        "## Stop reasons",
        "",
    ]
    if stop_reasons:
        lines.extend(f"- `{s}`" for s in stop_reasons)
    else:
        lines.append("- (none)")
    lines += ["", "## Per-document", ""]
    for d in docs:
        lines.append(f"### `{d['document_id']}` ({d['role']})")
        lines.append("")
        lines.append(
            f"- Fields: OFF {d['off']['correct_count']}/{d['off']['field_count']} → "
            f"ON {d['on']['correct_count']}/{d['on']['field_count']}"
        )
        lines.append(
            f"- Secondary OCR latency: OFF {d['off']['secondary_ocr_ms']} ms, "
            f"ON {d['on']['secondary_ocr_ms']} ms "
            f"(DI helper ON {d['on']['pipeline_helper_ms']} ms, separate)"
        )
        lines.append(f"- Repair types ON: `{d['repair_types_on']}`")
        if d["deltas"]:
            lines.append("- Deltas:")
            for delta in d["deltas"]:
                lines.append(
                    f"  - `{delta['field_id']}`: `{delta['off_pred']}` → `{delta['on_pred']}` "
                    f"(correct {delta['off_correct']}→{delta['on_correct']}; "
                    f"expected `{delta['expected']}`)"
                )
        else:
            lines.append("- Deltas: none")
        lines.append("")
    lines += [
        "## Commands",
        "",
        "```text",
        "python scripts/run_secondary_repair_expanded_canary.py",
        "```",
        "",
        f"Artifacts: `{OUT_DIR.as_posix()}`",
        "",
    ]
    (OUT_DIR / "EXPANDED_CANARY_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
