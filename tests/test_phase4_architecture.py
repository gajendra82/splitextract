"""Phase 4 document intelligence architecture tests."""

from __future__ import annotations

import io
import os
import unittest
from typing import Any, Dict, Optional
from unittest import mock

import fitz

from services.document_intelligence.confidence import build_confidence
from services.document_intelligence.decisions import (
    EXTRACTION_ERROR,
    PROVIDER_ERROR,
    RECOVERED_AND_VERIFIED,
    UNRESOLVED,
    VERIFIED,
    decide_final_status,
)
from services.document_intelligence.pipeline import DocumentIntelligencePipeline
from services.document_intelligence.verification import independently_verify
from services.document_router import DocumentRoute, route_document
from services.evidence import DocumentEvidence, PageEvidence, TextToken, BBox
from services.extraction.parser_registry import default_registry
from services.hybrid_schema import parse_recovery_number
from services.layout.detector import understand_layout
from services.ocr.base import OCRResult, OCRWord
from services.ocr.orchestrator import OCROrchestrator, OCRSettings
from services.ocr.paddle_engine import PaddleEngine
from services.ocr.tesseract_engine import TesseractEngine


def _pdf(text: str) -> bytes:
    doc = fitz.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), text)
        return doc.tobytes()
    finally:
        doc.close()


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeEngine:
    def __init__(self, name: str, text: str, confidence: float = 0.9):
        self.name = name
        self._text = text
        self._confidence = confidence

    def available(self) -> bool:
        return True

    def recognize(self, image_bytes, *, page=1, metadata=None):
        return OCRResult(
            text=self._text,
            words=[
                OCRWord(
                    text=self._text.split()[0],
                    bbox=(1.0, 2.0, 3.0, 4.0),
                    confidence=self._confidence,
                    page=page,
                )
            ],
            confidence=self._confidence,
            page=page,
            engine=self.name,
            metadata=dict(metadata or {}),
        )


_DIGITAL_TEXT = (
    "STOCK AND SALES STATEMENT\n"
    "Product Opening Receipt Sales Closing\n"
    "AMOXICILLIN 10 0 2 8\n"
    "PARACETAMOL 20 5 3 22\n"
)


class DigitalPdfShortCircuitTests(unittest.TestCase):
    def test_digital_pdf_does_not_require_ocr(self):
        route = route_document(
            _pdf(_DIGITAL_TEXT),
            "digital.pdf",
            requested_product="sales_statement",
        )
        self.assertFalse(route.requires_ocr)
        self.assertEqual(route.pdf_kind, "digital")

    def test_pipeline_ocr_policy_short_circuits_native_pdf(self):
        payload = {
            "stockist_name": "ACME",
            "line_items": [],
            "totals": {"extra": {"extraction_method": "unit_test"}},
        }

        with mock.patch(
            "services.sales_statement_extractor.extract_sales_statement_core",
            return_value=dict(payload),
        ), mock.patch(
            "services.sales_statement_extractor.attach_phase1_architecture",
            side_effect=lambda result, *a, **k: {
                **result,
                "phase1": {
                    "validation": {"accepted": True, "failures": []},
                    "route": {},
                    "evidence": {},
                    "normalized": {},
                },
            },
        ), mock.patch(
            "services.sales_statement_extractor.maybe_apply_phase2_recovery",
            side_effect=lambda result, *a, **k: result,
        ):
            out = DocumentIntelligencePipeline().run(
                _pdf(_DIGITAL_TEXT),
                "native.pdf",
            )
        ocr = out["document_intelligence"]["ocr"]
        self.assertFalse(ocr.get("invoked_here"))
        self.assertEqual(ocr.get("reason"), "native_text_short_circuit")
        self.assertIn(
            out["document_intelligence"]["decision"]["status"],
            {VERIFIED, UNRESOLVED},
        )


