"""Controlled canary: OCR_SECONDARY_REPAIR flag OFF vs ON on GT fixtures.

Does not run the 385-image batch. Writes evidence under
phase4/secondary_repair_canary/.
"""

from __future__ import annotations

import json
import os
import sys
import time
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ground_truth.html_table import parse_html_tables
from services.ground_truth.matching import match_field
from services.ground_truth.schema import only_source_verified_fields, validate_ground_truth_document
from services.ocr import secondary_repair as sr
from services.ocr.base import OCRResult
from services.ocr.orchestrator import OCRBundle

GT_DIR = ROOT / "phase4" / "ground_truth_50" / "per_document"
VL_DIR = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"
OUT_DIR = ROOT / "phase4" / "secondary_repair_canary"

# Minimal representative set covering required cases.
CANARY_DOCS = [
    {
        "document_id": "0000701078_2026_08_ZL_03_362_05092026080346.jpeg",
        "role": "himalaya_expiry_breakage",
        "expect_repair_types": {"himalaya_row_anchor_ocr"},
        "watch_fields": ["aact_close", "talekt_exp"],
    },
    {
        "document_id": "0000701151_2026_08_ZL_14_361_03092026025650.jpg",
        "role": "total_footer",
        "expect_repair_types": {"total_footer_ocr"},
        "watch_fields": ["tot_op", "tot_cl", "aact_cl"],
    },
    {
        "document_id": "0000700275_2026_08_ZL_18_374_04092026171615.pdf",
        "role": "liv52_closing",
        "expect_repair_types": {"liv_printed_closing_ocr"},
        "watch_fields": ["liv100_cl"],
    },
    {
        "document_id": "0000700084_2026_08_ZL_06_8236_03092026155136.pdf",
        "role": "ordinary_compatible",
        "expect_repair_types": set(),
        "watch_fields": [],
    },
    {
        "document_id": "0000700209_2026_08_ZL_18_374_04092026060313.pdf",
        "role": "negative_blank_unresolved",
        "expect_repair_types": set(),
        "watch_fields": ["aact_op", "aact_cl"],
        "must_remain_missing": ["aact_op", "aact_cl"],
    },
]


def _load_vl(doc: str) -> Dict[str, Any]:
    payload = json.loads((VL_DIR / f"{doc}.json").read_text(encoding="utf-8"))
    return payload["pages"][0]


def _load_gt_fields(doc: str) -> List[Dict[str, Any]]:
    gt = json.loads((GT_DIR / f"{doc}.json").read_text(encoding="utf-8"))
    ok, errors = validate_ground_truth_document(gt)
    if not ok:
        raise RuntimeError(f"invalid GT {doc}: {errors}")
    return only_source_verified_fields(gt)


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
                "match_mode": m.get("match_mode"),
            }
        )
    return out


def _preflight_flag() -> Dict[str, Any]:
    results = {}
    # Unset
    old = os.environ.pop("OCR_SECONDARY_REPAIR", None)
    try:
        results["unset_enabled"] = sr.secondary_repair_enabled()
        html = "<table><tr><td>X</td><td>1</td></tr></table>"
        text, _, report = sr.maybe_apply_secondary_repairs("preflight.pdf", html)
        results["unset_noop"] = text == html and report.unresolved[0]["reason"] == (
            "secondary_repair_disabled"
        )
        # Explicit false
        os.environ["OCR_SECONDARY_REPAIR"] = "false"
        results["false_enabled"] = sr.secondary_repair_enabled()
        # Accidental values
        for val, expect in [("0", False), ("no", False), ("TRUE", True), ("on", True)]:
            os.environ["OCR_SECONDARY_REPAIR"] = val
            results[f"value_{val!r}"] = sr.secondary_repair_enabled() is expect
        # Pipeline helper with flag off must not mutate text
        os.environ["OCR_SECONDARY_REPAIR"] = "false"
        from services.document_intelligence.pipeline import _maybe_secondary_repair_bundle

        bundle = OCRBundle(
            primary=OCRResult(text=html, engine="canary", metadata={}),
            metadata={},
        )
        _maybe_secondary_repair_bundle(
            bundle, document_id="preflight.pdf", image_bytes=b""
        )
        results["pipeline_off_unchanged"] = bundle.primary.text == html
        results["pipeline_off_meta_disabled"] = (
            (bundle.metadata.get("secondary_repair") or {}).get("unresolved")
            or [{}]
        )[0].get("reason") == "secondary_repair_disabled" or (
            (bundle.primary.metadata.get("secondary_repair") or {}).get("unresolved")
            or [{}]
        )[0].get("reason") == "secondary_repair_disabled"
    finally:
        if old is None:
            os.environ.pop("OCR_SECONDARY_REPAIR", None)
        else:
            os.environ["OCR_SECONDARY_REPAIR"] = old
    results["pass"] = all(
        v is True
        for k, v in results.items()
        if k
        in {
            "unset_noop",
            "pipeline_off_unchanged",
            "value_'0'",
            "value_'no'",
            "value_'TRUE'",
            "value_'on'",
        }
    ) and results.get("unset_enabled") is False and results.get("false_enabled") is False
    return results


