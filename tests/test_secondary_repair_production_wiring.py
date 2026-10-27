"""End-to-end wiring: DI secondary repair → production extraction output.

Primary VL is stubbed from stored benchmarks (no live PaddleOCR-VL).
Secondary Tesseract runs on real source bytes when the flag is ON.
``extract_sales_statement_core`` runs for real on the ON path via DI
``ocr_text_override`` (HTML parse — no Gemini / no hung 1151 vision probe).
"""

from __future__ import annotations

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

from services.document_intelligence.pipeline import DocumentIntelligencePipeline
from services.ground_truth.schema import only_source_verified_fields
from services.ocr import secondary_repair as sr
from services.ocr.base import OCRResult
from services.ocr.orchestrator import OCRBundle, OCROrchestrator
from services.ocr.ocr_text_override import get_ocr_text_override, ocr_text_override

ROOT = Path(__file__).resolve().parents[1]
DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
GT = ROOT / "phase4" / "ground_truth_50" / "per_document"
VL = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"

DOC_1078 = "0000701078_2026_08_ZL_03_362_05092026080346.jpeg"
DOC_1151 = "0000701151_2026_08_ZL_14_361_03092026025650.jpg"
DOC_0275 = "0000700275_2026_08_ZL_18_374_04092026171615.pdf"
DOC_0084 = "0000700084_2026_08_ZL_06_8236_03092026155136.pdf"
DOC_0209 = "0000700209_2026_08_ZL_18_374_04092026060313.pdf"
DOC_0124 = "0000700124_2026_08_ZL_06_521_02092026204703.pdf"

# Hard timeout — never leave hung vision/OCR processes.
_PIPELINE_TIMEOUT_SEC = 120


def _vl_text(doc: str) -> str:
    payload = json.loads((VL / f"{doc}.json").read_text(encoding="utf-8"))
    return payload["pages"][0].get("text") or ""


def _gt_qty(doc: str, field_id: str) -> str:
    gt = json.loads((GT / f"{doc}.json").read_text(encoding="utf-8"))
    for f in only_source_verified_fields(gt):
        if f.get("field_id") == field_id:
            return str(f.get("expected_raw"))
    raise AssertionError(field_id)


