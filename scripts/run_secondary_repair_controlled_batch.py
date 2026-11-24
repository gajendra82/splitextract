"""Controlled / full-dataset secondary OCR repair evaluation (DI production path).

Usage:
  python scripts/run_secondary_repair_controlled_batch.py \\
      --image-root \"C:\\Users\\amnsa\\Downloads\\image dataset stock\" \\
      --all-385 --resume

Protocol:
  1) Reconfirm GT-20 OFF vs ON via DocumentIntelligencePipeline (stored VL +
     real secondary + real core override). Stop if regressions/incorrect.
  2) Evaluate every image under --image-root (or nested prerana/): primary OCR
     once (cached), then OFF vs ON with identical bytes. Checkpoint + resume.
  3) Always restore OCR_SECONDARY_REPAIR to OFF.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.document_intelligence.pipeline import DocumentIntelligencePipeline
from services.ground_truth.matching import match_field
from services.ground_truth.schema import only_source_verified_fields, validate_ground_truth_document
from services.ocr import secondary_repair as sr
from services.ocr.base import OCRResult
from services.ocr.orchestrator import OCRBundle, OCROrchestrator
from services.ocr.ocr_text_override import get_ocr_text_override, ocr_text_override

GT_DIR = ROOT / "phase4" / "ground_truth_50" / "per_document"
VL_DIR = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"
OUT_ROOT = ROOT / "phase4" / "secondary_repair_canary" / "controlled_batch"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".pdf", ".tif", ".tiff", ".webp", ".bmp"}
DOC_TIMEOUT_SEC = int(os.getenv("SECONDARY_REPAIR_BATCH_TIMEOUT_SEC", "180"))
# Cold Paddle VL serve start + first inference can exceed 5 minutes on this host.
PRIMARY_TIMEOUT_SEC = int(os.getenv("SECONDARY_REPAIR_PRIMARY_TIMEOUT_SEC", "600"))

_ORCH: Optional[OCROrchestrator] = None

# Align with services.ocr.orchestrator._DEFAULT_PRIMARY (not tesseract).
_DEFAULT_BATCH_OCR_ENGINE = "paddleocr-vl"


def _assert_primary_engine(required: Optional[str]) -> str:
    """Log requested vs instantiated primary; abort on mismatch when required."""
    from services.ocr.orchestrator import ocr_settings

    requested = (os.getenv("OCR_ENGINE") or _DEFAULT_BATCH_OCR_ENGINE).strip().lower()
    settings = ocr_settings()
    actual = (settings.primary_engine or "").strip().lower()
    orch = _get_orchestrator()
    eng = orch.engine(actual)
    available = bool(eng is not None and eng.available())
    print(
        f"[BATCH] OCR engine requested={requested!r} "
        f"settings.primary={actual!r} available={available}",
        flush=True,
    )
    if required:
        req = required.strip().lower()
        aliases = {req}
        if req in {"paddleocr-vl", "paddleocr-vl-1.6", "paddle_vl"}:
            aliases |= {"paddleocr-vl", "paddleocr-vl-1.6", "paddle_vl"}
        if actual not in aliases:
            raise SystemExit(
                f"Primary OCR mismatch: require {req!r} but OCR_ENGINE/settings "
                f"selected {actual!r}. Refusing to continue (no silent Tesseract)."
            )
        if not available:
            raise SystemExit(
                f"Required primary OCR {req!r} is not available. "
                f"Refusing unverified fallback / silent engine swap."
            )
    return actual


def _get_orchestrator() -> OCROrchestrator:
    """Reuse one OCROrchestrator so the VL serve worker stays warm."""
    global _ORCH
    if _ORCH is None:
        _ORCH = OCROrchestrator()
    return _ORCH


class _StubOrchestrator(OCROrchestrator):
    def __init__(self, text: str, engine: str = "cached-primary") -> None:
        self.settings = type(
            "S",
            (),
            {"primary_engine": engine, "__dict__": {"primary_engine": engine}},
        )()
        self._text = text
        self._engines = {}

    def recognize(
        self,
        image_bytes,
        *,
        page=1,
        force_secondary=False,
        low_confidence=False,
        metadata=None,
    ):
        return OCRBundle(
            primary=OCRResult(
                text=self._text,
                engine=self.settings.primary_engine,
                page=page,
                metadata={"status": "success"},
            ),
            engines_used=[self.settings.primary_engine],
            metadata={"status": "success"},
        )


def _utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _count_images(directory: Path) -> int:
    return sum(
        1
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def resolve_image_dataset(image_root: Path) -> Tuple[Path, List[Path]]:
    """Validate --image-root and locate the ~385-document image set."""
    root = image_root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"--image-root is not a directory: {root}")

    candidates: List[Tuple[int, Path]] = []
    for cand in [root, root / "prerana", *sorted(p for p in root.iterdir() if p.is_dir())]:
        if not cand.is_dir():
            continue
        n = _count_images(cand)
        if n > 0:
            candidates.append((n, cand))

    if not candidates:
        raise SystemExit(f"No image/PDF files found under {root}")

    # Prefer a directory whose file count is closest to 385 (exact match wins).
    candidates.sort(key=lambda t: (abs(t[0] - 385), -t[0], str(t[1])))
    best_n, best = candidates[0]
    if best_n < 50:
        raise SystemExit(
            f"Resolved dataset under {best} has only {best_n} files "
            f"(expected ~385). Refusing to continue."
        )
    files = sorted(
        p for p in best.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    # Deduplicate by sha256+stem later; return unique paths by name.
    return best, files


def _statement_to_cells(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    cells: List[Dict[str, Any]] = []
    header_map = [
        ("opening_qty", "Op.Bal. Qty."),
        ("closing_qty", "Closing Balance"),
        ("sales_qty", "Issue Qty."),
        ("receipts_qty", "Receipt Qty."),
    ]
    for item in result.get("line_items") or []:
        pk = item.get("product_name") or item.get("name")
        packing = item.get("packing")
        for field, header in header_map:
            if field not in item:
                continue
            val = item.get(field)
            raw = (
                ""
                if val is None
                else (
                    str(int(val))
                    if isinstance(val, float) and val == int(val)
                    else str(val)
                )
            )
            cells.append(
                {
                    "is_header": False,
                    "product_key": pk,
                    "packing": packing,
                    "header": header,
                    "value": raw,
                }
            )
            if field == "closing_qty":
                cells.append(
                    {
                        "is_header": False,
                        "product_key": pk,
                        "packing": packing,
                        "header": "Closing",
                        "value": raw,
                    }
                )
        exp = (item.get("extra") or {}).get("expiry_breakage_qty")
        if exp is not None:
            cells.append(
                {
                    "is_header": False,
                    "product_key": pk,
                    "packing": packing,
                    "header": "Expiry Breakage",
                    "value": str(int(exp))
                    if isinstance(exp, float) and exp == int(exp)
                    else str(exp),
                }
            )
    totals = result.get("totals") or {}
    for field, header in header_map:
        if field in totals and totals.get(field) is not None:
            val = totals.get(field)
            cells.append(
                {
                    "is_header": False,
                    "product_key": "TOTAL",
                    "header": header,
                    "value": str(int(val))
                    if isinstance(val, float) and val == int(val)
                    else str(val),
                }
            )
    return cells


def _qty_fingerprint(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for item in result.get("line_items") or []:
        rows.append(
            {
                "product_name": item.get("product_name") or item.get("name"),
                "packing": item.get("packing"),
                "opening_qty": item.get("opening_qty"),
                "closing_qty": item.get("closing_qty"),
                "sales_qty": item.get("sales_qty"),
                "receipts_qty": item.get("receipts_qty"),
                "expiry_breakage_qty": (item.get("extra") or {}).get(
                    "expiry_breakage_qty"
                ),
            }
        )
    totals = result.get("totals") or {}
    rows.append(
        {
            "product_name": "TOTAL",
            "opening_qty": totals.get("opening_qty"),
            "closing_qty": totals.get("closing_qty"),
            "sales_qty": totals.get("sales_qty"),
            "receipts_qty": totals.get("receipts_qty"),
        }
    )
    return rows


def _diff_qty(off: Dict[str, Any], on: Dict[str, Any]) -> List[Dict[str, Any]]:
    off_map = {
        (
            str(r.get("product_name") or ""),
            str(r.get("packing") or ""),
        ): r
        for r in _qty_fingerprint(off)
    }
    on_map = {
        (
            str(r.get("product_name") or ""),
            str(r.get("packing") or ""),
        ): r
        for r in _qty_fingerprint(on)
    }
    deltas = []
    keys = sorted(set(off_map) | set(on_map))
    fields = (
        "opening_qty",
        "closing_qty",
        "sales_qty",
        "receipts_qty",
        "expiry_breakage_qty",
    )
    for key in keys:
        a = off_map.get(key) or {}
        b = on_map.get(key) or {}
        for field in fields:
            if a.get(field) != b.get(field):
                deltas.append(
                    {
                        "product_name": key[0],
                        "packing": key[1],
                        "field": field,
                        "off": a.get(field),
                        "on": b.get(field),
                    }
                )
    return deltas


def _cohort_from_text(text: str, document_id: str) -> str:
    low = (text or "").casefold()
    stem = Path(document_id).stem
    if "0209" in stem or "0124" in stem:
        return "negative_unresolved"
    has_opbal = "op.bal" in low or "opbal" in low.replace(" ", "")
    has_expiry = "expiry breakage" in low or "expirybreakage" in low.replace(" ", "")
    has_issue = "issue" in low
    has_closing_bal = "closing balance" in low
    has_liv = "liv-52 syrup" in low and "100ml" in low.replace(" ", "")
    if has_opbal and has_expiry:
        return "repair_himalaya"
    if has_opbal and has_issue and has_closing_bal:
        return "repair_total_footer"
    if has_liv and ("opening" in low or "issues" in low):
        return "repair_liv_strike"
    if has_opbal:
        return "compatible_himalaya_qty"
    if "opening" in low and "closing" in low:
        return "compatible_ssa"
    compact = "".join(ch for ch in low if ch.isalnum())
    if len(compact) < 80:
        return "ambiguous_poor_quality"
    return "ordinary"


def _run_di(
    document_id: str,
    file_bytes: bytes,
    primary_text: str,
    flag: str,
    *,
    timeout_sec: int = DOC_TIMEOUT_SEC,
) -> Dict[str, Any]:
    import services.sales_statement_extractor as sse

    pipe = DocumentIntelligencePipeline(
        ocr=_StubOrchestrator(primary_text, engine="cached-primary")
    )
    real_core = sse.extract_sales_statement_core

    def _vision_forbidden(*_a, **_k):
        raise RuntimeError("paid vision forbidden in controlled batch")

    def _call():
        with mock.patch.dict(
            os.environ,
            {
                "OCR_SECONDARY_REPAIR": flag,
                "PHASE2_RECOVERY_ENABLED": "false",
                "OCR_ENGINE": os.getenv("OCR_ENGINE", _DEFAULT_BATCH_OCR_ENGINE),
            },
            clear=False,
        ):
            with mock.patch(
                "services.sales_extraction_runtime.sales_generate_content_via_vertex",
                side_effect=_vision_forbidden,
            ):
                if str(flag).strip().lower() in {"1", "true", "yes", "on"}:
                    return pipe.run(file_bytes, document_id)

                def _core_off(_b, _n):
                    if get_ocr_text_override() is not None:
                        raise RuntimeError("DI must not set override when flag OFF")
                    with ocr_text_override(primary_text):
                        return real_core(_b, _n)

                with mock.patch(
                    "services.sales_statement_extractor.extract_sales_statement_core",
                    side_effect=_core_off,
                ):
                    with mock.patch(
                        "services.sales_statement_extractor.maybe_apply_phase2_recovery",
                        side_effect=lambda result, *a, **k: result,
                    ):
                        return pipe.run(file_bytes, document_id)

    # Do not use `with ThreadPoolExecutor`: on timeout its __exit__ calls
    # shutdown(wait=True) and wedges forever on a hung worker thread.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(_call)
        try:
            return fut.result(timeout=timeout_sec)
        except FuturesTimeout as exc:
            raise TimeoutError(
                f"{document_id} flag={flag} exceeded {timeout_sec}s"
            ) from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _primary_ocr_once(
    path: Path, cache_path: Path, *, timeout_sec: int = PRIMARY_TIMEOUT_SEC
) -> Dict[str, Any]:
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cached = None
        # Never reuse empty/failed VL cache — that poisoned doc 31 resume.
        if (
            isinstance(cached, dict)
            and cached.get("status") == "success"
            and (cached.get("text") or "").strip()
        ):
            return cached
        try:
            cache_path.unlink()
        except OSError:
            pass

    file_bytes = path.read_bytes()

    def _call():
        orch = _get_orchestrator()
        bundle = orch.recognize(file_bytes, page=1)
        primary = getattr(bundle, "primary", None)
        text = (primary.text if primary else "") or ""
        engine = (primary.engine if primary else "") or ""
        return {
            "document_id": path.name,
            "sha256": _sha256(file_bytes),
            "engine": engine,
            "text": text,
            "text_len": len(text),
            "status": (getattr(primary, "metadata", None) or {}).get("status"),
        }

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(_call)
        try:
            payload = fut.result(timeout=timeout_sec)
        except FuturesTimeout as exc:
            # Reset serve worker so the next document is not blocked by a hung thread.
            global _ORCH
            try:
                from services.ocr.paddle_vl_engine import _kill_serve_proc

                _kill_serve_proc()
            except Exception:
                pass
            _ORCH = None
            raise TimeoutError(
                f"primary OCR timeout {path.name} after {timeout_sec}s"
            ) from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    # Only persist successful nonempty OCR; failures must be retried next run.
    if payload.get("status") == "success" and (payload.get("text") or "").strip():
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def reconfirm_gt20(out_dir: Path) -> Dict[str, Any]:
    """GT-20 OFF/ON through production DI entrypoint (stored VL + real secondary)."""
    totals = {
        "fields": 0,
        "off_correct": 0,
        "on_correct": 0,
        "verified_corrections": 0,
        "regressions": 0,
        "incorrect_on": 0,
        "unresolved_missing_on": 0,
    }
    stop: List[str] = []
    docs = []
    prior = os.environ.get("OCR_SECONDARY_REPAIR")
    try:
        for path in sorted(GT_DIR.glob("*.json")):
            gt = json.loads(path.read_text(encoding="utf-8"))
            ok, _ = validate_ground_truth_document(gt)
            if not ok:
                continue
            fields = only_source_verified_fields(gt)
            if not fields:
                continue
            doc = gt["document_id"]
            src = sr.resolve_source_path(doc)
            vl_path = VL_DIR / f"{doc}.json"
            if src is None or not src.is_file() or not vl_path.is_file():
                stop.append(f"missing_source_or_vl:{doc}")
                continue
            vl_text = json.loads(vl_path.read_text(encoding="utf-8"))["pages"][0].get(
                "text"
            ) or ""
            file_bytes = src.read_bytes()
            t0 = time.perf_counter()
            off_res = _run_di(doc, file_bytes, vl_text, "false")
            on_res = _run_di(doc, file_bytes, vl_text, "true")
            elapsed_ms = (time.perf_counter() - t0) * 1000
            off_cells = _statement_to_cells(off_res)
            on_cells = _statement_to_cells(on_res)
            doc_deltas = []
            for f in fields:
                if f.get("field_type") == "header":
                    continue
                totals["fields"] += 1
                m0 = match_field(f, ocr_text="", table_cells=off_cells)
                m1 = match_field(f, ocr_text="", table_cells=on_cells)
                if m0.get("correct"):
                    totals["off_correct"] += 1
                if m1.get("correct"):
                    totals["on_correct"] += 1
                if (not m0.get("correct")) and m1.get("correct"):
                    totals["verified_corrections"] += 1
                    doc_deltas.append(
                        {
                            "field_id": f.get("field_id"),
                            "off": m0.get("predicted_raw"),
                            "on": m1.get("predicted_raw"),
                            "expected": f.get("expected_raw"),
                        }
                    )
                if m0.get("correct") and not m1.get("correct"):
                    totals["regressions"] += 1
                    stop.append(f"regression:{doc}:{f.get('field_id')}")
                if (
                    m1.get("predicted_raw") not in (None, "")
                    and not m1.get("correct")
                    and m0.get("predicted_raw") != m1.get("predicted_raw")
                ):
                    overlay = ((on_res.get("totals") or {}).get("extra") or {}).get(
                        "secondary_repair_overlay"
                    )
                    if overlay:
                        totals["incorrect_on"] += 1
                        stop.append(f"incorrect:{doc}:{f.get('field_id')}")
                # Unresolved silently populated: OFF empty/missing, ON invented wrong.
                if (
                    (m0.get("predicted_raw") in (None, "") or m0.get("error_type") == "MISSING")
                    and m1.get("predicted_raw") not in (None, "")
                    and not m1.get("correct")
                ):
                    stop.append(f"unresolved_populated:{doc}:{f.get('field_id')}")
                    totals["unresolved_missing_on"] += 1
            overlay = ((on_res.get("totals") or {}).get("extra") or {}).get(
                "secondary_repair_overlay"
            )
            # Provenance gate: any overlay field must carry repair type.
            if overlay:
                for entry in overlay.get("fields") or []:
                    if not entry.get("repair"):
                        stop.append(f"missing_provenance:{doc}:{entry.get('field')}")
            docs.append(
                {
                    "document_id": doc,
                    "deltas": doc_deltas,
                    "elapsed_ms": elapsed_ms,
                    "overlay_on": overlay,
                }
            )
            print(
                f"[GT20] {doc[:48]} deltas={len(doc_deltas)} "
                f"overlay={bool(overlay)} ms={elapsed_ms:.0f}"
            )
            if stop:
                break
    finally:
        if prior is None:
            os.environ.pop("OCR_SECONDARY_REPAIR", None)
        else:
            os.environ["OCR_SECONDARY_REPAIR"] = prior

    decision = "PASS"
    if stop or totals["regressions"] or totals["incorrect_on"]:
        decision = "BLOCKED"
    elif totals["verified_corrections"] < 5:
        decision = "INCONCLUSIVE"
        stop.append(f"verified_corrections={totals['verified_corrections']}")

    payload = {
        "decision": decision,
        "totals": totals,
        "stop_reasons": stop,
        "documents": docs,
    }
    (out_dir / "gt20_reconfirm.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    print(f"[GT20] DECISION {decision} {json.dumps(totals)}")
    return payload


def _load_completed(checkpoints_path: Path) -> set:
    done = set()
    if not checkpoints_path.is_file():
        return done
    for line in checkpoints_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") == "complete" and row.get("document_id"):
            done.add(row["document_id"])
    return done


def _append_checkpoint(checkpoints_path: Path, row: Dict[str, Any]) -> None:
    with checkpoints_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _incompatible_layout_edit(cohort: str, overlay: Optional[Dict[str, Any]]) -> Optional[str]:
    if not overlay:
        return None
    repairs = {str(f.get("repair") or "") for f in (overlay.get("fields") or [])}
    if cohort == "compatible_ssa" and any("total_footer" in r for r in repairs):
        return "incompatible_layout:total_footer_on_ssa"
    if cohort == "ordinary" and any(
        r.startswith("himalaya_") or r.startswith("liv_") or "total_footer" in r
        for r in repairs
        if r
    ):
        # Ordinary may still legitimately hit total/himalaya if headers match;
        # only flag total_footer when layout heuristic said ordinary AND no op.bal.
        if any("total_footer" in r for r in repairs):
            return "incompatible_layout:total_footer_on_ordinary"
    return None


def run_batch(
    *,
    image_root: Path,
    run_id: str,
    resume: bool,
    max_docs: Optional[int],
    skip_gt20: bool,
    require_primary_engine: Optional[str] = None,
) -> int:
    out_dir = OUT_ROOT / run_id
    per_dir = out_dir / "per_document"
    primary_dir = out_dir / "primary_ocr_cache"
    per_dir.mkdir(parents=True, exist_ok=True)
    primary_dir.mkdir(parents=True, exist_ok=True)
    checkpoints_path = out_dir / "checkpoints.jsonl"

    print(f"[BATCH] Resolving dataset under {image_root}", flush=True)
    _assert_primary_engine(require_primary_engine)
    dataset_dir, files = resolve_image_dataset(image_root)
    if max_docs is not None:
        files = files[: max(0, int(max_docs))]

    manifest = {
        "run_id": run_id,
        "image_root_arg": str(image_root),
        "resolved_dataset_dir": str(dataset_dir),
        "document_count": len(files),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "entrypoint": "DocumentIntelligencePipeline.run",
        "primary_ocr_engine_env": os.getenv("OCR_ENGINE", _DEFAULT_BATCH_OCR_ENGINE),
        "documents": [
            {
                "document_id": p.name,
                "path": str(p),
                "bytes": p.stat().st_size,
            }
            for p in files
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[BATCH] dataset={dataset_dir} docs={len(files)} run_id={run_id}", flush=True)

    prior_flag = os.environ.get("OCR_SECONDARY_REPAIR")
    os.environ.pop("OCR_SECONDARY_REPAIR", None)

    summary: Dict[str, Any] = {
        "run_id": run_id,
        "decision": "INCONCLUSIVE",
        "gt20": None,
        "processed": 0,
        "remaining": len(files),
        "errors": 0,
        "repair_triggered": 0,
        "docs_with_deltas": 0,
        "delta_fields_total": 0,
        "latency_ms_sum_off": 0.0,
        "latency_ms_sum_on": 0.0,
        "latency_ms_sum_primary": 0.0,
        "cohort_counts": {},
        "stop_reasons": [],
        "interrupted": False,
        "source_verified": {
            "note": "GT accuracy is scored only in gt20_reconfirm; "
            "385-image set has no source GT — no accuracy claim.",
        },
    }

    exit_code = 1
    try:
        if not skip_gt20:
            print("[BATCH] Reconfirming GT-20 via production DI entrypoint...")
            gt20 = reconfirm_gt20(out_dir)
            summary["gt20"] = {
                "decision": gt20.get("decision"),
                "totals": gt20.get("totals"),
                "stop_reasons": gt20.get("stop_reasons"),
            }
            if gt20.get("decision") != "PASS":
                summary["decision"] = "BLOCKED"
                summary["stop_reasons"] = list(gt20.get("stop_reasons") or [])
                _write_report(out_dir, summary)
                return 2
        else:
            print("[BATCH] Skipping GT-20 reconfirm (--skip-gt20)")
            cached = out_dir / "gt20_reconfirm.json"
            if cached.is_file():
                gt20 = json.loads(cached.read_text(encoding="utf-8"))
                summary["gt20"] = {
                    "decision": gt20.get("decision"),
                    "totals": gt20.get("totals"),
                    "stop_reasons": gt20.get("stop_reasons"),
                    "loaded_from_cache": True,
                }
                if gt20.get("decision") != "PASS":
                    summary["decision"] = "BLOCKED"
                    summary["stop_reasons"] = list(gt20.get("stop_reasons") or [])
                    _write_report(out_dir, summary)
                    return 2
            else:
                summary["stop_reasons"].append("gt20_reconfirm_missing_with_skip")
                summary["decision"] = "BLOCKED"
                _write_report(out_dir, summary)
                return 2

        completed = _load_completed(checkpoints_path) if resume else set()
        if completed:
            print(f"[BATCH] Resume: {len(completed)} docs already complete")

        for idx, path in enumerate(files, start=1):
            doc_id = path.name
            if doc_id in completed:
                summary["processed"] += 1
                summary["remaining"] = len(files) - summary["processed"]
                continue

            artifact_path = per_dir / f"{path.stem}.json"
            primary_cache = primary_dir / f"{path.stem}.json"
            row: Dict[str, Any] = {
                "document_id": doc_id,
                "path": str(path),
                "status": "error",
            }
            try:
                file_bytes = path.read_bytes()
                row["sha256"] = _sha256(file_bytes)
                print(
                    f"[BATCH] {idx}/{len(files)} primary OCR {doc_id[:48]} ...",
                    flush=True,
                )
                t_primary = time.perf_counter()
                primary = _primary_ocr_once(path, primary_cache)
                primary_ms = (time.perf_counter() - t_primary) * 1000
                summary["latency_ms_sum_primary"] += primary_ms
                text = primary.get("text") or ""
                cohort = _cohort_from_text(text, doc_id)
                summary["cohort_counts"][cohort] = (
                    summary["cohort_counts"].get(cohort, 0) + 1
                )

                t0 = time.perf_counter()
                off_res = _run_di(doc_id, file_bytes, text, "false")
                off_ms = (time.perf_counter() - t0) * 1000
                t1 = time.perf_counter()
                on_res = _run_di(doc_id, file_bytes, text, "true")
                on_ms = (time.perf_counter() - t1) * 1000
                summary["latency_ms_sum_off"] += off_ms
                summary["latency_ms_sum_on"] += on_ms

                overlay = ((on_res.get("totals") or {}).get("extra") or {}).get(
                    "secondary_repair_overlay"
                )
                deltas = _diff_qty(off_res, on_res)
                if overlay and overlay.get("applied_count", 0) > 0:
                    summary["repair_triggered"] += 1
                if deltas:
                    summary["docs_with_deltas"] += 1
                    summary["delta_fields_total"] += len(deltas)

                incompat = _incompatible_layout_edit(cohort, overlay)
                if incompat:
                    summary["stop_reasons"].append(f"{incompat}:{doc_id}")
                    row.update(
                        {
                            "status": "stopped",
                            "cohort": cohort,
                            "stop_reason": incompat,
                            "overlay_on": overlay,
                            "deltas": deltas,
                        }
                    )
                    artifact_path.write_text(
                        json.dumps(row, indent=2, default=str), encoding="utf-8"
                    )
                    _append_checkpoint(
                        checkpoints_path,
                        {
                            "document_id": doc_id,
                            "status": "stopped",
                            "reason": incompat,
                            "ts": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    summary["decision"] = "BLOCKED"
                    summary["processed"] += 1
                    summary["remaining"] = len(files) - summary["processed"]
                    _write_report(out_dir, summary)
                    return 3

                # Provenance: changed qty with overlay must list repair types.
                if deltas and overlay:
                    for entry in overlay.get("fields") or []:
                        if not entry.get("repair"):
                            reason = f"missing_provenance:{doc_id}"
                            summary["stop_reasons"].append(reason)
                            summary["decision"] = "BLOCKED"
                            row["status"] = "stopped"
                            row["stop_reason"] = reason
                            artifact_path.write_text(
                                json.dumps(row, indent=2, default=str), encoding="utf-8"
                            )
                            _append_checkpoint(
                                checkpoints_path,
                                {
                                    "document_id": doc_id,
                                    "status": "stopped",
                                    "reason": reason,
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                },
                            )
                            _write_report(out_dir, summary)
                            return 3

                row.update(
                    {
                        "status": "complete",
                        "cohort": cohort,
                        "primary_engine": primary.get("engine"),
                        "primary_text_len": primary.get("text_len"),
                        "primary_ms": primary_ms,
                        "off_ms": off_ms,
                        "on_ms": on_ms,
                        "overlay_on": overlay,
                        "di_override_on": (
                            (on_res.get("totals") or {}).get("extra") or {}
                        ).get("di_ocr_override"),
                        "deltas": deltas,
                        "off_line_item_count": len(off_res.get("line_items") or []),
                        "on_line_item_count": len(on_res.get("line_items") or []),
                        "off_totals": {
                            k: (off_res.get("totals") or {}).get(k)
                            for k in ("opening_qty", "closing_qty", "sales_qty", "receipts_qty")
                        },
                        "on_totals": {
                            k: (on_res.get("totals") or {}).get(k)
                            for k in ("opening_qty", "closing_qty", "sales_qty", "receipts_qty")
                        },
                    }
                )
                artifact_path.write_text(
                    json.dumps(row, indent=2, default=str), encoding="utf-8"
                )
                _append_checkpoint(
                    checkpoints_path,
                    {
                        "document_id": doc_id,
                        "status": "complete",
                        "cohort": cohort,
                        "deltas": len(deltas),
                        "overlay": bool(overlay),
                        "ts": datetime.now(timezone.utc).isoformat(),
                    },
                )
                summary["processed"] += 1
                summary["remaining"] = len(files) - summary["processed"]
                print(
                    f"[BATCH] {idx}/{len(files)} {doc_id[:40]} "
                    f"cohort={cohort} deltas={len(deltas)} "
                    f"overlay={bool(overlay)} "
                    f"primary_ms={primary_ms:.0f} on_ms={on_ms:.0f}"
                )
            except Exception as exc:
                summary["errors"] += 1
                row["status"] = "error"
                row["error"] = str(exc)[:500]
                row["traceback"] = traceback.format_exc()[-2000:]
                artifact_path.write_text(
                    json.dumps(row, indent=2, default=str), encoding="utf-8"
                )
                _append_checkpoint(
                    checkpoints_path,
                    {
                        "document_id": doc_id,
                        "status": "error",
                        "error": str(exc)[:300],
                        "ts": datetime.now(timezone.utc).isoformat(),
                    },
                )
                summary["processed"] += 1
                summary["remaining"] = len(files) - summary["processed"]
                print(f"[BATCH] ERROR {doc_id}: {exc}")
                # Continue after operational errors; GT stop conditions already gated.
                continue

        # Final decision for 385 soak (no source GT accuracy claim).
        if summary["stop_reasons"]:
            summary["decision"] = "BLOCKED"
            exit_code = 3
        elif summary["errors"] > 0 and summary["processed"] < len(files):
            summary["decision"] = "INCONCLUSIVE"
            summary["interrupted"] = True
            exit_code = 4
        elif summary["processed"] < len(files):
            summary["decision"] = "INCONCLUSIVE"
            summary["interrupted"] = True
            summary["stop_reasons"].append(
                f"incomplete:{summary['processed']}/{len(files)}"
            )
            exit_code = 4
        else:
            summary["decision"] = "PASS"
            exit_code = 0
    except KeyboardInterrupt:
        summary["decision"] = "INCONCLUSIVE"
        summary["interrupted"] = True
        summary["stop_reasons"].append("keyboard_interrupt")
        exit_code = 130
        print("[BATCH] Interrupted — writing partial summary")
    finally:
        if prior_flag is None:
            os.environ.pop("OCR_SECONDARY_REPAIR", None)
        else:
            os.environ["OCR_SECONDARY_REPAIR"] = prior_flag
        # Hard restore OFF for safety.
        os.environ.pop("OCR_SECONDARY_REPAIR", None)
        print(
            "[BATCH] OCR_SECONDARY_REPAIR restored OFF; enabled=",
            sr.secondary_repair_enabled(),
        )
        _write_report(out_dir, summary)

    print(f"[BATCH] DECISION {summary['decision']}")
    print(
        json.dumps(
            {
                "processed": summary["processed"],
                "remaining": summary["remaining"],
                "errors": summary["errors"],
                "repair_triggered": summary["repair_triggered"],
                "docs_with_deltas": summary["docs_with_deltas"],
                "decision": summary["decision"],
            }
        )
    )
    return exit_code


def _write_report(out_dir: Path, summary: Dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    gt = summary.get("gt20") or {}
    lines = [
        "# Secondary OCR repair — controlled / 385-document batch",
        "",
        f"**Run ID:** `{summary.get('run_id')}`",
        f"**Decision:** `{summary.get('decision')}`",
        f"**Interrupted:** `{summary.get('interrupted')}`",
        "",
        "## GT-20 reconfirm (source-verified accuracy)",
        "",
        f"- Decision: `{gt.get('decision')}`",
        f"- Totals: `{json.dumps(gt.get('totals') or {}, sort_keys=True)}`",
        f"- Stop reasons: `{gt.get('stop_reasons') or []}`",
        "",
        "## 385-document operational soak (no accuracy claim)",
        "",
        f"- Processed: **{summary.get('processed')}**",
        f"- Remaining: **{summary.get('remaining')}**",
        f"- Errors: **{summary.get('errors')}**",
        f"- Repair triggered (overlay): **{summary.get('repair_triggered')}**",
        f"- Docs with qty deltas: **{summary.get('docs_with_deltas')}**",
        f"- Delta fields total: **{summary.get('delta_fields_total')}**",
        f"- Primary OCR latency sum: **{summary.get('latency_ms_sum_primary'):.0f} ms**",
        f"- OFF path latency sum: **{summary.get('latency_ms_sum_off'):.0f} ms**",
        f"- ON path latency sum: **{summary.get('latency_ms_sum_on'):.0f} ms**",
        "",
        "### Cohorts",
        "",
        "```json",
        json.dumps(summary.get("cohort_counts") or {}, indent=2),
        "```",
        "",
        "### Stop reasons",
        "",
    ]
    stops = summary.get("stop_reasons") or []
    lines += [f"- `{s}`" for s in stops] or ["- (none)"]
    lines += [
        "",
        "## Notes",
        "",
        "- Accuracy metrics apply **only** to GT-20 reconfirm.",
        "- The 385-image archive has no source GT; metrics are repair triggers, "
        "deltas, errors, and latency only.",
        "- `OCR_SECONDARY_REPAIR` restored OFF after the run.",
        "",
    ]
    (out_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Secondary OCR repair controlled/full batch evaluation"
    )
    parser.add_argument(
        "--image-root",
        type=Path,
        default=None,
        help="Path to prerana / image dataset stock root (required for 385-doc run)",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Artifact run id (default: UTC timestamp)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip documents already marked complete in checkpoints.jsonl",
    )
    parser.add_argument(
        "--max-docs",
        type=int,
        default=None,
        help="Optional cap (e.g. 65). Omit with --all-385 for full set.",
    )
    parser.add_argument(
        "--all-385",
        action="store_true",
        help="Evaluate the full resolved dataset (no max-docs cap)",
    )
    parser.add_argument(
        "--skip-gt20",
        action="store_true",
        help="Skip GT-20 reconfirm (not recommended)",
    )
    parser.add_argument(
        "--gt20-only",
        action="store_true",
        help="Only reconfirm GT-20; do not run the image batch",
    )
    parser.add_argument(
        "--require-primary-engine",
        default=None,
        help=(
            "Abort unless OCR_ENGINE/settings select this primary "
            "(e.g. paddleocr-vl). Prevents silent Tesseract evaluation."
        ),
    )
    args = parser.parse_args(argv)

    run_id = args.run_id or _utc_run_id()
    out_dir = OUT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    prior = os.environ.get("OCR_SECONDARY_REPAIR")
    os.environ.pop("OCR_SECONDARY_REPAIR", None)
    try:
        if args.gt20_only:
            gt20 = reconfirm_gt20(out_dir)
            summary = {
                "run_id": run_id,
                "decision": gt20.get("decision"),
                "gt20": gt20,
                "processed": 0,
                "remaining": 0,
                "errors": 0,
                "repair_triggered": 0,
                "docs_with_deltas": 0,
                "delta_fields_total": 0,
                "latency_ms_sum_off": 0.0,
                "latency_ms_sum_on": 0.0,
                "latency_ms_sum_primary": 0.0,
                "cohort_counts": {},
                "stop_reasons": list(gt20.get("stop_reasons") or []),
                "interrupted": False,
            }
            _write_report(out_dir, summary)
            return 0 if gt20.get("decision") == "PASS" else 2

        if args.image_root is None:
            env_root = os.getenv("SPLITEXTRACT_CANARY_IMAGE_ROOT", "").strip()
            if env_root:
                args.image_root = Path(env_root)
            else:
                raise SystemExit(
                    "Missing --image-root (and SPLITEXTRACT_CANARY_IMAGE_ROOT is unset). "
                    "Pass the prerana / image dataset stock directory explicitly."
                )

        max_docs = None if args.all_385 else args.max_docs
        if not args.all_385 and max_docs is None:
            # Default safety cap from CONTROLLED_BATCH_PLAN unless --all-385.
            max_docs = 65
            print(
                "[BATCH] Neither --all-385 nor --max-docs set; "
                "defaulting to --max-docs 65. Pass --all-385 for full set."
            )

        return run_batch(
            image_root=args.image_root,
            run_id=run_id,
            resume=args.resume,
            max_docs=max_docs,
            skip_gt20=args.skip_gt20,
            require_primary_engine=args.require_primary_engine,
        )
    finally:
        if prior is None:
            os.environ.pop("OCR_SECONDARY_REPAIR", None)
        else:
            os.environ["OCR_SECONDARY_REPAIR"] = prior
        os.environ.pop("OCR_SECONDARY_REPAIR", None)


if __name__ == "__main__":
    raise SystemExit(main())
