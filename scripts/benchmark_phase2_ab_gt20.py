"""A/B benchmark: Phase 2 OFF vs ON on SOURCE_VERIFIED GT-20 documents.

Does not modify extraction logic. Scores only SOURCE_VERIFIED fields.
Never fabricates ground truth. Writes per-document JSON + aggregate report.

Usage:
  PYTHONPATH=. .venv/Scripts/python.exe scripts/benchmark_phase2_ab_gt20.py
  PYTHONPATH=. .venv/Scripts/python.exe scripts/benchmark_phase2_ab_gt20.py --limit 1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

from services.ground_truth.html_table import _normalize_product_key
from services.ground_truth.matching import exact_numeric_match, normalize_numeric, normalize_text
from services.ground_truth.metrics import aggregate_metrics, document_status
from services.ground_truth.schema import only_source_verified_fields, validate_ground_truth_document
from services.ocr.ocr_text_override import ocr_text_override
from services.sales_statement_extractor import (
    attach_phase1_architecture,
    extract_sales_statement,
    extract_sales_statement_core,
    maybe_apply_phase2_recovery,
)

SOURCE_DIR = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
GT_DIR = ROOT / "phase4" / "ground_truth_50"
VL_DIR = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"
SEED = GT_DIR / "seed_index_20.json"
OUT_DIR = ROOT / "phase4" / "phase2_ab_gt20"
PER_DIR = OUT_DIR / "per_document"

# Public ballpark pricing for gemini-2.5-flash-lite (USD / 1M tokens).
# Used only for estimates; recorded separately from measured tokens.
PRICE_IN_PER_M = 0.10
PRICE_OUT_PER_M = 0.40

_HEADER_TO_FIELD = (
    (re.compile(r"op\.?\s*bal|opening|op\.?\s*qty|op\.?\s*stock|\bop\b|\bob\b", re.I), "opening_qty"),
    (re.compile(r"receipt|receive|\bpur\b|purchase", re.I), "receipts_qty"),
    (re.compile(r"issue|sales?\s*qty|\bsales?\b|\bissue/sales", re.I), "sales_qty"),
    (re.compile(r"closing|cl\.?\s*stock|\bclstk\b|\bcb\b|closing balance", re.I), "closing_qty"),
    (re.compile(r"stock[\s\-]?out|ss\s*sp", re.I), "stock_out_qty"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(document_id: str) -> str:
    return document_id.replace("/", "_").replace("\\", "_").replace(" ", "_")


def _canonical_field(printed_header: str) -> Optional[str]:
    header = str(printed_header or "")
    for pattern, field in _HEADER_TO_FIELD:
        if pattern.search(header):
            # Prefer issue/sales over generic "stock" false positives.
            if field == "opening_qty" and re.search(r"closing|issue|sales|receipt", header, re.I):
                continue
            if field == "sales_qty" and re.search(r"closing|opening|receipt|value", header, re.I):
                if not re.search(r"issue|sales?\s*qty|\bsales?\b", header, re.I):
                    continue
            return field
    return None


def _find_line_item(items: List[Dict[str, Any]], product_key: str) -> Optional[Dict[str, Any]]:
    target = _normalize_product_key(product_key)
    if not target:
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _normalize_product_key(str(item.get("product_name") or ""))
        if not name:
            continue
        if name == target or target in name or name in target:
            return item
    return None


def _line_items(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    for statement in result.get("statements") or []:
        if isinstance(statement, dict):
            items.extend(i for i in (statement.get("line_items") or []) if isinstance(i, dict))
    return items


def _qty_from_item(item: Dict[str, Any], field: str) -> Any:
    if field == "stock_out_qty":
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        return extra.get("stock_out_qty", item.get("stock_out_qty"))
    return item.get(field)


def score_extraction(result: Optional[Dict[str, Any]], gt_fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Score SOURCE_VERIFIED fields against extraction line items / headers."""
    matches: List[Dict[str, Any]] = []
    items = _line_items(result) if isinstance(result, dict) else []
    stockist = normalize_text((result or {}).get("stockist_name") if result else "")
    company = normalize_text((result or {}).get("company_name") if result else "")
    product_names = {
        _normalize_product_key(str(i.get("product_name") or ""))
        for i in items
        if i.get("product_name")
    }

    for field in gt_fields:
        field_type = str(field.get("field_type") or "other")
        expected_raw = str(field.get("expected_raw") or "")
        product_key = field.get("product_key")
        printed_header = str(field.get("printed_header") or "")
        predicted_raw = None
        match_mode = None
        correct = False
        error_type = "MISSING"
        silent_wrong = False

        if field_type in {"quantity", "numeric", "price", "table_cell"} and product_key:
            item = _find_line_item(items, str(product_key))
            canonical = _canonical_field(printed_header)
            if item is not None and canonical:
                value = _qty_from_item(item, canonical)
                if value is not None:
                    predicted_raw = value
                    match_mode = f"extraction:{canonical}"
                    if exact_numeric_match(expected_raw, str(value)):
                        correct = True
                        error_type = None
                    else:
                        error_type = "WRONG_VALUE"
                        silent_wrong = True
            elif item is None:
                error_type = "MISSING_PRODUCT"
            else:
                error_type = "UNMAPPED_HEADER"
        elif field_type == "header":
            # Header presence is not rewritten by Phase 2; score via OCR evidence
            # when available, else via extraction method metadata.
            ocr = ""
            if isinstance(result, dict):
                extra = ((result.get("totals") or {}).get("extra") or {})
                ocr = str(extra.get("ocr_table_html") or extra.get("ocr_text") or "")
            if normalize_text(printed_header or expected_raw) in normalize_text(ocr):
                predicted_raw = printed_header or expected_raw
                match_mode = "ocr_header_presence"
                correct = True
                error_type = None
            else:
                error_type = "MISSING"
        elif field_type == "text":
            target = normalize_text(expected_raw)
            if target and (target in stockist or target in company or any(target in p for p in product_names)):
                predicted_raw = expected_raw
                match_mode = "extraction_text"
                correct = True
                error_type = None
            else:
                error_type = "MISSING"
        else:
            error_type = "UNSUPPORTED_FIELD_TYPE"

        matches.append(
            {
                "field_id": field.get("field_id"),
                "field_type": field_type,
                "printed_header": printed_header,
                "product_key": product_key,
                "expected_raw": expected_raw,
                "expected_normalized": field.get("expected_normalized"),
                "predicted_raw": predicted_raw,
                "match_mode": match_mode,
                "correct": correct,
                "error_type": error_type,
                "silent_wrong": silent_wrong,
                "critical": str(field.get("priority") or "").lower() == "high"
                and field_type in {"quantity", "numeric"},
                "priority": field.get("priority"),
            }
        )

    metrics = aggregate_metrics(matches)
    qty_matches = [m for m in matches if m.get("field_type") == "quantity"]
    product_keys = {
        str(f.get("product_key"))
        for f in gt_fields
        if f.get("product_key") and f.get("field_type") in {"quantity", "numeric", "table_cell"}
    }
    found_products = 0
    for key in product_keys:
        if _find_line_item(items, key) is not None:
            found_products += 1
    missing_values = sum(1 for m in matches if m.get("predicted_raw") is None)
    return {
        "matches": matches,
        "metrics": metrics,
        "document_status": document_status(matches),
        "quantity_accuracy": (
            (sum(1 for m in qty_matches if m.get("correct")) / len(qty_matches))
            if qty_matches
            else None
        ),
        "product_identity": {
            "gt_products": len(product_keys),
            "found_products": found_products,
            "recall": (found_products / len(product_keys)) if product_keys else None,
        },
        "missing_values": missing_values,
        "verified_fields": len(matches),
        "correct_fields": sum(1 for m in matches if m.get("correct")),
    }