class _StubOrchestrator(OCROrchestrator):
    """Return stored VL HTML as primary OCR; skip live VL."""

    def __init__(self, text_by_page: Dict[int, str]) -> None:
        self.settings = type(
            "S", (), {"primary_engine": "stub-vl", "__dict__": {"primary_engine": "stub-vl"}}
        )()
        self._text_by_page = text_by_page
        self._engines = {}

    def recognize(
        self,
        image_bytes: bytes,
        *,
        page: int = 1,
        force_secondary: bool = False,
        low_confidence: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OCRBundle:
        text = self._text_by_page.get(int(page)) or self._text_by_page.get(1) or ""
        return OCRBundle(
            primary=OCRResult(
                text=text,
                engine="stub-vl",
                page=page,
                metadata={"status": "success"},
            ),
            engines_used=["stub-vl"],
            metadata={"status": "success"},
        )


def _find_item(result: Dict[str, Any], product: str, packing_substr: str = ""):
    target = sr._norm_product_token(product)
    for item in result.get("line_items") or []:
        name = str(item.get("product_name") or item.get("name") or "")
        if sr._norm_product_token(name) != target and target not in sr._norm_product_token(
            name
        ):
            continue
        if packing_substr:
            pack = str(item.get("packing") or "")
            if packing_substr.upper() not in pack.upper().replace(" ", ""):
                continue
        return item
    return None


def _qty(item, field: str):
    if item is None:
        return None
    if field == "expiry_breakage_qty":
        extra = item.get("extra") or {}
        return extra.get("expiry_breakage_qty")
    return item.get(field)


@unittest.skipUnless(DATASET.is_dir(), "dataset root missing")
class OverlayUnitTests(unittest.TestCase):
    def test_overlay_applies_marked_cells_only(self):
        result = {
            "line_items": [
                {
                    "product_name": "AACTARIL SOAP",
                    "packing": "75GM",
                    "opening_qty": 82.0,
                    "closing_qty": 89.0,
                    "extra": {},
                },
                {
                    "product_name": "TALEKT CAPS.",
                    "packing": "60'S",
                    "opening_qty": 12.0,
                    "closing_qty": 0.0,
                    "extra": {"expiry_breakage_qty": 0.0},
                },
            ],
            "totals": {"opening_qty": 1.0, "closing_qty": 2.0, "extra": {}},
        }
        cells = [
            {
                "is_header": False,
                "product_key": "AACTARIL SOAP",
                "header": "Closing Balance",
                "value": "69",
                "packing": "75GM",
                "secondary_repair": "himalaya_row_anchor_ocr",
            },
            {
                "is_header": False,
                "product_key": "TALEKT CAPS.",
                "header": "Expiry Breakage",
                "value": "12",
                "packing": "60'S",
                "secondary_repair": "himalaya_row_anchor_ocr",
            },
            {
                "is_header": False,
                "product_key": "TOTAL",
                "header": "Op.Bal. Qty.",
                "value": "834578",
                "secondary_repair": "total_footer_ocr",
            },
            {
                "is_header": False,
                "product_key": "TOTAL",
                "header": "Closing Balance",
                "value": "775274",
                "secondary_repair": "total_footer_ocr",
            },
            {
                "is_header": False,
                "product_key": "AACTARIL SOAP",
                "header": "Op.Bal. Qty.",
                "value": "999",
            },
        ]
        out = sr.overlay_secondary_repairs_onto_result(result, cells)
        aact = _find_item(out, "AACTARIL SOAP")
        talekt = _find_item(out, "TALEKT CAPS.")
        self.assertEqual(_qty(aact, "closing_qty"), 69.0)
        self.assertEqual(_qty(aact, "opening_qty"), 82.0)
        self.assertEqual(_qty(talekt, "expiry_breakage_qty"), 12.0)
        self.assertEqual(out["totals"]["opening_qty"], 834578.0)
        self.assertEqual(out["totals"]["closing_qty"], 775274.0)

    def test_overlay_clears_empty_to_none_not_zero(self):
        result = {
            "line_items": [
                {
                    "product_name": "LIV-52 SYRUP",
                    "packing": "100ML",
                    "closing_qty": 162.0,
                    "extra": {},
                }
            ],
            "totals": {"extra": {}},
        }
        cells = [
            {
                "is_header": False,
                "product_key": "LIV-52 SYRUP",
                "packing": "100ML",
                "header": "Closing",
                "value": "",
                "secondary_repair": "strike_conflict_unresolved",
            }
        ]
        out = sr.overlay_secondary_repairs_onto_result(result, cells)
        item = _find_item(out, "LIV-52 SYRUP", "100ML")
        self.assertIsNone(_qty(item, "closing_qty"))

    def test_ocr_override_context(self):
        self.assertIsNone(get_ocr_text_override())
        with ocr_text_override("HELLO"):
            self.assertEqual(get_ocr_text_override(), "HELLO")
        self.assertIsNone(get_ocr_text_override())

    def test_statement_from_ocr_html_keeps_packings_distinct(self):
        html = (
            "<table><tr><td>Product Name</td><td>Packing</td><td>Closing</td></tr>"
            "<tr><td>LIV-52 SYRUP</td><td>100ML</td><td>162</td></tr>"
            "<tr><td>LIV-52 SYRUP</td><td>200ML</td><td>37</td></tr></table>"
        )
        stmt = sr.statement_from_ocr_html(html, filename="x.pdf", source_format="pdf")
        items = stmt["line_items"]
        self.assertEqual(len(items), 2)
        by_pack = {i["packing"]: i["closing_qty"] for i in items}
        self.assertEqual(by_pack["100ML"], 162.0)
        self.assertEqual(by_pack["200ML"], 37.0)


def _run_production_entrypoint(doc: str, flag: str) -> Dict[str, Any]:
    """``DocumentIntelligencePipeline.run`` — production extraction entrypoint.

    - Stub primary OCR only (stored VL HTML).
    - Flag ON: real ``extract_sales_statement_core`` via DI override (no Gemini).
    - Flag OFF: parse unrepaired VL HTML through the same helper without
      secondary repair (avoids live vision hangs; proves OFF skips overlay).
    - Vision APIs raise if accidentally invoked.
    """
    path = sr.resolve_source_path(doc)
    if path is None or not path.is_file():
        raise unittest.SkipTest(f"source missing {doc}")
    if not (VL / f"{doc}.json").is_file():
        raise unittest.SkipTest(f"VL missing {doc}")
    file_bytes = path.read_bytes()
    vl_text = _vl_text(doc)
    stub = _StubOrchestrator({1: vl_text})
    pipe = DocumentIntelligencePipeline(ocr=stub)

    def _vision_forbidden(*_a, **_k):
        raise AssertionError("paid vision must not run in wiring tests")

    env = {
        "OCR_SECONDARY_REPAIR": flag,
        "PHASE2_RECOVERY_ENABLED": "false",
        # Safety: never start Paddle VL workers inside wiring tests.
        "OCR_ENGINE": "tesseract",
    }

    def _call():
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch(
                "services.sales_extraction_runtime.sales_generate_content_via_vertex",
                side_effect=_vision_forbidden,
            ):
                with mock.patch(
                    "services.vertex_gemini_client.generate_content_via_vertex",
                    side_effect=_vision_forbidden,
                ):
                    if str(flag).strip().lower() in {"1", "true", "yes", "on"}:
                        # Real core — DI sets ocr_text_override when flag ON.
                        return pipe.run(file_bytes, doc)

                    # OFF: real core on unrepaired VL HTML (same post-processing
                    # as ON) without secondary repair / overlay.
                    import services.sales_statement_extractor as sse

                    real_core = sse.extract_sales_statement_core

                    def _core_off(b, n):
                        if get_ocr_text_override() is not None:
                            raise AssertionError("override must be unset when flag OFF")
                        with ocr_text_override(vl_text):
                            return real_core(b, n)

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
            return fut.result(timeout=_PIPELINE_TIMEOUT_SEC)
        except FuturesTimeout as exc:
            raise TimeoutError(
                f"pipeline timed out after {_PIPELINE_TIMEOUT_SEC}s for {doc} flag={flag}"
            ) from exc


@unittest.skipUnless((DATASET / DOC_1078).is_file(), "1078 missing")
class ProductionWiringE2ETests(unittest.TestCase):
    """Real DI entrypoint proofs per repair type + unresolved / ordinary docs."""

    def test_flag_off_skips_overlay_marker(self):
        result = _run_production_entrypoint(DOC_0084, "false")
        extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertNotIn("secondary_repair_overlay", extra)

    def test_flag_off_preserves_pre_repair_quantities_on_1078(self):
        result = _run_production_entrypoint(DOC_1078, "false")
        aact = _find_item(result, "AACTARIL SOAP")
        self.assertEqual(float(_qty(aact, "closing_qty")), 89.0)
        talekt = _find_item(result, "TALEKT CAPS.")
        self.assertEqual(float(_qty(talekt, "expiry_breakage_qty")), 0.0)
        extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertNotIn("secondary_repair_overlay", extra)

    def test_1078_himalaya_repairs_reach_final_output(self):
        """Repair type: himalaya Closing/Expiry — real core + overlay."""
        result = _run_production_entrypoint(DOC_1078, "true")
        extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertTrue(extra.get("di_ocr_override"), "core must use DI OCR override")
        aact = _find_item(result, "AACTARIL SOAP")
        talekt = _find_item(result, "TALEKT CAPS.")
        self.assertIsNotNone(aact, result.get("line_items"))
        self.assertEqual(float(_qty(aact, "closing_qty")), float(_gt_qty(DOC_1078, "aact_close")))
        self.assertIsNotNone(talekt, result.get("line_items"))
        self.assertEqual(
            float(_qty(talekt, "expiry_breakage_qty")),
            float(_gt_qty(DOC_1078, "talekt_exp")),
        )
        overlay = extra.get("secondary_repair_overlay")
        self.assertTrue(overlay and overlay.get("applied_count", 0) >= 2)

    def test_1151_total_footer_repairs_reach_final_output(self):
        """Repair type: TOTAL footer — real core + overlay. No live vision."""
        result = _run_production_entrypoint(DOC_1151, "true")
        totals = result.get("totals") or {}
        self.assertEqual(float(totals.get("opening_qty")), float(_gt_qty(DOC_1151, "tot_op")))
        self.assertEqual(float(totals.get("closing_qty")), float(_gt_qty(DOC_1151, "tot_cl")))
        aact = _find_item(result, "AACTARIL SOAP")
        if aact is not None:
            self.assertEqual(float(_qty(aact, "closing_qty")), float(_gt_qty(DOC_1151, "aact_cl")))
        overlay = (totals.get("extra") or {}).get("secondary_repair_overlay")
        self.assertTrue(overlay and overlay.get("applied_count", 0) >= 2)

    def test_0275_liv_printed_closing_reaches_final_output(self):
        """Repair type: LIV-52 100ML printed Closing — real core + overlay."""
        result = _run_production_entrypoint(DOC_0275, "true")
        item = _find_item(result, "LIV-52 SYRUP", "100ML")
        self.assertIsNotNone(item, result.get("line_items"))
        self.assertEqual(float(_qty(item, "closing_qty")), float(_gt_qty(DOC_0275, "liv100_cl")))
        # 200ML must stay aligned (not overwritten by 100ML repair).
        item200 = _find_item(result, "LIV-52 SYRUP", "200ML")
        if item200 is not None and _qty(item200, "closing_qty") is not None:
            self.assertEqual(float(_qty(item200, "closing_qty")), 37.0)
        overlay = ((result.get("totals") or {}).get("extra") or {}).get(
            "secondary_repair_overlay"
        )
        self.assertTrue(overlay and overlay.get("applied_count", 0) >= 1)

    def test_0084_ordinary_no_incorrect_overlay(self):
        off = _run_production_entrypoint(DOC_0084, "false")
        on = _run_production_entrypoint(DOC_0084, "true")
        off_names = {
            (i.get("product_name"), i.get("closing_qty"))
            for i in (off.get("line_items") or [])
        }
        on_names = {
            (i.get("product_name"), i.get("closing_qty"))
            for i in (on.get("line_items") or [])
        }
        if off_names or on_names:
            self.assertEqual(off_names, on_names)

    def test_0209_blank_aactaril_not_fabricated(self):
        result = _run_production_entrypoint(DOC_0209, "true")
        item = _find_item(result, "AACTARIL SOAP")
        if item is None:
            return
        for field in ("opening_qty", "closing_qty"):
            val = _qty(item, field)
            if val is not None:
                self.assertNotEqual(float(val), 69.0)

    def test_0124_unreadable_header_not_invented_as_qty(self):
        result = _run_production_entrypoint(DOC_0124, "true")
        overlay = ((result.get("totals") or {}).get("extra") or {}).get(
            "secondary_repair_overlay"
        )
        if overlay:
            for entry in overlay.get("fields") or []:
                self.assertNotEqual(entry.get("field"), "h_op")


@unittest.skipUnless(DATASET.is_dir(), "dataset root missing")
class WiringSafetyTests(unittest.TestCase):
    def test_override_prevents_duplicate_vision_on_1078(self):
        calls = {"n": 0}

        def _track(*_a, **_k):
            calls["n"] += 1
            raise AssertionError("vision should not be reached")

        path = sr.resolve_source_path(DOC_1078)
        if path is None or not path.is_file():
            raise unittest.SkipTest("1078 missing")
        vl = _vl_text(DOC_1078)
        pipe = DocumentIntelligencePipeline(ocr=_StubOrchestrator({1: vl}))
        with mock.patch.dict(
            os.environ,
            {"OCR_SECONDARY_REPAIR": "true", "PHASE2_RECOVERY_ENABLED": "false"},
            clear=False,
        ):
            with mock.patch(
                "services.sales_extraction_runtime.sales_generate_content_via_vertex",
                side_effect=_track,
            ):
                result = pipe.run(path.read_bytes(), DOC_1078)
        self.assertEqual(calls["n"], 0)
        self.assertTrue(
            ((result.get("totals") or {}).get("extra") or {}).get("di_ocr_override")
        )

    def test_native_digital_pdf_skips_secondary_even_if_flag_on(self):
        """Digital PDF short-circuit must not run secondary OCR."""
        # Minimal PDF with enough embedded text to be digital.
        try:
            import fitz
        except ImportError:
            raise unittest.SkipTest("pymupdf missing")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(
            (72, 72),
            "Product Name Opening Closing Receipt Issue Balance Stock Statement "
            "AACTARIL SOAP 10 20 0 0 20 TOTAL LINE 100 200",
        )
        pdf_bytes = doc.tobytes()
        doc.close()
        pipe = DocumentIntelligencePipeline(ocr=_StubOrchestrator({1: "<table></table>"}))
        with mock.patch.dict(os.environ, {"OCR_SECONDARY_REPAIR": "true"}, clear=False):
            with mock.patch(
                "services.ocr.secondary_repair.apply_secondary_repairs",
                side_effect=AssertionError("secondary must not run on native PDF"),
            ):
                with mock.patch(
                    "services.sales_statement_extractor.extract_sales_statement_core",
                    return_value={
                        "line_items": [],
                        "totals": {"extra": {}},
                    },
                ):
                    with mock.patch(
                        "services.sales_statement_extractor.maybe_apply_phase2_recovery",
                        side_effect=lambda r, *a, **k: r,
                    ):
                        with mock.patch(
                            "services.sales_statement_extractor.attach_phase1_architecture",
                            side_effect=lambda r, *a, **k: r,
                        ):
                            result = pipe.run(pdf_bytes, "native_stock.pdf")
        extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertNotIn("secondary_repair_overlay", extra)


if __name__ == "__main__":
    unittest.main()
