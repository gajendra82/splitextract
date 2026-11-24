"""GT-20 OFF/ON through DocumentIntelligencePipeline production entrypoint.

Stubs primary OCR with stored VL HTML (no live PaddleOCR-VL).
Runs real secondary Tesseract when flag=ON.
Runs real ``extract_sales_statement_core`` on the ON path via DI
``ocr_text_override`` (HTML parse — no Gemini). OFF path parses unrepaired
VL HTML without secondary repair/overlay.
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any, Dict, List, Optional
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
from services.ocr.ocr_text_override import get_ocr_text_override

GT_DIR = ROOT / "phase4" / "ground_truth_50" / "per_document"
VL_DIR = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"
OUT = ROOT / "phase4" / "secondary_repair_canary" / "production_wiring_eval"
DOC_TIMEOUT_SEC = 120


class _StubOrchestrator(OCROrchestrator):
    def __init__(self, text: str) -> None:
        self.settings = type(
            "S", (), {"primary_engine": "stub-vl", "__dict__": {"primary_engine": "stub-vl"}}
        )()
        self._text = text
        self._engines = {}

    def recognize(self, image_bytes, *, page=1, force_secondary=False, low_confidence=False, metadata=None):
        return OCRBundle(
            primary=OCRResult(
                text=self._text,
                engine="stub-vl",
                page=page,
                metadata={"status": "success"},
            ),
            engines_used=["stub-vl"],
            metadata={"status": "success"},
        )


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
            if field in item:
                val = item.get(field)
                cells.append(
                    {
                        "is_header": False,
                        "product_key": pk,
                        "packing": packing,
                        "header": header,
                        "value": ""
                        if val is None
                        else (
                            str(int(val))
                            if isinstance(val, float) and val == int(val)
                            else str(val)
                        ),
                    }
                )
            if field == "closing_qty" and field in item:
                val = item.get(field)
                cells.append(
                    {
                        "is_header": False,
                        "product_key": pk,
                        "packing": packing,
                        "header": "Closing",
                        "value": ""
                        if val is None
                        else (
                            str(int(val))
                            if isinstance(val, float) and val == int(val)
                            else str(val)
                        ),
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


def _run(doc: str, flag: str, vl_text: str, file_bytes: bytes) -> Dict[str, Any]:
    import services.sales_statement_extractor as sse

    pipe = DocumentIntelligencePipeline(ocr=_StubOrchestrator(vl_text))
    real_core = sse.extract_sales_statement_core

    def _vision_forbidden(*_a, **_k):
        raise RuntimeError("vision forbidden in wiring eval")

    def _call():
        with mock.patch.dict(
            os.environ,
            {
                "OCR_SECONDARY_REPAIR": flag,
                "PHASE2_RECOVERY_ENABLED": "false",
                "OCR_ENGINE": "tesseract",
            },
            clear=False,
        ):
            with mock.patch(
                "services.sales_extraction_runtime.sales_generate_content_via_vertex",
                side_effect=_vision_forbidden,
            ):
                if str(flag).strip().lower() in {"1", "true", "yes", "on"}:
                    # Production DI path: secondary repair + real core + overlay.
                    return pipe.run(file_bytes, doc)

                # OFF: same real core post-processing on unrepaired VL HTML,
                # without secondary repair / overlay (flag off ⇒ no DI override).
                from services.ocr.ocr_text_override import ocr_text_override

                def _core_off(_b, _n):
                    if get_ocr_text_override() is not None:
                        raise RuntimeError("DI must not set override when flag OFF")
                    with ocr_text_override(vl_text):
                        return real_core(_b, _n)

                with mock.patch(
                    "services.sales_statement_extractor.extract_sales_statement_core",
                    side_effect=_core_off,
                ):
                    with mock.patch(
                        "services.sales_statement_extractor.maybe_apply_phase2_recovery",
                        side_effect=lambda result, *a, **k: result,
                    ):
                        return pipe.run(file_bytes, doc)

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_call)
        try:
            return fut.result(timeout=DOC_TIMEOUT_SEC)
        except FuturesTimeout as exc:
            raise TimeoutError(f"{doc} flag={flag} exceeded {DOC_TIMEOUT_SEC}s") from exc


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    prior = os.environ.get("OCR_SECONDARY_REPAIR")
    docs = []
    stop = []
    totals = {
        "fields": 0,
        "off_correct": 0,
        "on_correct": 0,
        "verified_corrections": 0,
        "regressions": 0,
        "incorrect_on": 0,
    }
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
                continue
            vl_text = json.loads(vl_path.read_text(encoding="utf-8"))["pages"][0].get(
                "text"
            ) or ""
            file_bytes = src.read_bytes()
            off_res = _run(doc, "false", vl_text, file_bytes)
            on_res = _run(doc, "true", vl_text, file_bytes)
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
                # Incorrect repair: ON changed a value, is wrong, and overlay ran.
                if (
                    m1.get("predicted_raw") not in (None, "")
                    and not m1.get("correct")
                    and m0.get("predicted_raw") != m1.get("predicted_raw")
                ):
                    overlay_meta = ((on_res.get("totals") or {}).get("extra") or {}).get(
                        "secondary_repair_overlay"
                    )
                    if overlay_meta:
                        totals["incorrect_on"] += 1
                        stop.append(f"incorrect:{doc}:{f.get('field_id')}")
            docs.append(
                {
                    "document_id": doc,
                    "deltas": doc_deltas,
                    "overlay_on": ((on_res.get("totals") or {}).get("extra") or {}).get(
                        "secondary_repair_overlay"
                    ),
                    "di_override_on": ((on_res.get("totals") or {}).get("extra") or {}).get(
                        "di_ocr_override"
                    ),
                }
            )
            print(
                doc[:40],
                "deltas",
                len(doc_deltas),
                "overlay",
                bool(((on_res.get("totals") or {}).get("extra") or {}).get(
                    "secondary_repair_overlay"
                )),
                "di_override",
                bool(((on_res.get("totals") or {}).get("extra") or {}).get("di_ocr_override")),
            )
    finally:
        if prior is None:
            os.environ.pop("OCR_SECONDARY_REPAIR", None)
        else:
            os.environ["OCR_SECONDARY_REPAIR"] = prior

    decision = "WIRING VERIFIED — READY FOR 385-DOCUMENT EVALUATION"
    if stop or totals["regressions"] or totals["incorrect_on"]:
        decision = "BLOCKED — FIX REQUIRED"
    elif totals["verified_corrections"] < 5:
        decision = "INCONCLUSIVE — MORE EVIDENCE REQUIRED"
        stop.append(f"verified_corrections={totals['verified_corrections']}")

    summary = {
        "decision": decision,
        "totals": totals,
        "stop_reasons": stop,
        "documents": docs,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    lines = [
        "# Production wiring GT eval (DI production entrypoint)",
        "",
        f"**Decision:** `{decision}`",
        "",
        "Primary VL stubbed; secondary Tesseract real; "
        "ON path uses real `extract_sales_statement_core` via DI OCR override.",
        "",
        "```json",
        json.dumps(totals, indent=2),
        "```",
        "",
        "## Stop reasons",
        "",
    ]
    lines += [f"- `{s}`" for s in stop] or ["- (none)"]
    lines += ["", "## Corrections", ""]
    for d in docs:
        if d["deltas"]:
            lines.append(f"### `{d['document_id']}`")
            for delta in d["deltas"]:
                lines.append(
                    f"- `{delta['field_id']}`: `{delta['off']}` → `{delta['on']}` "
                    f"(expected `{delta['expected']}`)"
                )
            lines.append("")
    (OUT / "WIRING_EVAL_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print("DECISION", decision)
    print(json.dumps(totals))
    return 0 if decision.startswith("WIRING VERIFIED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
