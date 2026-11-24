"""Day 4: cache LRU, concurrency isolation, failure recovery, schema batch."""

from __future__ import annotations

import io
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from PIL import Image

from services import sales_extraction_runtime as runtime
from services import sales_statement_extractor as sse
from services.hybrid_schema import accept_document, result_from_document
from services.ocr.bounded import OcrExecutionTimeout, call_bounded
from services.vertex_gemini_client import GeminiProviderError


def _png_bytes(color: tuple) -> bytes:
    image = Image.new("RGB", (8, 8), color=color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class PrimaryImageOcrLruTests(unittest.TestCase):
    def tearDown(self):
        sse._PRIMARY_IMAGE_OCR_CACHE.clear()

    def test_lru_keeps_recent_entries_instead_of_clearing_all(self):
        calls = []

        def fake_ocr(image, use_fallback=False):
            calls.append(1)
            return "TEXT", {
                "engine": "paddleocr-vl",
                "status": "ok",
                "unverified_fallback": False,
            }

        images = [_png_bytes((index, 0, 0)) for index in range(9)]
        with patch("services.ocr.runtime.primary_is_paddle_vl", return_value=True), patch(
            "services.ocr.runtime.ocr_pil_image_text", side_effect=fake_ocr
        ):
            for data in images:
                sse._ocr_image_to_text(data)
            self.assertEqual(len(calls), 9)
            self.assertEqual(len(sse._PRIMARY_IMAGE_OCR_CACHE), 8)
            # images[1] is still in the LRU window; must not re-run OCR.
            before = len(calls)
            text = sse._ocr_image_to_text(images[1])
            self.assertEqual(text, "TEXT")
            self.assertEqual(len(calls), before)
            # images[0] was evicted; one more OCR call is expected.
            sse._ocr_image_to_text(images[0])
            self.assertEqual(len(calls), before + 1)

    def test_same_bytes_second_call_is_cache_hit(self):
        calls = []

        def fake_ocr(image, use_fallback=False):
            calls.append(1)
            time.sleep(0.02)
            return "HIORA", {
                "engine": "paddleocr-vl",
                "status": "ok",
                "unverified_fallback": False,
            }

        data = _png_bytes((9, 9, 9))
        with patch("services.ocr.runtime.primary_is_paddle_vl", return_value=True), patch(
            "services.ocr.runtime.ocr_pil_image_text", side_effect=fake_ocr
        ):
            t0 = time.perf_counter()
            a = sse._ocr_image_to_text(data)
            cold = time.perf_counter() - t0
            t1 = time.perf_counter()
            b = sse._ocr_image_to_text(data, psm=4)
            warm = time.perf_counter() - t1
        self.assertEqual(a, b)
        self.assertEqual(calls, [1])
        self.assertLess(warm, cold)


class ConcurrentIsolationTests(unittest.TestCase):
    def test_ocr_text_override_does_not_leak_across_threads(self):
        from services.ocr.ocr_text_override import get_ocr_text_override, ocr_text_override

        seen = {}

        def worker(name: str, value: str):
            with ocr_text_override(value):
                time.sleep(0.05)
                seen[name] = get_ocr_text_override()

        threads = [
            threading.Thread(target=worker, args=("a", "ALPHA")),
            threading.Thread(target=worker, args=("b", "BETA")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(seen["a"], "ALPHA")
        self.assertEqual(seen["b"], "BETA")
        self.assertIsNone(get_ocr_text_override())

    def test_pipeline_reuses_process_orchestrator(self):
        from services.document_intelligence.pipeline import DocumentIntelligencePipeline
        from services.ocr.runtime import get_orchestrator, reset_orchestrator

        reset_orchestrator()
        shared = get_orchestrator()
        pipeline = DocumentIntelligencePipeline()
        self.assertIs(pipeline.ocr, shared)
        reset_orchestrator()


class FailureRecoveryTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_ocr_timeout_raises_explicit_error(self):
        def hang():
            time.sleep(1.0)
            return "late"

        with self.assertRaises(OcrExecutionTimeout):
            call_bounded(hang, timeout_s=0.05, label="day4")

    def test_gemini_429_is_provider_error_not_success(self):
        err = GeminiProviderError(429, "RESOURCE_EXHAUSTED")
        self.assertEqual(err.code, 429)
        self.assertIn("RESOURCE_EXHAUSTED", str(err))

    def test_provider_cooldown_retries_are_bounded(self):
        self.assertGreaterEqual(runtime.GEMINI_PROVIDER_429_MAX_RETRIES, 1)
        self.assertLessEqual(runtime.GEMINI_PROVIDER_429_MAX_RETRIES, 5)
        self.assertLessEqual(runtime.MAX_CONCURRENT_EXTRACTIONS, 8)

    def test_cancellation_stops_new_ocr_work(self):
        real = type(
            "P",
            (),
            {
                "image_to_string": staticmethod(lambda *a, **k: "x"),
                "image_to_data": staticmethod(lambda *a, **k: {"text": []}),
            },
        )()
        gated = runtime.wrap_pytesseract_module(real)
        runtime.start_sales_deadline("day4-cancel", limit_seconds=60)
        runtime.request_cancel_sales_extraction("day4-cancel")
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            gated.image_to_string(b"abc", config="--psm 6")

    def test_partial_batch_keeps_good_documents(self):
        good = {
            "line_items": [
                {
                    "product_name": "SPEMAN",
                    "opening_qty": None,
                    "sales_qty": 0,
                    "closing_qty": 4,
                    "extra": {"note": "kept"},
                }
            ],
            "totals": {"extra": {"extraction_method": "unit"}},
        }

        def work(kind: str):
            if kind == "bad":
                raise ValueError("malformed")
            return json.loads(json.dumps(good))

        outcomes = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(work, kind) for kind in ("good", "bad", "good")]
            for future in futures:
                try:
                    outcomes.append({"ok": True, "result": future.result()})
                except Exception as exc:
                    outcomes.append({"ok": False, "error": str(exc)})
        self.assertEqual(sum(1 for item in outcomes if item["ok"]), 2)
        self.assertEqual(sum(1 for item in outcomes if not item["ok"]), 1)
        for item in outcomes:
            if item["ok"]:
                row = item["result"]["line_items"][0]
                self.assertIsNone(row["opening_qty"])
                self.assertEqual(row["sales_qty"], 0)
                self.assertEqual(row["extra"]["note"], "kept")


class SchemaBatchStabilityTests(unittest.TestCase):
    def test_three_classes_survive_api_and_never_mark_verified(self):
        from app import build_split_extract_response_from_sales_statement

        cases = {
            "predefined": {
                "product_name": "SPEMAN",
                "packing": "60s",
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
            self.assertFalse(envelope["verified"], name)
            self.assertEqual(envelope["schema_class"], name)
            response = build_split_extract_response_from_sales_statement(
                sales_result=result_from_document(payload),
                source_filename=f"{name}.json",
                use_blob_storage=False,
            )
            encoded = json.dumps(response, ensure_ascii=False)
            self.assertNotIn('"verified": true', encoded.lower().replace(" ", ""))
            if name == "fully_dynamic":
                self.assertEqual(response["dynamic"], payload)
                self.assertEqual(response["invoices"], [])
            elif name == "hybrid":
                fields = response["invoices"][0]["extracted_data"]["data"]["line_items"]["items"][0][
                    "additional_fields"
                ]
                self.assertEqual(fields["note"], "हिंदी")
                self.assertEqual(fields["flags"], [None, False])
                self.assertEqual(fields["closing_qty"], 4.0)
            else:
                item = response["invoices"][0]["extracted_data"]["data"]["line_items"]["items"][0]
                self.assertEqual(item["product_description"], "SPEMAN")
                self.assertEqual(item["additional_fields"]["sales_qty"], 0.0)

    def test_invalid_json_rejected_without_fabricated_zeros(self):
        from services.recovery_runner import _parse_recovery_text

        self.assertIsNone(_parse_recovery_text("{"))
        self.assertIsNone(
            _parse_recovery_text('{"line_items":[{"fixed":{"opening_qty":"bad"}}]}')
        )


if __name__ == "__main__":
    unittest.main()