def stock_equation_stats(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "accepted": None,
            "fail_count": None,
            "is_valid": None,
            "healthy_rows": 0,
            "failing_rows": 0,
        }
    phase1 = result.get("phase1") if isinstance(result.get("phase1"), dict) else {}
    validation = phase1.get("validation") if isinstance(phase1.get("validation"), dict) else {}
    extra = ((result.get("totals") or {}).get("extra") or {})
    items = _line_items(result)
    healthy = 0
    failing = 0
    for item in items:
        extra_row = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if extra_row.get("stock_identity_ok") is False:
            failing += 1
        elif extra_row.get("stock_identity_ok") is True:
            healthy += 1
    return {
        "accepted": validation.get("accepted"),
        "fail_count": extra.get("stock_identity_fail_count"),
        "is_valid": (extra.get("stock_validation") or {}).get("is_valid")
        if isinstance(extra.get("stock_validation"), dict)
        else None,
        "healthy_rows": healthy,
        "failing_rows": failing,
        "reasons": list(validation.get("reasons") or []),
    }


def healthy_row_diffs(off_result: Dict[str, Any], on_result: Dict[str, Any]) -> Dict[str, Any]:
    """Rows that were healthy under Phase2 OFF must keep the same quantities ON."""
    off_items = {
        _normalize_product_key(str(i.get("product_name") or "")): i
        for i in _line_items(off_result)
        if i.get("product_name")
    }
    on_items = {
        _normalize_product_key(str(i.get("product_name") or "")): i
        for i in _line_items(on_result)
        if i.get("product_name")
    }
    changed = []
    checked = 0
    fields = ("opening_qty", "receipts_qty", "sales_qty", "closing_qty", "stock_out_qty")
    for key, off_item in off_items.items():
        extra = off_item.get("extra") if isinstance(off_item.get("extra"), dict) else {}
        if extra.get("stock_identity_ok") is not True:
            continue
        checked += 1
        on_item = on_items.get(key)
        if on_item is None:
            changed.append({"product_name": off_item.get("product_name"), "error": "missing_in_on"})
            continue
        row_changes = {}
        for field in fields:
            left = _qty_from_item(off_item, field)
            right = _qty_from_item(on_item, field)
            if normalize_numeric(left) != normalize_numeric(right) and not (
                left is None and right is None
            ):
                if left != right:
                    row_changes[field] = {"off": left, "on": right}
        if row_changes:
            changed.append(
                {"product_name": off_item.get("product_name"), "changes": row_changes}
            )
    return {
        "healthy_rows_checked": checked,
        "healthy_rows_changed": len(changed),
        "unchanged": len(changed) == 0,
        "changes": changed,
    }


