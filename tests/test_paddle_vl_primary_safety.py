"""PaddleOCR-VL primary routing, evidence contract, and silent-wrong gates."""

from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from services.cell_validator import validate_critical_cells
from services.column_geometry import (
    CellSpan,
    HeaderSpan,
    map_cells_to_headers,
    reject_plausibility_only_assignment,
)
from services.column_mapping import ColumnMapping, SOURCE_LAYOUT
from services.document_intelligence.verification import independently_verify
from services.document_router import route_document
from services.failure_taxonomy import FailureType, classify_failure
from services.hybrid_schema import parse_recovery_number
from services.ocr.base import OCRResult, OCRWord
from services.ocr.contract import page_evidence_from_result
from services.ocr.orchestrator import OCROrchestrator, OCRSettings, ocr_settings
from services.ocr.runtime import reset_orchestrator


def _png(width: int = 32, height: int = 24) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


class _FakeEngine:
    def __init__(self, name: str, text: str = "ok", available: bool = True):
        self.name = name
        self._text = text
        self._available = available
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def recognize(self, image_bytes, *, page=1, metadata=None):
        self.calls += 1
        return OCRResult(
            text=self._text,
            words=[OCRWord(text=self._text, bbox=(1, 2, 3, 4), page=page)],
            confidence=0.9,
            page=page,
            engine=self.name,
            metadata={"status": "success", "model": self.name},
        )


class PrimaryRoutingTests(unittest.TestCase):
    def tearDown(self):
        reset_orchestrator()

    def test_allow_inprocess_prefers_inprocess_backend(self):
        from services.ocr.paddle_vl_engine import PaddleVLEngine

        engine = PaddleVLEngine()
        with mock.patch.dict(os.environ, {"PADDLE_VL_ALLOW_INPROCESS": "true"}, clear=False):
            with mock.patch.object(engine, "_specs_present", return_value=True):
                self.assertTrue(engine.available())
                self.assertEqual(engine.model_info().get("backend"), "inprocess")

    def test_worker_child_never_selects_subprocess(self):
        from services.ocr.paddle_vl_engine import PaddleVLEngine

        engine = PaddleVLEngine()
        with mock.patch.dict(
            os.environ,
            {
                "PADDLE_VL_WORKER_CHILD": "1",
                "PADDLE_VL_ALLOW_INPROCESS": "1",
            },
            clear=False,
        ):
            with mock.patch.object(engine, "_specs_present", return_value=True):
                self.assertTrue(engine.available())
                self.assertEqual(engine.model_info().get("backend"), "inprocess")
            # Recursive subprocess path is hard-blocked.
            result = engine._recognize_subprocess(
                b"x", page=1, metadata={}
            )
            self.assertEqual(
                (result.metadata or {}).get("error"),
                "recursive_subprocess_blocked",
            )

    def test_same_python_as_vl_venv_uses_inprocess(self):
        from services.ocr import paddle_vl_engine as mod
        from services.ocr.paddle_vl_engine import PaddleVLEngine

        engine = PaddleVLEngine()
        with mock.patch.object(
            engine, "_resolve_python", return_value=r"C:\vl\python.exe"
        ), mock.patch.object(
            mod, "_same_python", return_value=True
        ), mock.patch.object(
            engine, "_specs_present", return_value=True
        ), mock.patch.dict(
            os.environ,
            {"PADDLE_VL_ALLOW_INPROCESS": "", "PADDLE_VL_WORKER_CHILD": ""},
            clear=False,
        ):
            self.assertTrue(engine.available())
            self.assertEqual(engine.model_info().get("backend"), "inprocess")

    def test_default_primary_is_paddleocr_vl(self):
        with mock.patch.dict(
            os.environ,
            {
                "OCR_ENGINE": "",
                "OCR_FALLBACK_ENGINE": "",
                "OCR_ENSEMBLE_ENABLED": "false",
                "OCR_ALLOW_UNVERIFIED_FALLBACK": "false",
            },
            clear=False,
        ):
            settings = ocr_settings()
        self.assertEqual(settings.primary_engine, "paddleocr-vl")
        self.assertEqual(settings.fallback_engine, "gemini")
        self.assertFalse(settings.allow_unverified_fallback)

    def test_rollback_switch_to_tesseract(self):
        with mock.patch.dict(os.environ, {"OCR_ENGINE": "tesseract"}, clear=False):
            self.assertEqual(ocr_settings().primary_engine, "tesseract")

    def test_vl_primary_used_when_available(self):
        vl = _FakeEngine("paddleocr-vl-1.6", "Opening 10")
        tess = _FakeEngine("tesseract", "SHOULD_NOT_RUN")
        orch = OCROrchestrator(
            settings=OCRSettings(primary_engine="paddleocr-vl", allow_unverified_fallback=False),
            paddle_vl=vl,
            tesseract=tess,
        )
        bundle = orch.recognize(_png())
        self.assertEqual(vl.calls, 1)
        self.assertEqual(tess.calls, 0)
        self.assertEqual(bundle.primary.engine, "paddleocr-vl-1.6")
        self.assertFalse(bundle.unverified_fallback)

    def test_unavailable_primary_uses_labeled_fallback(self):
        vl = _FakeEngine("paddleocr-vl-1.6", available=False)
        gemini = _FakeEngine("gemini", "gemini text")
        orch = OCROrchestrator(
            settings=OCRSettings(
                primary_engine="paddleocr-vl",
                fallback_engine="gemini",
                allow_unverified_fallback=False,
            ),
            paddle_vl=vl,
            gemini=gemini,
        )
        bundle = orch.recognize(_png())
        self.assertEqual(vl.calls, 0)
        self.assertEqual(gemini.calls, 1)
        self.assertEqual(bundle.primary.text, "gemini text")
        self.assertEqual(bundle.primary.engine, "gemini")
        self.assertTrue(bundle.unverified_fallback)
        self.assertEqual(bundle.metadata.get("status"), "fallback")

    def test_explicit_fallback_is_flagged(self):
        vl = _FakeEngine("paddleocr-vl-1.6", available=False)
        tess = _FakeEngine("tesseract", "Opening 99")
        orch = OCROrchestrator(
            settings=OCRSettings(
                primary_engine="paddleocr-vl",
                fallback_engine="tesseract",
                allow_unverified_fallback=True,
            ),
            paddle_vl=vl,
            tesseract=tess,
        )
        bundle = orch.recognize(_png())
        self.assertEqual(tess.calls, 1)
        self.assertEqual(bundle.primary.engine, "tesseract")
        self.assertTrue(bundle.unverified_fallback)
        self.assertEqual(bundle.metadata.get("status"), "fallback")