class OCRAbstractionTests(unittest.TestCase):
    def test_tesseract_engine_interface(self):
        engine = TesseractEngine()
        self.assertEqual(engine.name, "tesseract")
        self.assertTrue(hasattr(engine, "recognize"))
        self.assertTrue(hasattr(engine, "available"))

    def test_paddle_engine_interface(self):
        engine = PaddleEngine()
        self.assertEqual(engine.name, "paddle")
        # Presence check must not import torch/paddleocr.
        self.assertIsInstance(engine.available(), bool)
        # Exercise the common OCRResult contract via a stub recognize path.
        with mock.patch.object(engine, "available", return_value=False):
            result = engine.recognize(_png(), page=1)
        self.assertEqual(result.engine, "paddle")
        self.assertIn("text", result.to_dict())
        self.assertIn("words", result.to_dict())
        self.assertIn("error", result.metadata)

    def test_ocr_result_contains_bbox_confidence_engine(self):
        result = _FakeEngine("tesseract", "Opening 40").recognize(_png())
        payload = result.to_dict()
        self.assertEqual(payload["engine"], "tesseract")
        self.assertIsNotNone(payload["confidence"])
        self.assertEqual(payload["words"][0]["bbox"], [1.0, 2.0, 3.0, 4.0])

    def test_valid_primary_does_not_run_fallback(self):
        fallback = _FakeEngine("paddle", "Opening 99 Purchase 1")
        orch = OCROrchestrator(
            settings=OCRSettings(
                primary_engine="tesseract",
                fallback_engine="paddle",
                ensemble_enabled=True,
            ),
            tesseract=_FakeEngine("tesseract", "Opening 40 Purchase 10"),
            paddle=fallback,
        )
        bundle = orch.recognize(_png(), force_secondary=True, low_confidence=True)
        self.assertFalse(bundle.disagreement)
        self.assertEqual(bundle.engines_used, ["tesseract"])
        self.assertIsNotNone(bundle.primary)
        self.assertIsNone(bundle.secondary)

    def test_ensemble_off_by_default(self):
        fallback = _FakeEngine("paddle", "other")
        orch = OCROrchestrator(
            settings=OCRSettings(
                primary_engine="tesseract",
                fallback_engine="paddle",
                ensemble_enabled=False,
            ),
            tesseract=_FakeEngine("tesseract", "ok"),
            paddle=fallback,
        )
        bundle = orch.recognize(_png(), force_secondary=True)
        self.assertIsNone(bundle.secondary)
        self.assertFalse(bundle.metadata.get("fallback_used"))
        self.assertEqual(bundle.engines_used, ["tesseract"])

    def test_image_route_can_use_tesseract_policy(self):
        route = route_document(
            _png(), "scan.png", requested_product="sales_statement"
        )
        self.assertEqual(route.file_kind, "image")
        self.assertTrue(route.requires_ocr or route.file_kind == "image")
        orch = OCROrchestrator(
            settings=OCRSettings(
                primary_engine="tesseract",
                fallback_engine="paddle",
                ensemble_enabled=False,
            ),
            tesseract=_FakeEngine("tesseract", "Sale 5"),
            paddle=_FakeEngine("paddle", "Sale 5"),
        )
        bundle = orch.recognize(_png())
        self.assertEqual(bundle.primary.engine, "tesseract")
        self.assertIn("tesseract", bundle.engines_used)


class StrategyAndLayoutTests(unittest.TestCase):
    def test_known_parser_preferred_for_pdf(self):
        route = DocumentRoute(
            document_type="sales_statement",
            file_kind="pdf",
            pdf_kind="digital",
            requires_ocr=False,
            confidence=0.95,
        )
        evidence = DocumentEvidence(
            document_id="d1", source_type="pdf", pages=[]
        )
        layout = understand_layout(route, evidence)
        selected = default_registry().select(route, layout)
        self.assertTrue(selected["known_parser_preferred"])
        self.assertEqual(selected["selected"]["kind"], "named_parser")

    def test_unknown_layout_reaches_generic(self):
        route = DocumentRoute(
            document_type="sales_statement",
            file_kind="unsupported",
            requires_ocr=False,
            confidence=0.1,
        )
        evidence = DocumentEvidence(
            document_id="d2",
            source_type="unsupported",
            pages=[
                PageEvidence(
                    page_number=1,
                    tokens=[
                        TextToken(
                            text="WeirdHeader",
                            confidence=0.9,
                            bbox=BBox(0, 0, 10, 10),
                            page_number=1,
                            source_engine="native",
                        )
                    ],
                )
            ],
        )
        layout = understand_layout(route, evidence)
        self.assertFalse(layout.known_layout)
        selected = default_registry().select(route, layout)
        names = [c["name"] for c in selected["candidates"]]
        self.assertIn("generic_structured", names)


