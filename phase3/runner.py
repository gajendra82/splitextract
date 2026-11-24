"""Shadow evaluation. Writes only under the Phase 3 output directory."""

from __future__ import annotations

import copy
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from phase3.compare import baseline_accepted, classify_transition, recovery_state
from phase3.telemetry import project_telemetry
from phase3.verify import resolve_ground_truth, verify_against_ground_truth


def assert_output_outside_dataset(dataset_dir: Path, output_dir: Path) -> None:
    dataset = Path(dataset_dir).resolve()
    output = Path(output_dir).resolve()
    if output == dataset or dataset in output.parents:
        raise RuntimeError("Phase 3 output must not be written inside the dataset")


def extract_without_phase2(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """Existing extractor with Phase 2 forced off for this call only."""
    previous = os.environ.get("PHASE2_RECOVERY_ENABLED")
    os.environ["PHASE2_RECOVERY_ENABLED"] = "false"
    try:
        from services.sales_statement_extractor import extract_sales_statement

        return extract_sales_statement(file_bytes, filename)
    finally:
        if previous is None:
            os.environ.pop("PHASE2_RECOVERY_ENABLED", None)
        else:
            os.environ["PHASE2_RECOVERY_ENABLED"] = previous


def recover_shadow(baseline: Dict[str, Any], file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """Approved Phase 2 recovery on a copy. Does not write the source file."""
    from services.recovery_runner import apply_phase2_recovery

    return apply_phase2_recovery(
        copy.deepcopy(baseline),
        file_bytes=file_bytes,
        filename=filename,
    )


def _provider_error(text: str) -> bool:
    upper = (text or "").upper()
    return "RESOURCE_EXHAUSTED" in upper or (
        "429" in upper and any(token in upper for token in ("GEMINI", "QUOTA", "RATE", "VERTEX"))
    )


def _record_id(relative_path: str) -> str:
    return relative_path.replace("\\", "__").replace("/", "__").replace(":", "_")


def evaluate_document(
    path: Path,
    *,
    dataset_dir: Path,
    output_dir: Path,
    extract_fn: Callable[[bytes, str], Dict[str, Any]] = extract_without_phase2,
    recover_fn: Callable[[Dict[str, Any], bytes, str], Dict[str, Any]] = recover_shadow,
    ground_directories: Optional[List[Path]] = None,
    allow_pdf_ground_truth: bool = True,
) -> Dict[str, Any]:
    assert_output_outside_dataset(dataset_dir, output_dir)
    path = Path(path)
    before = path.stat()
    file_bytes = path.read_bytes()
    started = time.perf_counter()
    baseline = None
    baseline_error = ""
    try:
        baseline = extract_fn(file_bytes, path.name)
    except Exception as exc:
        baseline_error = f"{type(exc).__name__}: {exc}"
    phase2_result = None
    recover_error = ""
    if isinstance(baseline, dict):
        original_items = copy.deepcopy(baseline.get("line_items"))
        try:
            phase2_result = recover_fn(copy.deepcopy(baseline), file_bytes, path.name)
        except Exception as exc:
            recover_error = f"{type(exc).__name__}: {exc}"
            phase2_result = copy.deepcopy(baseline)
        if baseline.get("line_items") != original_items:
            raise RuntimeError("shadow recovery modified the baseline line items")
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    after = path.stat()
    if after.st_size != before.st_size or after.st_mtime != before.st_mtime:
        raise RuntimeError("shadow evaluation modified the source file")

    route = {}
    if isinstance(baseline, dict) and isinstance(baseline.get("phase1"), dict):
        route = baseline["phase1"].get("route") or {}
    requires_ocr = bool(route.get("requires_ocr"))
    ground = None
    ground_source = "none"
    if isinstance(phase2_result, dict):
        try:
            ground, ground_source = resolve_ground_truth(
                path,
                stored_directories=ground_directories or [],
                allow_pdf_build=allow_pdf_ground_truth,
                requires_ocr=requires_ocr,
            )
        except Exception:
            ground, ground_source = None, "none"
    verification = "UNVERIFIED"
    if isinstance(phase2_result, dict) and recovery_state(phase2_result) == "RECOVERY_ACCEPTED":
        verification = verify_against_ground_truth(ground, phase2_result)

    compared = classify_transition(
        baseline if isinstance(baseline, dict) else {},
        phase2_result if isinstance(phase2_result, dict) else {},
        verification=verification,
        profile_id="",
        baseline_error=bool(baseline_error),
    )
    route_kind = str(route.get("file_kind") or path.suffix.lower().lstrip("."))
    route_pdf = str(route.get("pdf_kind") or "")
    baseline_telemetry = project_telemetry(
        baseline if isinstance(baseline, dict) else {},
        document_id=path.name,
        file_kind=route_kind,
        pdf_kind=route_pdf,
        requires_ocr=requires_ocr,
        processing_time_ms=elapsed_ms,
    )
    final_telemetry = project_telemetry(
        phase2_result if isinstance(phase2_result, dict) else (
            baseline if isinstance(baseline, dict) else {}
        ),
        document_id=path.name,
        file_kind=route_kind,
        pdf_kind=route_pdf,
        requires_ocr=requires_ocr,
        processing_time_ms=elapsed_ms,
    )
    telemetry = dict(baseline_telemetry)
    telemetry["final_validation_accepted"] = final_telemetry.get("final_validation_accepted")
    telemetry["final_status"] = final_telemetry.get("final_status")
    telemetry["candidate_status"] = final_telemetry.get("candidate_status")
    telemetry["recovery_strategy"] = final_telemetry.get("recovery_strategy")
    telemetry["attempt_number"] = final_telemetry.get("attempt_number")
    if final_telemetry.get("failure_type"):
        telemetry["failure_type"] = final_telemetry.get("failure_type")
    if telemetry.get("profile"):
        compared = classify_transition(
            baseline if isinstance(baseline, dict) else {},
            phase2_result if isinstance(phase2_result, dict) else {},
            verification=verification,
            profile_id=str(telemetry.get("profile") or ""),
            baseline_error=bool(baseline_error),
        )
    if baseline_error:
        outcome = "ERROR"
    elif baseline_accepted(baseline):
        outcome = "PASS"
    else:
        outcome = "FAIL"
    attempts = 0
    if isinstance(phase2_result, dict) and isinstance(phase2_result.get("phase2"), dict):
        attempts = int(phase2_result["phase2"].get("attempts") or 0)
    rel = str(path.resolve().relative_to(Path(dataset_dir).resolve())) if path.is_absolute() else path.name
    try:
        rel = str(path.resolve().relative_to(Path(dataset_dir).resolve()))
    except ValueError:
        rel = path.name
    return {
        "document_id": path.name,
        "relative_path": rel,
        "file_type": path.suffix.lower() or "(none)",
        "baseline_outcome": outcome,
        "baseline_error": baseline_error,
        "recover_error": recover_error,
        "baseline_provider_error": _provider_error(baseline_error),
        "baseline_row_count": len((baseline or {}).get("line_items") or []) if isinstance(baseline, dict) else 0,
        "phase2_row_count": len((phase2_result or {}).get("line_items") or []) if isinstance(phase2_result, dict) else 0,
        "transition": compared["transition"],
        "recovery_state": compared["recovery_state"],
        "verification": compared["verification"],
        "safety_flags": compared["safety_flags"],
        "ground_truth_source": ground_source if ground is not None else "none",
        "attempts": attempts,
        "provider_calls": attempts,
        "processing_time_ms": elapsed_ms,
        "ocr_required": bool(telemetry.get("ocr_required")),
        "ocr_path": telemetry.get("ocr_path"),
        "ocr_quality": telemetry.get("ocr_quality"),
        "profile": telemetry.get("profile") or "unknown",
        "recovery_strategy": telemetry.get("recovery_strategy") or "none",
        "failure_type": telemetry.get("failure_type") or "none",
        "extraction_method": telemetry.get("extraction_method"),
        "phase1_accepted": telemetry.get("phase1_accepted"),
        "final_status": telemetry.get("final_status"),
        "final_validation_accepted": telemetry.get("final_validation_accepted"),
        "evidence_type": telemetry.get("evidence_type"),
        "mapping_status": telemetry.get("mapping_status"),
        "projection_status": telemetry.get("projection_status"),
        "pdf_kind": telemetry.get("pdf_kind"),
        "telemetry": telemetry,
    }


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def load_progress(output_dir: Path) -> Dict[str, Any]:
    path = Path(output_dir) / "progress.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def run_batch(
    selection: List[Dict[str, Any]],
    *,
    dataset_dir: Path,
    output_dir: Path,
    extract_fn: Callable[[bytes, str], Dict[str, Any]] = extract_without_phase2,
    recover_fn: Callable[[Dict[str, Any], bytes, str], Dict[str, Any]] = recover_shadow,
    ground_directories: Optional[List[Path]] = None,
    allow_pdf_ground_truth: bool = True,
    force: bool = False,
) -> List[Dict[str, Any]]:
    """Evaluate the selected batch and checkpoint after every document."""
    assert_output_outside_dataset(dataset_dir, output_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    progress = {} if force else load_progress(output_dir)
    records: List[Dict[str, Any]] = []
    for item in selection:
        relative = str(item.get("relative_path") or item.get("filename"))
        record_path = output_dir / "records" / f"{_record_id(relative)}.json"
        if not force and progress.get(relative, {}).get("state") == "complete" and record_path.is_file():
            records.append(json.loads(record_path.read_text(encoding="utf-8")))
            continue
        source = Path(item["path"]) if item.get("path") else Path(dataset_dir) / relative
        try:
            record = evaluate_document(
                source,
                dataset_dir=dataset_dir,
                output_dir=output_dir,
                extract_fn=extract_fn,
                recover_fn=recover_fn,
                ground_directories=ground_directories,
                allow_pdf_ground_truth=allow_pdf_ground_truth,
            )
        except Exception as exc:
            record = {
                "document_id": Path(relative).name,
                "relative_path": relative,
                "file_type": Path(relative).suffix.lower() or "(none)",
                "baseline_outcome": "ERROR",
                "baseline_error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "transition": "BASELINE_ERROR",
                "recovery_state": "RECOVERY_NOT_ATTEMPTED",
                "verification": "RECOVERY_UNVERIFIED",
                "safety_flags": [],
                "attempts": 0,
                "provider_calls": 0,
                "processing_time_ms": None,
                "ocr_required": bool(item.get("requires_ocr")),
                "ocr_path": "ocr_required" if item.get("requires_ocr") else "other",
                "profile": "unknown",
                "recovery_strategy": "none",
                "failure_type": "none",
            }
        record["relative_path"] = relative
        _write_json(record_path, record)
        progress[relative] = {
            "state": "complete",
            "transition": record.get("transition"),
            "baseline_outcome": record.get("baseline_outcome"),
        }
        _write_json(output_dir / "progress.json", progress)
        records.append(record)
        print(
            f"{len(records)}/{len(selection)} {relative} {str(record.get('transition')).replace(chr(8594), '->')}",
            flush=True,
        )
    return records