class NativePathTests(unittest.TestCase):
    def test_digital_pdf_does_not_require_ocr(self):
        import fitz

        doc = fitz.open()
        try:
            page = doc.new_page()
            page.insert_text(
                (72, 72),
                "STOCK STATEMENT\nProduct Opening Receipt Sales Closing\n"
                "AMOX 10 0 2 8\n",
            )
            payload = doc.tobytes()
        finally:
            doc.close()
        route = route_document(payload, "digital.pdf", requested_product="sales_statement")
        self.assertFalse(route.requires_ocr)

    def test_image_requires_ocr(self):
        route = route_document(_png(), "scan.png", requested_product="sales_statement")
        self.assertTrue(route.requires_ocr)
        self.assertEqual(route.file_kind, "image")


class EvidenceContractTests(unittest.TestCase):
    def test_page_evidence_preserves_layout_without_fabricating_confidence(self):
        result = OCRResult(
            text="Opening 33",
            words=[OCRWord(text="Opening 33", bbox=(10, 20, 110, 40), confidence=None)],
            confidence=None,
            page=2,
            engine="paddleocr-vl-1.6",
            metadata={
                "status": "success",
                "model": "PaddleOCR-VL-1.6",
                "model_version": "v1.6",
                "layout": {
                    "available": True,
                    "regions": [
                        {
                            "text": "Opening 33",
                            "bbox": [10, 20, 110, 40],
                            "label": "table",
                            "order": 1,
                        }
                    ],
                },
                "tables": {"available": True, "items": [{"html": "<table></table>"}]},
                "reading_order": [1],
                "raw_keys": ["parsing_res_list"],
            },
        )
        page = page_evidence_from_result(result)
        payload = page.to_dict()
        self.assertEqual(payload["page"], 2)
        self.assertEqual(payload["engine"], "paddleocr-vl-1.6")
        self.assertIsNone(payload["confidence"]["mean"])
        self.assertFalse(payload["confidence"]["available"])
        self.assertEqual(payload["blocks"][0]["bbox"], [10.0, 20.0, 110.0, 40.0])
        self.assertTrue(payload["tables"])