class GeminiCallTracker:
    """Patch Vertex generate_content wherever callers hold a reference.

    ``app`` imports ``generate_content_via_vertex`` by name at load time. Patching
    only ``services.vertex_gemini_client`` leaves ``app`` holding a prior wrap whose
    ``_orig`` was cleared on uninstall (``NoneType`` not callable on later docs).
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._orig = None
        self._patched_modules: List[Any] = []

    def install(self) -> None:
        import services.vertex_gemini_client as vertex

        modules = [vertex]
        try:
            import app as app_mod

            modules.append(app_mod)
        except Exception:
            pass

        # Prefer the live module function; fall back to app's bound copy.
        self._orig = getattr(vertex, "generate_content_via_vertex", None)
        if self._orig is None:
            for mod in modules:
                fn = getattr(mod, "generate_content_via_vertex", None)
                if callable(fn):
                    self._orig = fn
                    break
        if not callable(self._orig):
            raise RuntimeError("generate_content_via_vertex unavailable for tracking")

        tracker = self
        orig = self._orig

        def wrapped(model, payload, timeout=60, **kwargs):
            started = time.perf_counter()
            error = None
            response = None
            try:
                response = orig(model, payload, timeout=timeout, **kwargs)
                return response
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                usage = {}
                if isinstance(response, dict):
                    usage = response.get("usageMetadata") or {}
                elif response is not None:
                    if hasattr(response, "json") and callable(getattr(response, "json")):
                        try:
                            usage = (response.json() or {}).get("usageMetadata") or {}
                        except Exception:
                            usage = {}
                    if not usage:
                        usage = getattr(response, "usageMetadata", None) or {}
                    if hasattr(usage, "__dict__") and not isinstance(usage, dict):
                        usage = {
                            "promptTokenCount": getattr(usage, "prompt_token_count", 0),
                            "candidatesTokenCount": getattr(usage, "candidates_token_count", None)
                            or getattr(usage, "response_token_count", 0),
                        }
                prompt_tokens = int((usage or {}).get("promptTokenCount") or 0)
                out_tokens = int(
                    (usage or {}).get("candidatesTokenCount")
                    or (usage or {}).get("candidates_token_count")
                    or 0
                )
                gen_cfg = (payload or {}).get("generationConfig") or {}
                schema = gen_cfg.get("responseJsonSchema") or {}
                tracker.calls.append(
                    {
                        "model": model,
                        "timeout": timeout,
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                        "prompt_tokens": prompt_tokens,
                        "output_tokens": out_tokens,
                        "error": error,
                        "response_mime_type": gen_cfg.get("responseMimeType"),
                        "schema_title": schema.get("title") if isinstance(schema, dict) else None,
                        "part_kinds": [
                            (
                                "inline_data"
                                if ("inline_data" in part or "inlineData" in part)
                                else "text"
                            )
                            for content in ((payload or {}).get("contents") or [])
                            for part in (content.get("parts") or [])
                            if isinstance(part, dict)
                        ],
                    }
                )

        self._patched_modules = []
        for mod in modules:
            if hasattr(mod, "generate_content_via_vertex"):
                setattr(mod, "generate_content_via_vertex", wrapped)
                self._patched_modules.append(mod)

    def uninstall(self) -> None:
        if self._orig is None:
            return
        for mod in self._patched_modules:
            setattr(mod, "generate_content_via_vertex", self._orig)
        self._patched_modules = []
        self._orig = None

    def summary(self) -> Dict[str, Any]:
        prompt = sum(c.get("prompt_tokens") or 0 for c in self.calls)
        output = sum(c.get("output_tokens") or 0 for c in self.calls)
        latency = sum(c.get("latency_ms") or 0 for c in self.calls)
        est_cost = (prompt / 1_000_000.0) * PRICE_IN_PER_M + (output / 1_000_000.0) * PRICE_OUT_PER_M
        return {
            "gemini_calls": len(self.calls),
            "prompt_tokens": prompt,
            "output_tokens": output,
            "latency_ms": latency,
            "estimated_cost_usd": round(est_cost, 6),
            "pricing_note": (
                f"estimate using flash-lite ballpark "
                f"${PRICE_IN_PER_M}/1M in + ${PRICE_OUT_PER_M}/1M out"
            ),
            "calls": self.calls,
        }


def _set_phase2(enabled: bool) -> None:
    os.environ["PHASE2_RECOVERY_ENABLED"] = "true" if enabled else "false"


def load_stored_vl_text(document_id: str) -> Optional[str]:
    path = VL_DIR / f"{_safe_name(document_id)}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    chunks: List[str] = []
    for page in payload.get("pages") or []:
        if not isinstance(page, dict):
            continue
        if page.get("status") != "success":
            continue
        text = str(page.get("text") or "").strip()
        if text:
            chunks.append(text)
    return "\n\n".join(chunks) if chunks else None


def run_arm(
    file_bytes: bytes,
    filename: str,
    *,
    phase2: bool,
    live_ocr: bool,
    ocr_text: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], Optional[str]]:
    """Run one extraction arm.

    Default mode reuses stored PaddleOCR-VL page text for both OFF/ON so the
    only intentional variable is Phase 2. ``--live-ocr`` runs the full pipeline
    including live Paddle (slow; may timeout on CPU).
    """
    _set_phase2(phase2)
    tracker = GeminiCallTracker()
    tracker.install()
    started = time.perf_counter()
    error = None
    result = None
    mode = "live_ocr" if live_ocr else "stored_vl_ocr"
    try:
        if live_ocr:
            result = extract_sales_statement(file_bytes, filename)
        else:
            if not ocr_text:
                raise RuntimeError("stored_vl_ocr_text_missing")
            with ocr_text_override(ocr_text):
                result = extract_sales_statement_core(file_bytes, filename)
            if isinstance(result, dict):
                extra = result.setdefault("totals", {}).setdefault("extra", {})
                if isinstance(extra, dict):
                    extra["di_ocr_override"] = True
                    if "<table" in ocr_text.lower():
                        extra.setdefault("ocr_table_html", ocr_text[:20000])
                    else:
                        extra.setdefault("ocr_text", ocr_text[:20000])
                    extra.setdefault("ocr_source", "paddleocr-vl-stored")
                result = attach_phase1_architecture(result, file_bytes, filename)
                result = maybe_apply_phase2_recovery(result, file_bytes, filename)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        tracker.uninstall()
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    telemetry = tracker.summary()
    telemetry["wall_latency_ms"] = elapsed_ms
    telemetry["phase2_enabled"] = phase2
    telemetry["ocr_mode"] = mode
    if isinstance(result, dict):
        phase2_meta = result.get("phase2") if isinstance(result.get("phase2"), dict) else {}
        telemetry["phase2_status"] = phase2_meta.get("status")
        telemetry["phase2_attempts"] = phase2_meta.get("attempts")
        telemetry["phase2_failure_type"] = phase2_meta.get("failure_type")
        telemetry["extraction_method"] = (
            ((result.get("totals") or {}).get("extra") or {}).get("extraction_method")
        )
    return result, telemetry, error


def load_gt20(source_dir: Path) -> List[Dict[str, Any]]:
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    docs = []
    for document_id in seed.get("document_ids") or []:
        gt_path = GT_DIR / "per_document" / f"{document_id}.json"
        if not gt_path.is_file():
            docs.append(
                {
                    "document_id": document_id,
                    "error": f"missing_gt:{gt_path}",
                }
            )
            continue
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        ok, errors = validate_ground_truth_document(gt)
        if not ok:
            docs.append({"document_id": document_id, "error": f"invalid_gt:{errors}"})
            continue
        fields = only_source_verified_fields(gt)
        source = source_dir / document_id
        docs.append(
            {
                "document_id": document_id,
                "gt": gt,
                "fields": fields,
                "source_path": str(source),
                "source_exists": source.is_file(),
            }
        )
    return docs


def slim_result(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    items = []
    for item in _line_items(result)[:200]:
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        items.append(
            {
                "product_name": item.get("product_name"),
                "opening_qty": item.get("opening_qty"),
                "receipts_qty": item.get("receipts_qty"),
                "sales_qty": item.get("sales_qty"),
                "closing_qty": item.get("closing_qty"),
                "stock_out_qty": extra.get("stock_out_qty"),
                "stock_identity_ok": extra.get("stock_identity_ok"),
                "field_sources": extra.get("field_sources"),
                "recovery_original": extra.get("recovery_original"),
            }
        )
    extra = ((result.get("totals") or {}).get("extra") or {})
    return {
        "stockist_name": result.get("stockist_name"),
        "company_name": result.get("company_name"),
        "line_item_count": len(_line_items(result)),
        "line_items": items,
        "extraction_method": extra.get("extraction_method"),
        "di_ocr_override": extra.get("di_ocr_override"),
        "phase1_validation": (result.get("phase1") or {}).get("validation"),
        "phase2": result.get("phase2"),
    }


def compare_metrics(off_score: Dict[str, Any], on_score: Dict[str, Any]) -> Dict[str, Any]:
    def delta(a, b):
        if a is None or b is None:
            return None
        return b - a

    off_m = off_score.get("metrics") or {}
    on_m = on_score.get("metrics") or {}
    return {
        "field_accuracy_off": off_m.get("field_accuracy"),
        "field_accuracy_on": on_m.get("field_accuracy"),
        "field_accuracy_delta": delta(off_m.get("field_accuracy"), on_m.get("field_accuracy")),
        "quantity_accuracy_off": off_score.get("quantity_accuracy"),
        "quantity_accuracy_on": on_score.get("quantity_accuracy"),
        "quantity_accuracy_delta": delta(
            off_score.get("quantity_accuracy"), on_score.get("quantity_accuracy")
        ),
        "product_identity_recall_off": (off_score.get("product_identity") or {}).get("recall"),
        "product_identity_recall_on": (on_score.get("product_identity") or {}).get("recall"),
        "missing_values_off": off_score.get("missing_values"),
        "missing_values_on": on_score.get("missing_values"),
        "missing_values_delta": delta(off_score.get("missing_values"), on_score.get("missing_values")),
    }


def write_aggregate(rows: List[Dict[str, Any]], missing: List[str]) -> Dict[str, Any]:
    off_matches: List[Dict[str, Any]] = []
    on_matches: List[Dict[str, Any]] = []
    gemini_calls = 0
    gemini_success = 0
    gemini_failed = 0
    prompt_tokens = 0
    output_tokens = 0
    est_cost = 0.0
    healthy_changed_docs = 0
    stock_improved = 0
    stock_worsened = 0
    phase2_recovered = 0
    phase2_unresolved = 0
    phase2_not_needed = 0

    for row in rows:
        if row.get("error"):
            continue
        off_matches.extend((row.get("off") or {}).get("matches") or [])
        on_matches.extend((row.get("on") or {}).get("matches") or [])
        on_status = ((row.get("on") or {}).get("telemetry") or {}).get("phase2_status")
        if on_status == "recovered":
            phase2_recovered += 1
        elif on_status == "not_needed":
            phase2_not_needed += 1
        elif on_status:
            phase2_unresolved += 1
        for arm in ("off", "on"):
            tel = ((row.get(arm) or {}).get("telemetry") or {})
            gemini_calls += int(tel.get("gemini_calls") or 0)
            prompt_tokens += int(tel.get("prompt_tokens") or 0)
            output_tokens += int(tel.get("output_tokens") or 0)
            est_cost += float(tel.get("estimated_cost_usd") or 0)
            for call in tel.get("calls") or []:
                if not isinstance(call, dict):
                    continue
                if call.get("error"):
                    gemini_failed += 1
                else:
                    gemini_success += 1
        healthy = row.get("healthy_row_check") or {}
        if healthy.get("healthy_rows_changed"):
            healthy_changed_docs += 1
        off_fail = ((row.get("off") or {}).get("stock_equation") or {}).get("failing_rows")
        on_fail = ((row.get("on") or {}).get("stock_equation") or {}).get("failing_rows")
        if isinstance(off_fail, int) and isinstance(on_fail, int):
            if on_fail < off_fail:
                stock_improved += 1
            elif on_fail > off_fail:
                stock_worsened += 1

    def qty_acc(matches: List[Dict[str, Any]]) -> Optional[float]:
        qty = [m for m in matches if m.get("field_type") == "quantity"]
        if not qty:
            return None
        return sum(1 for m in qty if m.get("correct")) / float(len(qty))

    off_metrics = aggregate_metrics(off_matches) if off_matches else {}
    on_metrics = aggregate_metrics(on_matches) if on_matches else {}
    qty_off = qty_acc(off_matches)
    qty_on = qty_acc(on_matches)
    missing_off = sum(1 for m in off_matches if m.get("predicted_raw") is None)
    missing_on = sum(1 for m in on_matches if m.get("predicted_raw") is None)
    off_fa = off_metrics.get("field_accuracy")
    on_fa = on_metrics.get("field_accuracy")

    return {
        "created_at": _utc_now(),
        "documents_requested": 20,
        "documents_scored": len([r for r in rows if not r.get("error")]),
        "missing_sources": missing,
        "off_metrics": off_metrics,
        "on_metrics": on_metrics,
        "field_accuracy_off": off_fa,
        "field_accuracy_on": on_fa,
        "field_accuracy_delta": (on_fa - off_fa)
        if off_fa is not None and on_fa is not None
        else None,
        "quantity_accuracy_off": qty_off,
        "quantity_accuracy_on": qty_on,
        "quantity_accuracy_delta": (qty_on - qty_off)
        if qty_off is not None and qty_on is not None
        else None,
        "missing_values_off": missing_off,
        "missing_values_on": missing_on,
        "missing_values_delta": missing_on - missing_off,
        "comparison": {
            "field_accuracy_off": off_fa,
            "field_accuracy_on": on_fa,
            "field_accuracy_delta": (on_fa - off_fa)
            if off_fa is not None and on_fa is not None
            else None,
            "quantity_accuracy_off": qty_off,
            "quantity_accuracy_on": qty_on,
            "quantity_accuracy_delta": (qty_on - qty_off)
            if qty_off is not None and qty_on is not None
            else None,
            "missing_values_off": missing_off,
            "missing_values_on": missing_on,
            "missing_values_delta": missing_on - missing_off,
        },
        "stock_equation": {
            "docs_failing_rows_improved": stock_improved,
            "docs_failing_rows_worsened": stock_worsened,
        },
        "healthy_rows": {
            "docs_with_healthy_row_changes": healthy_changed_docs,
        },
        "gemini": {
            "total_calls": gemini_calls,
            "successful_calls": gemini_success,
            "failed_calls": gemini_failed,
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": round(est_cost, 6),
            "pricing_note": (
                f"estimate using flash-lite ballpark "
                f"${PRICE_IN_PER_M}/1M in + ${PRICE_OUT_PER_M}/1M out"
            ),
        },
        "phase2_outcomes": {
            "recovered": phase2_recovered,
            "not_needed": phase2_not_needed,
            "unresolved": phase2_unresolved,
        },
        "phase2_default_after_run": "false",
        "ocr_mode": None,
        "claims": {
            "accuracy_improvement_claimed": False,
            "note": "Deltas below are measured only; do not treat as production claims without review.",
        },
    }


def render_report(aggregate: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    lines = [
        "# Phase 2 A/B Benchmark — GT-20 SOURCE_VERIFIED",
        "",
        f"Created: {aggregate.get('created_at')}",
        "",
        "## Availability",
        f"- Documents requested: {aggregate.get('documents_requested')}",
        f"- Documents scored: {aggregate.get('documents_scored')}",
        f"- Missing sources: {aggregate.get('missing_sources') or 'none'}",
        f"- OCR mode: {aggregate.get('ocr_mode')}",
        "",
        "## Measured accuracy (SOURCE_VERIFIED fields only)",
        f"- Field accuracy OFF: {aggregate.get('field_accuracy_off')}",
        f"- Field accuracy ON: {aggregate.get('field_accuracy_on')}",
        f"- Field accuracy delta (ON-OFF): {aggregate.get('field_accuracy_delta')}",
        f"- Quantity accuracy OFF: {aggregate.get('quantity_accuracy_off')}",
        f"- Quantity accuracy ON: {aggregate.get('quantity_accuracy_on')}",
        f"- Quantity accuracy delta (ON-OFF): {aggregate.get('quantity_accuracy_delta')}",
        f"- Missing values OFF: {aggregate.get('missing_values_off')}",
        f"- Missing values ON: {aggregate.get('missing_values_on')}",
        "",
        "## Stock equations",
        f"- Docs with fewer failing rows ON: {(aggregate.get('stock_equation') or {}).get('docs_failing_rows_improved')}",
        f"- Docs with more failing rows ON: {(aggregate.get('stock_equation') or {}).get('docs_failing_rows_worsened')}",
        "",
        "## Healthy row integrity",
        f"- Docs where a Phase2-OFF healthy row changed under ON: {(aggregate.get('healthy_rows') or {}).get('docs_with_healthy_row_changes')}",
        "",
        "## Gemini usage",
        f"- Calls: {(aggregate.get('gemini') or {}).get('total_calls')}",
        f"- Successful calls: {(aggregate.get('gemini') or {}).get('successful_calls')}",
        f"- Failed calls: {(aggregate.get('gemini') or {}).get('failed_calls')}",
        f"- Prompt tokens: {(aggregate.get('gemini') or {}).get('prompt_tokens')}",
        f"- Output tokens: {(aggregate.get('gemini') or {}).get('output_tokens')}",
        f"- Estimated cost USD: {(aggregate.get('gemini') or {}).get('estimated_cost_usd')}",
        f"- Pricing note: {(aggregate.get('gemini') or {}).get('pricing_note')}",
        f"- Phase2 recovered docs: {(aggregate.get('phase2_outcomes') or {}).get('recovered')}",
        f"- Phase2 not_needed docs: {(aggregate.get('phase2_outcomes') or {}).get('not_needed')}",
        f"- Phase2 unresolved docs: {(aggregate.get('phase2_outcomes') or {}).get('unresolved')}",
        "",
        "## Claims",
        "- No accuracy improvement is claimed beyond the measured deltas above.",
        "- Phase 2 left disabled by default in process env after the run (`PHASE2_RECOVERY_ENABLED=false`).",
        "",
        "## Per-document summary",
        "",
        "| document_id | field_acc_off | field_acc_on | qty_acc_off | qty_acc_on | gemini_calls_on | healthy_unchanged | phase2_status |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        if row.get("error"):
            lines.append(
                f"| {row.get('document_id')} | error |  |  |  |  |  | {row.get('error')} |"
            )
            continue
        off_m = ((row.get("off") or {}).get("score") or {}).get("metrics") or {}
        on_m = ((row.get("on") or {}).get("score") or {}).get("metrics") or {}
        off_q = ((row.get("off") or {}).get("score") or {}).get("quantity_accuracy")
        on_q = ((row.get("on") or {}).get("score") or {}).get("quantity_accuracy")
        gem = ((row.get("on") or {}).get("telemetry") or {}).get("gemini_calls")
        healthy = (row.get("healthy_row_check") or {}).get("unchanged")
        status = ((row.get("on") or {}).get("telemetry") or {}).get("phase2_status")
        lines.append(
            "| {id} | {fo} | {fn} | {qo} | {qn} | {g} | {h} | {s} |".format(
                id=row.get("document_id"),
                fo=off_m.get("field_accuracy"),
                fn=on_m.get("field_accuracy"),
                qo=off_q,
                qn=on_q,
                g=gem,
                h=healthy,
                s=status,
            )
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Optional doc cap for smoke runs")
    parser.add_argument(
        "--source-dir",
        default=str(SOURCE_DIR),
        help="Directory containing the original ZL POD files",
    )
    parser.add_argument(
        "--live-ocr",
        action="store_true",
        help="Run live PaddleOCR in the full pipeline (slow; may timeout). Default uses stored VL OCR text for identical OFF/ON inputs.",
    )
    args = parser.parse_args()
    source_dir = Path(args.source_dir)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PER_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure process default ends disabled even if .env had true.
    _set_phase2(False)

    docs = load_gt20(source_dir)
    if args.limit and args.limit > 0:
        docs = docs[: args.limit]

    missing = [d["document_id"] for d in docs if not d.get("source_exists") and not d.get("error")]
    # Also treat missing source as error rows.
    rows: List[Dict[str, Any]] = []

    print(
        f"benchmark_start docs={len(docs)} source_dir={source_dir} live_ocr={args.live_ocr}",
        flush=True,
    )
    if missing:
        print(f"missing_sources={missing}", flush=True)

    for index, doc in enumerate(docs, start=1):
        document_id = doc["document_id"]
        print(f"[{index}/{len(docs)}] {document_id}", flush=True)
        if doc.get("error"):
            row = {"document_id": document_id, "error": doc["error"]}
            rows.append(row)
            (PER_DIR / f"{_safe_name(document_id)}.json").write_text(
                json.dumps(row, indent=2), encoding="utf-8"
            )
            continue
        if not doc.get("source_exists"):
            row = {
                "document_id": document_id,
                "error": f"missing_source:{doc.get('source_path')}",
            }
            rows.append(row)
            (PER_DIR / f"{_safe_name(document_id)}.json").write_text(
                json.dumps(row, indent=2), encoding="utf-8"
            )
            continue

        source_path = Path(doc["source_path"])
        file_bytes = source_path.read_bytes()
        fields = doc["fields"]
        stored_ocr = None if args.live_ocr else load_stored_vl_text(document_id)
        if not args.live_ocr and not stored_ocr:
            row = {
                "document_id": document_id,
                "error": "missing_stored_vl_ocr",
            }
            rows.append(row)
            (PER_DIR / f"{_safe_name(document_id)}.json").write_text(
                json.dumps(row, indent=2), encoding="utf-8"
            )
            continue

        off_result, off_tel, off_err = run_arm(
            file_bytes,
            document_id,
            phase2=False,
            live_ocr=args.live_ocr,
            ocr_text=stored_ocr,
        )
        on_result, on_tel, on_err = run_arm(
            file_bytes,
            document_id,
            phase2=True,
            live_ocr=args.live_ocr,
            ocr_text=stored_ocr,
        )
        _set_phase2(False)

        off_score = score_extraction(off_result, fields)
        on_score = score_extraction(on_result, fields)
        row = {
            "document_id": document_id,
            "source_path": str(source_path),
            "verified_fields": len(fields),
            "off": {
                "error": off_err,
                "telemetry": off_tel,
                "score": {
                    k: v
                    for k, v in off_score.items()
                    if k != "matches"
                },
                "matches": off_score.get("matches"),
                "stock_equation": stock_equation_stats(off_result),
                "result": slim_result(off_result),
            },
            "on": {
                "error": on_err,
                "telemetry": on_tel,
                "score": {
                    k: v
                    for k, v in on_score.items()
                    if k != "matches"
                },
                "matches": on_score.get("matches"),
                "stock_equation": stock_equation_stats(on_result),
                "result": slim_result(on_result),
            },
            "comparison": compare_metrics(off_score, on_score),
            "healthy_row_check": (
                healthy_row_diffs(off_result, on_result)
                if isinstance(off_result, dict) and isinstance(on_result, dict)
                else {"error": "missing_result"}
            ),
        }
        rows.append(row)
        out_path = PER_DIR / f"{_safe_name(document_id)}.json"
        out_path.write_text(json.dumps(row, indent=2, default=str), encoding="utf-8")
        print(
            "  off_field_acc={off} on_field_acc={on} gemini_on={g} healthy_unchanged={h}".format(
                off=(row["off"]["score"].get("metrics") or {}).get("field_accuracy"),
                on=(row["on"]["score"].get("metrics") or {}).get("field_accuracy"),
                g=on_tel.get("gemini_calls"),
                h=(row.get("healthy_row_check") or {}).get("unchanged"),
            ),
            flush=True,
        )

    aggregate = write_aggregate(rows, missing)
    aggregate["ocr_mode"] = "live_ocr" if args.live_ocr else "stored_vl_ocr_identical_inputs"
    aggregate["ocr_mode_note"] = (
        "Live PaddleOCR was not used by default because CPU OCR timed out during smoke "
        "testing. Both arms reuse the same stored PaddleOCR-VL page text so Phase 2 is "
        "the only intentional variable. Pass --live-ocr to force live OCR."
        if not args.live_ocr
        else "Live PaddleOCR full pipeline used for both arms."
    )
    (OUT_DIR / "aggregate.json").write_text(
        json.dumps({"aggregate": aggregate, "documents": [
            {
                "document_id": r.get("document_id"),
                "error": r.get("error"),
                "comparison": r.get("comparison"),
                "healthy_row_check": {
                    k: v
                    for k, v in (r.get("healthy_row_check") or {}).items()
                    if k != "changes"
                },
                "off_telemetry": (r.get("off") or {}).get("telemetry"),
                "on_telemetry": (r.get("on") or {}).get("telemetry"),
                "off_stock": (r.get("off") or {}).get("stock_equation"),
                "on_stock": (r.get("on") or {}).get("stock_equation"),
            }
            for r in rows
        ]}, indent=2, default=str),
        encoding="utf-8",
    )
    report = render_report(aggregate, rows)
    (OUT_DIR / "COMPARISON_REPORT.md").write_text(report, encoding="utf-8")
    _set_phase2(False)
    print("benchmark_done", OUT_DIR, flush=True)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
