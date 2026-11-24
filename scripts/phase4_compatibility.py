"""Stage A — Phase 4 compatibility gate.

Compares old production path (core + phase1) vs DocumentIntelligencePipeline
on the existing Phase 3 representative/replay manifests. No Gemini, no
Phase 2, no OCR ensemble.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phase3.compare import (
    baseline_accepted,
    canonical_signature,
    dynamic_signature,
    iter_line_items,
    null_became_zero,
)

DEFAULT_DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
OUTPUT = ROOT / "phase4"


def _configure_env() -> None:
    # Stage A must not call Gemini, Phase 2, or the Gemini OCR fallback.
    os.environ["PHASE2_RECOVERY_ENABLED"] = "false"
    os.environ["OCR_ENGINE"] = "paddleocr-vl"
    os.environ["OCR_FALLBACK_ENGINE"] = "off"
    os.environ["ENABLE_GEMINI_EXTRACTION_FALLBACK"] = "false"
    os.environ["ENABLE_VISION_FALLBACK"] = "false"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _disable_gemini() -> None:
    """Hard-block Vertex/Gemini calls used by parsers and fallback."""

    def _blocked(*args, **kwargs):
        raise RuntimeError("GEMINI_DISABLED_FOR_PHASE4_STAGE_A")

    try:
        import services.sales_extraction_runtime as runtime

        runtime.sales_generate_content_via_vertex = _blocked  # type: ignore
    except Exception:
        pass
    try:
        import services.vertex_gemini_client as vertex

        vertex.generate_content_via_vertex = _blocked  # type: ignore
    except Exception:
        pass
    try:
        import services.gemini_extraction_fallback as gef

        gef.maybe_apply_gemini_fallback = lambda result, *a, **k: result  # type: ignore
        gef._enabled = lambda: False  # type: ignore
    except Exception:
        pass


def _install_core_cache() -> None:
    """Share one core extraction between old-path and Phase4 for the same bytes.

    Proves the orchestration wrapper does not mutate business output without
    paying double OCR cost.
    """
    import copy
    import hashlib

    import services.sales_statement_extractor as sse

    original = sse.extract_sales_statement_core
    cache: Dict[str, Dict[str, Any]] = {}

    def cached(file_bytes: bytes, filename: str) -> Dict[str, Any]:
        key = hashlib.sha1(file_bytes).hexdigest() + "|" + (filename or "")
        if key not in cache:
            cache[key] = original(file_bytes, filename)
        return copy.deepcopy(cache[key])

    sse.extract_sales_statement_core = cached  # type: ignore


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _meta(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    phase1 = result.get("phase1") if isinstance(result.get("phase1"), dict) else {}
    validation = phase1.get("validation") if isinstance(phase1.get("validation"), dict) else {}
    return {
        "parser": extra.get("extraction_method") or extra.get("parser"),
        "extraction_method": extra.get("extraction_method"),
        "layout": extra.get("layout") or extra.get("layout_id") or "",
        "profile": extra.get("profile")
        or (result.get("stock_identity") or {}).get("profile")
        or "",
        "row_count": len(iter_line_items(result)),
        "validation_accepted": validation.get("accepted") is True,
        "primary_failure": validation.get("primary_failure")
        or validation.get("failure_type"),
        "totals": {
            "sales_value": totals.get("sales_value"),
            "closing_value": totals.get("closing_value"),
            "opening_value": totals.get("opening_value"),
        },
    }


def _strip_phase4(result: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(result)
    out.pop("document_intelligence", None)
    out.pop("phase4", None)
    return out


def _business_equal(old: Dict[str, Any], new: Dict[str, Any]) -> Tuple[bool, List[str]]:
    flags: List[str] = []
    if canonical_signature(old) != canonical_signature(new):
        flags.append("canonical_values_changed")
    if dynamic_signature(old) != dynamic_signature(new):
        flags.append("dynamic_field_changed")
    if null_became_zero(old, new):
        flags.append("null_became_zero")
    old_m, new_m = _meta(old), _meta(new)
    if old_m.get("extraction_method") != new_m.get("extraction_method"):
        flags.append("extraction_method_changed")
    if old_m.get("layout") != new_m.get("layout"):
        flags.append("layout_changed")
    if old_m.get("profile") != new_m.get("profile"):
        flags.append("profile_changed")
    if old_m.get("validation_accepted") != new_m.get("validation_accepted"):
        flags.append("validation_changed")
    if old_m.get("totals") != new_m.get("totals"):
        flags.append("totals_changed")
    return (not flags), flags


def _load_manifest_ids() -> List[str]:
    ids: List[str] = []
    records_dir = ROOT / "phase3_eval" / "records"
    for path in sorted(records_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        doc_id = payload.get("document_id") or path.name.replace(".json", "")
        if doc_id and doc_id not in ids:
            ids.append(str(doc_id))

    replay = ROOT / "phase3_eval" / "phase3_4_replay_results.json"
    if replay.is_file():
        data = json.loads(replay.read_text(encoding="utf-8"))
        for key in ("original_37", "pass_regression"):
            for item in data.get(key) or []:
                doc_id = item.get("document_id")
                if doc_id and doc_id not in ids:
                    ids.append(str(doc_id))
    return ids


def _load_baseline_records() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    records_dir = ROOT / "phase3_eval" / "records"
    for path in records_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        doc_id = str(payload.get("document_id") or path.name.replace(".json", ""))
        out[doc_id] = payload
    return out


def _resolve_path(dataset: Path, document_id: str) -> Optional[Path]:
    direct = dataset / document_id
    if direct.is_file():
        return direct
    # Case-insensitive / partial match on stem
    stem = Path(document_id).name
    matches = list(dataset.glob(stem))
    if matches:
        return matches[0]
    # Try without worrying about double extensions in json names
    for path in dataset.iterdir():
        if path.is_file() and path.name.lower() == stem.lower():
            return path
    return None


def run_old_path(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    from services.sales_statement_extractor import (
        attach_phase1_architecture,
        extract_sales_statement_core,
        maybe_apply_phase2_recovery,
    )

    result = extract_sales_statement_core(file_bytes, filename)
    result = attach_phase1_architecture(result, file_bytes, filename)
    return maybe_apply_phase2_recovery(result, file_bytes, filename)


def run_phase4(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    from services.document_intelligence.pipeline import DocumentIntelligencePipeline

    return DocumentIntelligencePipeline().run(file_bytes, filename)


def evaluate_one(
    path: Path,
    *,
    stored: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    file_bytes = path.read_bytes()
    started = time.perf_counter()
    old_err = ""
    new_err = ""
    old = None
    new = None
    try:
        old = run_old_path(file_bytes, path.name)
    except Exception as exc:
        old_err = f"{type(exc).__name__}: {exc}"
    try:
        new = run_phase4(file_bytes, path.name)
    except Exception as exc:
        new_err = f"{type(exc).__name__}: {exc}"
        new_err += "\n" + traceback.format_exc()
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    equal = False
    flags: List[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        equal, flags = _business_equal(old, _strip_phase4(new))
    elif old_err or new_err:
        flags.append("extraction_error")
        equal = bool(old_err) and bool(new_err) and old_err.split(":")[0] == new_err.split(":")[0]

    old_m = _meta(old if isinstance(old, dict) else None)
    new_m = _meta(new if isinstance(new, dict) else None)
    di = (new or {}).get("document_intelligence") if isinstance(new, dict) else None
    decision = ((di or {}).get("decision") or {}) if isinstance(di, dict) else {}

    stored_outcome = (stored or {}).get("baseline_outcome")
    pass_to_wrong = False
    if stored_outcome == "PASS" and isinstance(old, dict) and isinstance(new, dict):
        if baseline_accepted(old) and (
            "canonical_values_changed" in flags or "null_became_zero" in flags
        ):
            pass_to_wrong = True
    if baseline_accepted(old if isinstance(old, dict) else None) and (
        "canonical_values_changed" in flags or "null_became_zero" in flags
    ):
        pass_to_wrong = True

    provider_to_success = False
    if stored and stored.get("baseline_provider_error"):
        if new_m.get("validation_accepted") and decision.get("status") == "VERIFIED":
            provider_to_success = True

    unknown_accepted = False
    if isinstance(di, dict):
        strategy = (di.get("strategy") or {}).get("selected") or {}
        if strategy.get("kind") in {"generic", "vision"} and decision.get("status") == "VERIFIED":
            # VERIFIED on unknown layout without known parser is a soft signal only
            # when validation also lacks prior PASS evidence.
            if not old_m.get("validation_accepted"):
                unknown_accepted = True

    return {
        "document_id": path.name,
        "extension": path.suffix.lower() or "(none)",
        "elapsed_ms": elapsed_ms,
        "old_error": old_err,
        "phase4_error": new_err,
        "business_equal": equal,
        "flags": flags,
        "old": old_m,
        "phase4": new_m,
        "phase4_decision": decision.get("status"),
        "stored_baseline_outcome": stored_outcome,
        "pass_to_wrong": pass_to_wrong,
        "null_became_zero": "null_became_zero" in flags,
        "canonical_changed": "canonical_values_changed" in flags,
        "dynamic_corruption": "dynamic_field_changed" in flags,
        "unknown_layout_accepted": unknown_accepted,
        "provider_error_to_success": provider_to_success,
        "ocr_invoked_in_pipeline": bool(
            isinstance(di, dict)
            and (di.get("ocr") or {}).get("invoked_here")
        ),
    }


def main() -> int:
    _configure_env()
    _disable_gemini()
    _install_core_cache()
    dataset = Path(os.environ.get("PHASE4_DATASET", str(DEFAULT_DATASET)))
    ids = _load_manifest_ids()
    stored_map = _load_baseline_records()
    print(f"Stage A manifest documents: {len(ids)}", flush=True)
    print(f"Dataset: {dataset}", flush=True)
    print("Gemini hard-disabled; Phase2 off; OCR ensemble off", flush=True)

    checkpoint_path = OUTPUT / "compatibility_checkpoint.json"
    prior = {}
    if checkpoint_path.is_file() and not os.environ.get("PHASE4_COMPAT_FORCE"):
        try:
            prior = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = {}
    done_map = {
        r.get("document_id"): r
        for r in (prior.get("results") or [])
        if isinstance(r, dict) and r.get("document_id")
    }

    results: List[Dict[str, Any]] = []
    missing: List[str] = []
    for index, doc_id in enumerate(ids, 1):
        if doc_id in done_map and not os.environ.get("PHASE4_COMPAT_FORCE"):
            results.append(done_map[doc_id])
            if index % 20 == 0:
                print(f"[{index}/{len(ids)}] resume-skip {doc_id}", flush=True)
            continue
        path = _resolve_path(dataset, doc_id)
        if path is None:
            missing.append(doc_id)
            record = {
                "document_id": doc_id,
                "missing_source": True,
                "business_equal": False,
                "flags": ["missing_source"],
                "pass_to_wrong": False,
            }
            results.append(record)
            print(f"[{index}/{len(ids)}] MISSING {doc_id}", flush=True)
        else:
            print(f"[{index}/{len(ids)}] {path.name}", flush=True)
            record = evaluate_one(
                path, stored=stored_map.get(doc_id) or stored_map.get(path.name)
            )
            results.append(record)
            status = "OK" if record.get("business_equal") else "DIFF"
            print(
                f"  {status} equal={record.get('business_equal')} "
                f"flags={record.get('flags')} decision={record.get('phase4_decision')}",
                flush=True,
            )
        _write(
            checkpoint_path,
            {"results": results, "updated_at": time.time(), "count": len(results)},
        )

    pass_to_wrong = sum(1 for r in results if r.get("pass_to_wrong"))
    null_zero = sum(1 for r in results if r.get("null_became_zero"))
    canonical = sum(1 for r in results if r.get("canonical_changed"))
    dynamic = sum(1 for r in results if r.get("dynamic_corruption"))
    unknown_acc = sum(1 for r in results if r.get("unknown_layout_accepted"))
    provider_ok = sum(1 for r in results if r.get("provider_error_to_success"))
    unequal = [r for r in results if not r.get("business_equal") and not r.get("missing_source")]
    material = [
        r
        for r in results
        if r.get("pass_to_wrong")
        or r.get("null_became_zero")
        or r.get("canonical_changed")
        or r.get("provider_error_to_success")
        or (
            not r.get("business_equal")
            and not r.get("missing_source")
            and any(
                f in (r.get("flags") or [])
                for f in (
                    "canonical_values_changed",
                    "null_became_zero",
                    "validation_changed",
                    "extraction_method_changed",
                    "totals_changed",
                )
            )
        )
    ]

    decision = "COMPATIBILITY_PASS"
    if (
        pass_to_wrong
        or null_zero
        or provider_ok
        or material
    ):
        decision = "REGRESSION_FOUND"

    summary = {
        "decision": decision,
        "manifest_count": len(ids),
        "evaluated": len(results) - len(missing),
        "missing_source": len(missing),
        "business_equal": sum(1 for r in results if r.get("business_equal")),
        "business_unequal": len(unequal),
        "safety": {
            "pass_to_wrong": pass_to_wrong,
            "null_became_zero": null_zero,
            "canonical_value_changed_incorrectly": canonical,
            "dynamic_field_corruption": dynamic,
            "unknown_layout_incorrectly_accepted": unknown_acc,
            "provider_error_converted_to_success": provider_ok,
        },
        "env": {
            "PHASE2_RECOVERY_ENABLED": os.environ.get("PHASE2_RECOVERY_ENABLED"),
            "OCR_ENGINE": os.environ.get("OCR_ENGINE"),
            "OCR_FALLBACK_ENGINE": os.environ.get("OCR_FALLBACK_ENGINE"),
            "ENABLE_GEMINI_EXTRACTION_FALLBACK": os.environ.get(
                "ENABLE_GEMINI_EXTRACTION_FALLBACK"
            ),
        },
        "material_regressions": [
            {
                "document_id": r.get("document_id"),
                "flags": r.get("flags"),
                "old": r.get("old"),
                "phase4": r.get("phase4"),
            }
            for r in material
        ],
        "missing_ids": missing,
    }

    payload = {"summary": summary, "results": results}
    _write(OUTPUT / "compatibility_results.json", payload)

    lines = [
        "# Phase 4 Compatibility Report (Stage A)",
        "",
        f"**Decision: {decision}**",
        "",
        "## Environment",
        "",
        "- `PHASE2_RECOVERY_ENABLED=false`",
        "- `OCR_ENGINE=paddleocr-vl`",
        "- `OCR_FALLBACK_ENGINE=off`",
        "- `ENABLE_GEMINI_EXTRACTION_FALLBACK=false`",
        "- `ENABLE_VISION_FALLBACK=false`",
        "- Gemini hard-blocked in process (parser vision + fallback)",
        "- No second OCR engine, no Phase 2 recovery",
        "",
        "## Manifest",
        "",
        f"- Source: `phase3_eval/records` + `phase3_4_replay_results.json`",
        f"- Documents listed: {len(ids)}",
        f"- Evaluated: {summary['evaluated']}",
        f"- Missing source files: {len(missing)}",
        "",
        "## Comparison",
        "",
        "OLD = `extract_sales_statement_core` + Phase 1 + Phase 2 gate (off)",
        "",
        "NEW = `DocumentIntelligencePipeline`",
        "",
        f"- Business-equal: {summary['business_equal']}",
        f"- Business-unequal: {summary['business_unequal']}",
        "",
        "## Hard safety gate",
        "",
        f"- PASS → WRONG: **{pass_to_wrong}**",
        f"- null → zero: **{null_zero}**",
        f"- Canonical value changed incorrectly: **{canonical}**",
        f"- Dynamic field corruption: **{dynamic}**",
        f"- Unknown layout incorrectly accepted: **{unknown_acc}**",
        f"- Provider error → success: **{provider_ok}**",
        "",
        "## Material regressions",
        "",
    ]
    if not material:
        lines.append("None.")
    else:
        for item in summary["material_regressions"][:40]:
            lines.append(
                f"- `{item['document_id']}` flags={item['flags']}"
            )
        lines.append("")
        lines.append("STOP — Stage B must not run.")
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            "- `phase4/compatibility_results.json`",
            "- `phase4/COMPATIBILITY_REPORT.md`",
            "",
        ]
    )
    (OUTPUT / "COMPATIBILITY_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(decision, flush=True)
    return 0 if decision == "COMPATIBILITY_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