class ColumnGeometryTests(unittest.TestCase):
    def test_spatial_mapping_and_ambiguity(self):
        mappings = (
            ColumnMapping(
                printed_header="Opening",
                canonical_field="opening_qty",
                source=SOURCE_LAYOUT,
                layout_id="demo",
            ),
            ColumnMapping(
                printed_header="Closing",
                canonical_field="closing_qty",
                source=SOURCE_LAYOUT,
                layout_id="demo",
            ),
        )
        headers = [
            HeaderSpan("Opening", 0, 40),
            HeaderSpan("Closing", 80, 120),
        ]
        cells = [
            CellSpan("10", 5, 35, row_index=0),
            CellSpan("8", 85, 115, row_index=0),
            CellSpan("99", 50, 90, row_index=0),  # overlaps both → ambiguous/unmapped
        ]
        report = map_cells_to_headers(headers, cells, mappings)
        statuses = {a.cell_text: a.status for a in report.assignments}
        self.assertEqual(statuses["10"], "mapped")
        self.assertEqual(statuses["8"], "mapped")
        self.assertIn(statuses["99"], {"ambiguous", "unmapped_preserved"})

    def test_reject_plausibility_only_critical_qty(self):
        self.assertTrue(
            reject_plausibility_only_assignment(
                canonical_field="opening_qty", source="stock_equation"
            )
        )
        self.assertFalse(
            reject_plausibility_only_assignment(
                canonical_field="opening_qty", source="spatial_header_alignment"
            )
        )


class NoGuessingTests(unittest.TestCase):
    def test_blank_is_null_zero_stays_zero(self):
        self.assertIsNone(parse_recovery_number("-", "opening_qty"))
        self.assertIsNone(parse_recovery_number("", "opening_qty"))
        self.assertEqual(parse_recovery_number("0", "opening_qty"), 0.0)
        self.assertEqual(parse_recovery_number(0, "closing_qty"), 0.0)

    def test_cell_validator_flags_blank_to_zero_and_glued_qty(self):
        result = {
            "line_items": [
                {
                    "product_name": "DEMO",
                    "opening_qty": "106100",
                    "extra": {"blank_coerced_to_zero": True},
                }
            ]
        }
        report = validate_critical_cells(result)
        cats = {i.category for i in report.issues}
        self.assertIn("INVALID_NORMALIZATION", cats)
        self.assertIn("NUMERIC_CONCATENATION", cats)

    def test_stock_identity_mismatch_marks_critical_unresolved(self):
        result = {
            "line_items": [
                {
                    "product_name": "AACTARIL SOAP",
                    "opening_qty": 82,
                    "sales_qty": 13,
                    "closing_qty": 89,
                    "extra": {"stock_identity_ok": False},
                }
            ]
        }
        report = validate_critical_cells(result)
        cats = {i.category for i in report.issues}
        self.assertIn("STOCK_IDENTITY_MISMATCH", cats)
        self.assertTrue(report.has_blocking_issues)

    def test_ocr_summary_text_length_not_empty_page(self):
        report = validate_critical_cells(
            {"line_items": []},
            ocr_evidence={
                "pages": [
                    {
                        "page": 1,
                        "status": "success",
                        "text": "",
                        "text_length": 4000,
                        "tables": {"available": True, "count": 1},
                    }
                ]
            },
        )
        cats = {i.category for i in report.issues}
        self.assertNotIn("EMPTY_OCR_PAGE", cats)

    def test_unverified_fallback_blocks_verification(self):
        verification = independently_verify(
            {"line_items": []},
            validation={"accepted": True, "checks": {}},
            unverified_ocr_fallback=True,
        )
        self.assertFalse(verification["passed"])
        self.assertIn("unverified_ocr_fallback", verification["issues"])


class FailureTaxonomyTests(unittest.TestCase):
    def test_column_confusion_classified(self):
        failure = classify_failure(
            {
                "accepted": False,
                "reasons": ["column_confusion"],
                "checks": {"column_mapping": False},
            }
        )
        self.assertEqual(failure, FailureType.COLUMN_CONFUSION)


if __name__ == "__main__":
    unittest.main()
