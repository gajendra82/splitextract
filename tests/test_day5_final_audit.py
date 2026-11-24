"""Day 5 final audit: deadline, multipage override, schema, reliability."""

from __future__ import annotations

import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from pydantic import ValidationError

from services.hybrid_schema import (
    HybridSchemaError,
    SalesLineFixed,
    accept_document,
    result_from_document,
)
from services.ocr.bounded import OcrExecutionTimeout, call_bounded
from services.ocr.ocr_text_override import get_ocr_text_override, ocr_text_override
from services import sales_extraction_runtime as runtime
from services.recovery_runner import _parse_recovery_text
from services.vertex_gemini_client import GeminiProviderError


class Day5DeadlineTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_default_sales_deadline_is_600_seconds(self):
        self.assertEqual(runtime.SALES_EXTRACTION_MAX_EXECUTION_SECONDS, 600)

    def test_deadline_is_enforced_on_gated_ocr(self):
        from contextlib import contextmanager
        from unittest.mock import patch

        real = type(
            "P",
            (),
            {
                "image_to_string": staticmethod(lambda *a, **k: "x"),
                "image_to_data": staticmethod(lambda *a, **k: {"text": []}),
            },
        )()
        gated = runtime.wrap_pytesseract_module(real)
        runtime.start_sales_deadline("day5-deadline", limit_seconds=60)
        # Deterministic expiry: do not depend on wall-clock sleep under load.
        runtime._deadline_local.deadline_mono = time.monotonic() - 1.0

        @contextmanager
        def _slot(_label="t"):
            yield

        with patch.object(runtime, "sales_tesseract_slot", side_effect=_slot), patch.object(
            runtime, "run_bounded_sales_tesseract", side_effect=lambda fn, label: fn()
        ):
            with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
                gated.image_to_string(b"abc", config="--psm 6")

    def test_sales_tesseract_slot_propagates_body_errors(self):
        with runtime.sales_tesseract_slot("day5-slot"):
            raised = False
            try:
                raise runtime.SalesExtractionDeadlineExceeded(
                    "day5-slot", "ocr", 0.01
                )
            except runtime.SalesExtractionDeadlineExceeded:
                raised = True
        self.assertTrue(raised)


class Day5MultipageOverrideTests(unittest.TestCase):
    def test_pipeline_preserves_page_order_for_ocr_pages(self):
        from services.document_intelligence.pipeline import DocumentIntelligencePipeline
        from services.ocr.base import OCRResult
        from services.ocr.orchestrator import OCRBundle

        seen = []

        class FakeOcr:
            settings = type(
                "S",
                (),
                {"primary_engine": "fake", "__dict__": {"primary_engine": "fake"}},
            )()

            def recognize(self, image_bytes, page=1, metadata=None, use_fallback=True):
                seen.append(int(page))
                return OCRBundle(
                    primary=OCRResult(
                        text=f"PAGE{page}",
                        words=[],
                        confidence=1.0,
                        page=page,
                        engine="fake",
                        metadata={"status": "ok"},
                    ),
                    engines_used=["fake"],
                    metadata={"status": "ok"},
                )

        with patch(
            "services.document_intelligence.pipeline._should_run_ocr",
            return_value=True,
        ), patch(
            "services.document_intelligence.pipeline._ocr_page_images",
            return_value=[(1, b"a"), (2, b"b"), (3, b"c")],
        ), patch(
            "services.sales_statement_extractor.extract_sales_statement_core",
            return_value={"line_items": [], "totals": {"extra": {}}},
        ), patch(
            "services.sales_statement_extractor.maybe_apply_phase2_recovery",
            side_effect=lambda result, file_bytes, name: result,
        ):
            DocumentIntelligencePipeline(ocr=FakeOcr()).run(b"%PDF", "multi.pdf")
        self.assertEqual(seen, [1, 2, 3])

    def test_unreadable_override_skips_crop_cascade(self):
        from services.sales_statement_extractor import extract_sales_statement_core

        html = (
            "<table><tr><td>0g</td><td>0tv</td><td>Sales 0tv</td><td>Closure 0tv</td></tr>"
            "<tr><td>ANTIBIOL</td><td>10</td><td>0</td><td>0</td><td>10</td></tr></table>"
        )
        with ocr_text_override(html), patch(
            "services.sales_statement_extractor._parse_pdf",
            side_effect=AssertionError("crop cascade must not run"),
        ), patch(
            "services.sales_statement_extractor._parse_image",
            side_effect=AssertionError("image cascade must not run"),
        ), patch(
            "services.gemini_extraction_fallback.maybe_apply_gemini_fallback",
            side_effect=AssertionError("gemini must not run"),
        ):
            result = extract_sales_statement_core(b"%PDF-1.4", "0000700124.pdf")
        self.assertIsInstance(result, dict)
        extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertTrue(extra.get("di_ocr_override") or extra.get("extraction_method"))