def _run_mode(mode: str, doc: str, vl_text: str, image_bytes: Optional[bytes], fields, tokens):
    if mode == "off":
        os.environ["OCR_SECONDARY_REPAIR"] = "false"
    else:
        os.environ["OCR_SECONDARY_REPAIR"] = "true"
    t0 = time.perf_counter()
    # Production entrypoint (flag-gated).
    text, cells, report = sr.maybe_apply_secondary_repairs(
        doc,
        vl_text,
        image_bytes=image_bytes,
    )
    # Also exercise DI helper mutation path with a copy of VL text.
    from services.document_intelligence.pipeline import _maybe_secondary_repair_bundle

    bundle = OCRBundle(
        primary=OCRResult(text=vl_text, engine="canary", metadata={}),
        metadata={},
    )
    _maybe_secondary_repair_bundle(bundle, document_id=doc, image_bytes=image_bytes or b"")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    matches = _match_all(fields, text, cells, tokens)
    return {
        "mode": mode,
        "elapsed_ms": round(elapsed_ms, 1),
        "text_len": len(text or ""),
        "text_sha_prefix": __import__("hashlib").sha256((text or "").encode("utf-8")).hexdigest()[:16],
        "repair_report": report.to_dict(),
        "pipeline_bundle_text_sha_prefix": __import__("hashlib")
        .sha256((bundle.primary.text or "").encode("utf-8"))
        .hexdigest()[:16],
        "pipeline_bundle_matches_entrypoint": (bundle.primary.text or "") == (text or ""),
        "matches": matches,
        "correct_count": sum(1 for m in matches if m["correct"]),
        "field_count": len(matches),
        "silent_wrong_count": sum(1 for m in matches if m["silent_wrong"]),
        "missing_count": sum(1 for m in matches if m.get("error_type") == "MISSING"),
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    preflight = _preflight_flag()
    (OUT_DIR / "preflight.json").write_text(
        json.dumps(preflight, indent=2), encoding="utf-8"
    )
    if not preflight.get("pass"):
        print("PREFLIGHT FAIL", json.dumps(preflight, indent=2))
        _write_report(
            decision="CANARY BLOCKED — FIX REQUIRED",
            preflight=preflight,
            docs=[],
            stop_reasons=["preflight_flag_checks_failed"],
        )
        return 2

    # Ensure we start from OFF and restore OFF at end.
    prior = os.environ.get("OCR_SECONDARY_REPAIR")
    os.environ["OCR_SECONDARY_REPAIR"] = "false"

    doc_results = []
    stop_reasons: List[str] = []
    blocked = False

    try:
        for spec in CANARY_DOCS:
            doc = spec["document_id"]
            role = spec["role"]
            print("=" * 60, role, doc[:40])
            path = sr.resolve_source_path(doc)
            if path is None or not path.is_file():
                stop_reasons.append(f"missing_source:{doc}")
                blocked = True
                break
            vl = _load_vl(doc)
            if vl.get("status") != "success":
                stop_reasons.append(f"vl_unavailable:{doc}")
                blocked = True
                break
            vl_text = vl.get("text") or ""
            tokens = vl.get("numeric_tokens") or []
            fields = _load_gt_fields(doc)

            # Load raster bytes the same way production secondary path accepts.
            if path.suffix.lower() == ".pdf":
                image = sr.load_source_image(path, scale=2.5)
                buf = __import__("io").BytesIO()
                image.save(buf, format="PNG")
                image_bytes = buf.getvalue()
            else:
                image_bytes = path.read_bytes()

            off = _run_mode("off", doc, vl_text, image_bytes, fields, tokens)
            on = _run_mode("on", doc, vl_text, image_bytes, fields, tokens)

            # Force flag back off between docs.
            os.environ["OCR_SECONDARY_REPAIR"] = "false"

            off_by_id = {m["field_id"]: m for m in off["matches"]}
            on_by_id = {m["field_id"]: m for m in on["matches"]}
            deltas = []
            for fid in sorted(set(off_by_id) | set(on_by_id)):
                a, b = off_by_id.get(fid), on_by_id.get(fid)
                if not a or not b:
                    continue
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

            repair_types = {
                r.get("type")
                for r in (on["repair_report"].get("repairs") or [])
                if r.get("corrected")
            }
            unexpected_repairs = repair_types - set(spec.get("expect_repair_types") or set())
            missing_expected = set(spec.get("expect_repair_types") or set()) - repair_types

            # Stop conditions
            if on["silent_wrong_count"] > 0:
                stop_reasons.append(f"silent_wrong:{doc}")
                blocked = True
            if any(d["off_correct"] and not d["on_correct"] for d in deltas):
                stop_reasons.append(f"regression_correct_to_wrong:{doc}")
                blocked = True
            for fid in spec.get("must_remain_missing") or []:
                m = on_by_id.get(fid)
                if m and m.get("predicted_raw") not in (None, ""):
                    stop_reasons.append(f"unresolved_silently_populated:{doc}:{fid}")
                    blocked = True
            if unexpected_repairs and role in {
                "ordinary_compatible",
                "negative_blank_unresolved",
            }:
                stop_reasons.append(
                    f"incompatible_or_ordinary_modified:{doc}:{sorted(unexpected_repairs)}"
                )
                blocked = True
            # Unsupported repair: ON changed a field to wrong value
            for d in deltas:
                if d["on_pred"] not in (None, "") and not d["on_correct"]:
                    stop_reasons.append(
                        f"unsupported_or_wrong_repair:{doc}:{d['field_id']}"
                    )
                    blocked = True

            row = {
                "document_id": doc,
                "role": role,
                "source_path": str(path),
                "off": {
                    k: off[k]
                    for k in (
                        "elapsed_ms",
                        "text_sha_prefix",
                        "correct_count",
                        "field_count",
                        "silent_wrong_count",
                        "missing_count",
                        "repair_report",
                        "pipeline_bundle_matches_entrypoint",
                    )
                },
                "on": {
                    k: on[k]
                    for k in (
                        "elapsed_ms",
                        "text_sha_prefix",
                        "correct_count",
                        "field_count",
                        "silent_wrong_count",
                        "missing_count",
                        "repair_report",
                        "pipeline_bundle_matches_entrypoint",
                    )
                },
                "deltas": deltas,
                "repair_types_on": sorted(repair_types),
                "unexpected_repairs": sorted(unexpected_repairs),
                "missing_expected_repairs": sorted(missing_expected),
                "watch_field_detail": {
                    fid: {
                        "off": off_by_id.get(fid),
                        "on": on_by_id.get(fid),
                    }
                    for fid in (spec.get("watch_fields") or [])
                },
                "off_full_matches": off["matches"],
                "on_full_matches": on["matches"],
            }
            doc_results.append(row)
            (OUT_DIR / f"{Path(doc).stem}.json").write_text(
                json.dumps(row, indent=2), encoding="utf-8"
            )
            print(
                f"  off {off['correct_count']}/{off['field_count']} "
                f"on {on['correct_count']}/{on['field_count']} "
                f"deltas={len(deltas)} repairs={sorted(repair_types)} "
                f"ms_off={off['elapsed_ms']} ms_on={on['elapsed_ms']}"
            )
            if blocked:
                print("STOP", stop_reasons)
                break
    finally:
        # Hard restore: leave environment disabled.
        if prior is None:
            os.environ.pop("OCR_SECONDARY_REPAIR", None)
        else:
            os.environ["OCR_SECONDARY_REPAIR"] = prior
        # Prefer explicit false if was unset, to avoid accidental ON in shell.
        if prior is None:
            os.environ.pop("OCR_SECONDARY_REPAIR", None)

    if blocked:
        decision = "CANARY BLOCKED — FIX REQUIRED"
    elif not doc_results:
        decision = "CANARY INCONCLUSIVE — MORE EVIDENCE REQUIRED"
    else:
        # Require expected repairs actually fired for the three positive cases.
        roles_ok = True
        for row in doc_results:
            if row["role"] == "himalaya_expiry_breakage" and "himalaya_row_anchor_ocr" not in row[
                "repair_types_on"
            ]:
                roles_ok = False
            if row["role"] == "total_footer" and "total_footer_ocr" not in row["repair_types_on"]:
                roles_ok = False
            if row["role"] == "liv52_closing" and "liv_printed_closing_ocr" not in row[
                "repair_types_on"
            ]:
                roles_ok = False
            if row["role"] == "ordinary_compatible" and row["repair_types_on"]:
                roles_ok = False
            if row["role"] == "negative_blank_unresolved":
                for fid in ("aact_op", "aact_cl"):
                    pred = (row["watch_field_detail"].get(fid) or {}).get("on") or {}
                    if pred.get("predicted_raw") not in (None, ""):
                        roles_ok = False
        decision = (
            "CANARY PASS — READY FOR EXPANDED CONTROLLED ROLLOUT"
            if roles_ok and not stop_reasons
            else "CANARY INCONCLUSIVE — MORE EVIDENCE REQUIRED"
        )

    _write_report(
        decision=decision,
        preflight=preflight,
        docs=doc_results,
        stop_reasons=stop_reasons,
    )
    print("DECISION:", decision)
    return 0 if decision.startswith("CANARY PASS") else 1


def _write_report(*, decision, preflight, docs, stop_reasons):
    summary = {
        "decision": decision,
        "preflight": preflight,
        "stop_reasons": stop_reasons,
        "document_ids": [d.get("document_id") for d in docs],
        "documents": [
            {
                "document_id": d["document_id"],
                "role": d["role"],
                "off_correct": f"{d['off']['correct_count']}/{d['off']['field_count']}",
                "on_correct": f"{d['on']['correct_count']}/{d['on']['field_count']}",
                "deltas": d["deltas"],
                "repair_types_on": d["repair_types_on"],
                "watch_field_detail": d["watch_field_detail"],
                "elapsed_ms_off": d["off"]["elapsed_ms"],
                "elapsed_ms_on": d["on"]["elapsed_ms"],
            }
            for d in docs
        ],
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    lines = [
        "# Secondary OCR repair canary report",
        "",
        f"**Decision:** `{decision}`",
        "",
        "## Preflight",
        "",
        "```json",
        json.dumps(preflight, indent=2),
        "```",
        "",
        "## Document list",
        "",
    ]
    for d in docs:
        lines.append(
            f"- `{d['document_id']}` — role `{d['role']}` — "
            f"OFF {d['off']['correct_count']}/{d['off']['field_count']} → "
            f"ON {d['on']['correct_count']}/{d['on']['field_count']}"
        )
    lines += ["", "## Stop reasons", ""]
    if stop_reasons:
        for s in stop_reasons:
            lines.append(f"- `{s}`")
    else:
        lines.append("- (none)")
    lines += ["", "## Per-document findings", ""]
    for d in docs:
        lines.append(f"### `{d['document_id']}` ({d['role']})")
        lines.append("")
        lines.append(
            f"- Latency: OFF {d['off']['elapsed_ms']} ms, ON {d['on']['elapsed_ms']} ms"
        )
        lines.append(f"- Repair types ON: `{d['repair_types_on']}`")
        lines.append(
            f"- Pipeline helper text matched entrypoint: "
            f"`{d['on']['pipeline_bundle_matches_entrypoint']}`"
        )
        if d["deltas"]:
            lines.append("- Field deltas (OFF → ON):")
            for delta in d["deltas"]:
                lines.append(
                    f"  - `{delta['field_id']}`: "
                    f"`{delta['off_pred']}` → `{delta['on_pred']}` "
                    f"(correct {delta['off_correct']} → {delta['on_correct']}; "
                    f"expected `{delta['expected']}`)"
                )
        else:
            lines.append("- Field deltas: none")
        if d.get("watch_field_detail"):
            lines.append("- Watch fields:")
            for fid, pair in d["watch_field_detail"].items():
                lines.append(
                    f"  - `{fid}`: OFF `{pair.get('off')}` / ON `{pair.get('on')}`"
                )
        lines.append("")
    lines += [
        "## Claims",
        "",
        "- This canary does **not** claim 100% accuracy.",
        "- This canary does **not** score the 385-image `prerana` batch.",
        "- Flag left **disabled** after the run (environment restored).",
        "",
        f"Artifacts: `{OUT_DIR.as_posix()}`",
        "",
    ]
    (OUT_DIR / "CANARY_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