class HybridIntegrityTests(unittest.TestCase):
    def test_null_does_not_become_zero(self):
        self.assertIsNone(parse_recovery_number(None, "sales_qty"))
        self.assertIsNone(parse_recovery_number("", "sales_qty"))
        self.assertIsNone(parse_recovery_number("-", "sales_qty"))

    def test_printed_zero_remains_zero(self):
        self.assertEqual(parse_recovery_number(0, "sales_qty"), 0.0)
        self.assertEqual(parse_recovery_number("0", "sales_qty"), 0.0)

    def test_dynamic_overwrite_fails_verification(self):
        result = {
            "dynamic_fields": {"sales_qty": 9},
            "stock_identity": {},
        }
        verification = independently_verify(
            result, validation={"accepted": True}
        )
        self.assertFalse(verification["passed"])
        self.assertIn("dynamic_overwrote_canonical", verification["issues"])


class DecisionModelTests(unittest.TestCase):
    def test_verified_requires_validation_and_verification(self):
        decision = decide_final_status(
            validation={"accepted": True},
            verification={"passed": True},
            recovery={},
        )
        self.assertEqual(decision["status"], VERIFIED)

    def test_provider_error_never_verified(self):
        decision = decide_final_status(
            validation={"accepted": True},
            verification={"passed": True},
            recovery={},
            provider_error="PROVIDER_ERROR:timeout",
        )
        self.assertEqual(decision["status"], PROVIDER_ERROR)
        self.assertFalse(decision["verified"])

    def test_extraction_error_status(self):
        decision = decide_final_status(
            validation={"accepted": False},
            verification={"passed": False},
            recovery={},
            extraction_error="parse_crash",
        )
        self.assertEqual(decision["status"], EXTRACTION_ERROR)

    def test_recovery_and_verified(self):
        decision = decide_final_status(
            validation={"accepted": True},
            verification={"passed": True},
            recovery={"applied": True, "accepted": True},
        )
        self.assertEqual(decision["status"], RECOVERED_AND_VERIFIED)

    def test_recovery_and_verified_from_phase2_status(self):
        """recovery_runner emits status=recovered; decision must recognize it."""
        decision = decide_final_status(
            validation={"accepted": True},
            verification={"passed": True},
            recovery={
                "status": "recovered",
                "failure_type": "STOCK_IDENTITY_FAILURE",
                "strategy": "targeted_stock_extraction",
                "attempts": 1,
            },
        )
        self.assertEqual(decision["status"], RECOVERED_AND_VERIFIED)
        self.assertTrue(decision["verified"])

    def test_unresolved_on_ocr_disagreement(self):
        decision = decide_final_status(
            validation={"accepted": True},
            verification={"passed": True},
            recovery={},
            ocr_disagreement=True,
        )
        self.assertEqual(decision["status"], UNRESOLVED)

    def test_confidence_business_cannot_mask_missing_evidence(self):
        conf = build_confidence(
            layout_confidence=0.9,
            mapping_confidence=0.9,
            ocr_confidence=0.2,
            evidence_confidence=0.2,
            schema_ok=True,
            business_ok=True,
            known_parser=True,
        )
        self.assertEqual(conf["overall"], "low")


class RecoveryOrchestrationTests(unittest.TestCase):
    def test_validator_failure_keeps_unresolved_without_false_pass(self):
        decision = decide_final_status(
            validation={
                "accepted": False,
                "primary_failure": "STOCK_IDENTITY_FAILURE",
            },
            verification={"passed": False, "reason": "stock_identity_failure"},
            recovery={"applied": False},
        )
        self.assertEqual(decision["status"], UNRESOLVED)
        self.assertFalse(decision["verified"])

    def test_recovery_candidate_must_revalidate(self):
        # Recovery applied but validation still rejected → not verified.
        decision = decide_final_status(
            validation={"accepted": False, "primary_failure": "SCHEMA_FAILURE"},
            verification={"passed": False},
            recovery={"applied": True, "accepted": False},
        )
        self.assertEqual(decision["status"], UNRESOLVED)
        self.assertFalse(decision["verified"])


class MappingSafetyTests(unittest.TestCase):
    def test_column_mapping_is_layout_scoped(self):
        from services.column_mapping import ColumnMapping

        # Ensure constructor still requires layout identity / scope fields.
        mapping = ColumnMapping(
            layout_id="op_pur_orec",
            printed_header="Sale",
            canonical_field="sales_qty",
            source="phase3",
            confidence=0.9,
        )
        self.assertEqual(mapping.layout_id, "op_pur_orec")
        self.assertEqual(mapping.canonical_field, "sales_qty")


if __name__ == "__main__":
    unittest.main()