class Day5SchemaIntegrityTests(unittest.TestCase):
    def test_three_classes_and_no_false_verified(self):
        from app import build_split_extract_response_from_sales_statement

        cases = {
            "predefined": {
                "product_name": "SPEMAN",
                "packing": "60s",
                "opening_qty": None,
                "sales_qty": 0,
                "closing_qty": 4,
            },
            "hybrid": {
                "product_name": "SPEMAN",
                "closing_qty": 4,
                "note": "हिंदी",
                "flags": [None, False],
            },
            "fully_dynamic": {"rows": [{"sku": "α"}], "empty": [], "note": None},
        }
        for name, payload in cases.items():
            envelope = accept_document(payload)
            self.assertEqual(envelope["schema_class"], name)
            self.assertFalse(envelope["verified"])
            response = build_split_extract_response_from_sales_statement(
                sales_result=result_from_document(payload),
                source_filename=f"{name}.json",
                use_blob_storage=False,
            )
            if name == "fully_dynamic":
                self.assertEqual(response["dynamic"], payload)
                self.assertEqual(response["invoices"], [])
                self.assertFalse(response["verification"]["verified"])
            elif name == "hybrid":
                fields = response["invoices"][0]["extracted_data"]["data"]["line_items"][
                    "items"
                ][0]["additional_fields"]
                self.assertEqual(fields["note"], "हिंदी")
                self.assertEqual(fields["closing_qty"], 4.0)
            else:
                item = response["invoices"][0]["extracted_data"]["data"]["line_items"][
                    "items"
                ][0]
                self.assertEqual(item["additional_fields"]["sales_qty"], 0.0)
                self.assertIsNone(
                    accept_document(payload)["fixed"]["line_items"][0]["fixed"].get(
                        "opening_qty"
                    )
                )

    def test_invalid_known_values_do_not_become_zero(self):
        envelope = accept_document(
            {"product_name": "SPEMAN", "sales_qty": "not-a-number", "closing_qty": 4}
        )
        fixed = envelope["fixed"]["line_items"][0]["fixed"]
        self.assertNotIn("sales_qty", fixed)
        self.assertEqual(fixed["closing_qty"], 4.0)
        self.assertTrue(envelope["schema_errors"])
        with self.assertRaises(ValidationError):
            SalesLineFixed.model_validate({"unknown_column": 1})
        self.assertIsNone(_parse_recovery_text("{"))


class Day5ReliabilityTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_timeout_429_isolation_partial_batch(self):
        with self.assertRaises(OcrExecutionTimeout):
            call_bounded(lambda: time.sleep(1), timeout_s=0.05, label="day5")
        self.assertEqual(GeminiProviderError(503, "busy").code, 503)

        seen = {}

        def worker(key, value):
            with ocr_text_override(value):
                time.sleep(0.02)
                seen[key] = get_ocr_text_override()

        import threading

        threads = [
            threading.Thread(target=worker, args=("x", "X")),
            threading.Thread(target=worker, args=("y", "Y")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(seen, {"x": "X", "y": "Y"})

        outcomes = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(lambda k: (_ for _ in ()).throw(ValueError("bad")) if k == "bad" else {"ok": True}, kind)
                for kind in ("good", "bad", "good")
            ]
            for future in futures:
                try:
                    outcomes.append(future.result())
                except Exception:
                    outcomes.append(None)
        self.assertEqual(sum(1 for item in outcomes if item), 2)
        self.assertEqual(sum(1 for item in outcomes if item is None), 1)

    def test_orchestrator_reuse_and_cache_bound(self):
        from services.document_intelligence.pipeline import DocumentIntelligencePipeline
        from services.ocr.runtime import get_orchestrator, reset_orchestrator
        from services import sales_statement_extractor as sse

        reset_orchestrator()
        shared = get_orchestrator()
        self.assertIs(DocumentIntelligencePipeline().ocr, shared)
        self.assertEqual(sse._PRIMARY_IMAGE_OCR_CACHE_MAX, 8)
        reset_orchestrator()


if __name__ == "__main__":
    unittest.main()
