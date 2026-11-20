"""Stock-statement OCR policy: reuse, one recovery, no hidden retry."""

from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from services import sales_extraction_runtime as runtime
from services import sales_statement_extractor as sse
from services.stock_ocr_policy import StockOcrCall, StockOcrPolicy


def _call(
    *,
    request_id: str = "req-stock",
    operation: str = "image_to_string",
    input_key: str,
    region_key: str = "page-1",
    page: str = "1",
    region: str = "page",
    cell: str = "-",
    reason: str | None = None,
    scope: str = "page",
) -> StockOcrCall:
    return StockOcrCall(
        request_id=request_id,
        operation=operation,
        input_key=input_key,
        region_key=region_key,
        page=page,
        region=region,
        cell=cell,
        reason=reason,
        scope=scope,
    )


class StockOcrPolicyTests(unittest.TestCase):
    def test_successful_ocr_is_reused(self):
        policy = StockOcrPolicy(max_recovery_attempts=1)
        calls = {"n": 0}

        def run():
            calls["n"] += 1
            return "OPENING 10"

        call = _call(input_key="img-psm6")
        with self.assertLogs("services.stock_ocr_policy", level="INFO") as logs:
            first = policy.execute(call, run)
            second = policy.execute(call, run)
        self.assertEqual(first, "OPENING 10")
        self.assertEqual(second, "OPENING 10")
        self.assertEqual(calls["n"], 1)
        joined = "\n".join(logs.output)
        self.assertIn("STOCK_OCR_REUSE", joined)
        self.assertIn("request_id=req-stock", joined)
        self.assertIn("page=1", joined)
        self.assertIn("elapsed_ms=", joined)

    def test_identical_ocr_input_is_not_executed_twice(self):
        policy = StockOcrPolicy(max_recovery_attempts=1)
        calls = {"n": 0}

        def run():
            calls["n"] += 1
            return "CLOSING 4"

        call = _call(input_key="same-crop-psm6", region_key="crop-a", region="crop")
        policy.execute(call, run)
        policy.execute(call, run)
        self.assertEqual(calls["n"], 1)

    def test_only_one_recovery_attempt(self):
        policy = StockOcrPolicy(max_recovery_attempts=1)
        calls = {"n": 0}

        def run():
            calls["n"] += 1
            return "" if calls["n"] == 1 else "ABANA 10"

        with self.assertLogs("services.stock_ocr_policy", level="INFO") as logs:
            policy.execute(_call(input_key="psm6"), run)
            recovered = policy.execute(_call(input_key="psm4"), run)
            reused = policy.execute(_call(input_key="psm7"), run)
        self.assertEqual(recovered, "ABANA 10")
        self.assertEqual(reused, "ABANA 10")
        self.assertEqual(calls["n"], 2)
        joined = "\n".join(logs.output)
        self.assertEqual(joined.count("STOCK_OCR_RETRY "), 1)
        self.assertIn("STOCK_OCR_REUSE", joined)
        self.assertIn("reason=unusable_ocr", joined)
        self.assertIn("policy_decision=region_shared_reuse", joined)

    def test_retry_stops_after_configured_limit(self):
        policy = StockOcrPolicy(max_recovery_attempts=0)
        calls = {"n": 0}

        def run():
            calls["n"] += 1
            return ""

        policy.execute(_call(input_key="initial"), run)
        again = policy.execute(
            _call(input_key="recovery", reason="header_detection_failed"),
            run,
        )
        self.assertEqual(again, "")
        self.assertEqual(calls["n"], 1)

    def test_column_mapping_failure_does_not_retry_usable_ocr(self):
        policy = StockOcrPolicy(max_recovery_attempts=1)
        calls = {"n": 0}

        def run():
            calls["n"] += 1
            return "OPENING RECEIPTS SALES CLOSING"

        policy.execute(_call(input_key="page-psm6"), run)
        reused = policy.execute(
            _call(input_key="page-psm4", reason="column_mapping"),
            run,
        )
        self.assertEqual(reused, "OPENING RECEIPTS SALES CLOSING")
        self.assertEqual(calls["n"], 1)

    def test_production_cascade_46_collapses_to_one_page_ocr(self):
        """request_id=8aa348956970 pattern: unique crops must not mint attempt=1 forever."""
        from services.stock_ocr_policy import resolve_stock_ocr_region_key

        policy = StockOcrPolicy(max_recovery_attempts=1)
        calls = {"n": 0}

        def run():
            calls["n"] += 1
            return "STOCK STATEMENT OPENING"

        page_key = resolve_stock_ocr_region_key(
            scope="page",
            page="1",
            region="page",
            operation="image_to_string",
            pixel_key="ignored",
        )
        with self.assertLogs("services.stock_ocr_policy", level="INFO") as logs:
            for i in range(46, 50):
                policy.execute(
                    _call(
                        request_id="8aa348956970",
                        input_key=f"sales_image_to_string_crop_{i}",
                        region_key=page_key,
                    ),
                    run,
                )
        self.assertEqual(calls["n"], 1)
        joined = "\n".join(logs.output)
        self.assertEqual(joined.count("STOCK_OCR_START"), 1)
        self.assertGreaterEqual(joined.count("STOCK_OCR_REUSE"), 3)
        self.assertIn("request_id=8aa348956970", joined)

    def test_multi_stage_extract_requests_share_page_budget(self):
        """header → column → semantic → numeric → validation must not re-OCR page."""
        from services.stock_ocr_policy import resolve_stock_ocr_region_key

        policy = StockOcrPolicy(max_recovery_attempts=1)
        calls = {"n": 0}

        def run():
            calls["n"] += 1
            return "" if calls["n"] == 1 else "RECOVERED PAGE"

        page_key = resolve_stock_ocr_region_key(
            scope="page", page="1", region="page",
            operation="image_to_string", pixel_key="x",
        )
        stages = [
            ("header", None),
            ("column", "unusable_ocr"),
            ("semantic", "column_mapping"),
            ("numeric", "validation_failed"),
            ("fallback", "identity_failure"),
        ]
        results = []
        for stage, reason in stages:
            results.append(
                policy.execute(
                    _call(
                        input_key=f"{stage}-crop",
                        region_key=page_key,
                        reason=reason,
                    ),
                    run,
                )
            )
        self.assertEqual(calls["n"], 2)
        self.assertEqual(results[0], "")
        self.assertEqual(results[1], "RECOVERED PAGE")
        self.assertEqual(results[2], "RECOVERED PAGE")
        self.assertEqual(results[3], "RECOVERED PAGE")
        self.assertEqual(results[4], "RECOVERED PAGE")

    def test_cell_recovery_does_not_ocr_the_page_again(self):
        policy = StockOcrPolicy(max_recovery_attempts=1)
        calls = {"n": 0}

        def run():
            calls["n"] += 1
            return "PAGE TEXT"

        policy.execute(
            _call(input_key="page-img", region_key="page-img", scope="page"),
            run,
        )
        replay = policy.execute(
            _call(
                input_key="page-img-again",
                region_key="page-img",
                scope="cell",
                region="cell",
                cell="10,20,30,40",
                reason="numeric_cell_missing",
            ),
            run,
        )
        cell = policy.execute(
            _call(
                input_key="cell-img",
                region_key="cell-img",
                scope="cell",
                region="cell",
                cell="10,20,30,40",
                reason="numeric_cell_missing",
            ),
            run,
        )
        self.assertEqual(replay, "")
        self.assertEqual(cell, "PAGE TEXT")
        self.assertEqual(calls["n"], 2)

    def test_timeout_does_not_trigger_hidden_retry(self):
        policy = StockOcrPolicy(max_recovery_attempts=1)
        calls = {"n": 0}

        def run():
            calls["n"] += 1
            raise TimeoutError("tesseract timed out")

        with self.assertLogs("services.stock_ocr_policy", level="INFO") as logs:
            with self.assertRaises(TimeoutError):
                policy.execute(_call(input_key="page-psm6", region_key="page"), run)
            with self.assertRaises(TimeoutError):
                policy.execute(
                    _call(
                        input_key="page-psm4",
                        region_key="page",
                        reason="header_detection_failed",
                    ),
                    run,
                )
            with self.assertRaises(TimeoutError):
                policy.execute(
                    _call(
                        input_key="other-crop",
                        region_key="other-crop",
                        operation="image_to_data",
                    ),
                    run,
                )
        self.assertEqual(calls["n"], 1)
        joined = "\n".join(logs.output)
        self.assertIn("STOCK_OCR_TIMEOUT", joined)
        self.assertIn("STOCK_OCR_RETRY_STOP", joined)
        self.assertNotIn("STOCK_OCR_RETRY ", joined)

    def test_reread_digits_refuses_full_page_crop(self):
        page = Image.new("L", (80, 80), 255)
        fake = SimpleNamespace(calls=0)

        def image_to_string(*_a, **_k):
            fake.calls += 1
            return "12"

        fake.image_to_string = image_to_string
        with patch.object(sse, "_a2z_tesseract", return_value=fake):
            full = sse._a2z_reread_digits(
                page, {"x0": 0, "y0": 0, "x1": 80, "y1": 80}
            )
            small = sse._a2z_reread_digits(
                page, {"x0": 10, "y0": 10, "x1": 28, "y1": 22}
            )
        self.assertEqual(full, "")
        self.assertEqual(small, "12")
        self.assertEqual(fake.calls, 1)


class StockOcrGateTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_gate_reuses_identical_input_and_stops_after_timeout(self):
        calls = {"n": 0}

        def fake_image_to_string(*_a, **_k):
            calls["n"] += 1
            return "OPENING 10"

        real = SimpleNamespace(
            image_to_string=fake_image_to_string,
            image_to_data=lambda *_a, **_k: {"text": []},
        )
        gated = runtime.wrap_pytesseract_module(real)
        runtime.start_sales_deadline("req-gate", limit_seconds=60)

        class FakeImage:
            def __init__(self, payload: bytes):
                self.size = (4, 4)
                self.mode = "L"
                self._payload = payload

            def tobytes(self):
                return self._payload

        img = FakeImage(b"\x02" * 32)
        with patch.object(runtime, "sales_tesseract_slot") as slot, patch.object(
            runtime, "run_bounded_sales_tesseract", side_effect=lambda fn, label: fn()
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):

            @contextmanager
            def _slot(_label="t"):
                yield

            slot.side_effect = _slot
            with self.assertLogs("services.stock_ocr_policy", level="INFO") as logs:
                first = gated.image_to_string(img, config="--psm 6")
                second = gated.image_to_string(img, config="--psm 6")
        self.assertEqual(first, second)
        self.assertEqual(calls["n"], 1)
        self.assertIn("STOCK_OCR_REUSE", "\n".join(logs.output))
        self.assertEqual(runtime.sales_ocr_calls(), 1)

        # Different crop of the same page must REUSE, not start OCR #2.
        other = FakeImage(b"\x03" * 32)
        with patch.object(runtime, "sales_tesseract_slot") as slot, patch.object(
            runtime, "run_bounded_sales_tesseract", side_effect=lambda fn, label: fn()
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):

            @contextmanager
            def _slot(_label="t"):
                yield

            slot.side_effect = _slot
            third = gated.image_to_string(other, config="--psm 4")
        self.assertEqual(third, "OPENING 10")
        self.assertEqual(calls["n"], 1)

        # Fresh request: timeout on first page OCR halts further attempts.
        from services.stock_ocr_policy import clear_stock_ocr_request

        clear_stock_ocr_request("req-gate")
        clear_stock_ocr_request("req-gate-timeout")
        runtime.clear_sales_deadline()
        runtime.start_sales_deadline("req-gate-timeout", limit_seconds=60)
        calls["n"] = 0

        def timeout_fn(*_a, **_k):
            calls["n"] += 1
            raise TimeoutError("hidden")

        real.image_to_string = timeout_fn
        real.image_to_data = timeout_fn
        timed = FakeImage(b"\x04" * 32)
        with patch.object(runtime, "sales_tesseract_slot") as slot, patch.object(
            runtime, "run_bounded_sales_tesseract", side_effect=lambda fn, label: fn()
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):

            @contextmanager
            def _slot(_label="t"):
                yield

            slot.side_effect = _slot
            with self.assertRaises(TimeoutError):
                gated.image_to_string(timed, config="--psm 6")
            with self.assertRaises(TimeoutError):
                gated.image_to_string(timed, config="--psm 4")
            with self.assertRaises(TimeoutError):
                gated.image_to_data(timed, config="--psm 6")
        self.assertEqual(calls["n"], 1)



class StockOcrCascadeGateTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()
        from services.stock_ocr_policy import clear_stock_ocr_request
        clear_stock_ocr_request("8aa348956970")
        clear_stock_ocr_request()

    def test_gated_unique_crops_collapse_under_stable_page_region(self):
        calls = {"n": 0}

        def fake_image_to_string(*_a, **_k):
            calls["n"] += 1
            return "OPENING RECEIPT ISSUE CLOSING"

        real = SimpleNamespace(
            image_to_string=fake_image_to_string,
            image_to_data=lambda *_a, **_k: {"text": []},
        )
        gated = runtime.wrap_pytesseract_module(real)
        runtime.start_sales_deadline("8aa348956970", limit_seconds=60)

        class FakeImage:
            def __init__(self, payload: bytes):
                self.size = (8, 8)
                self.mode = "L"
                self._payload = payload

            def tobytes(self):
                return self._payload

        with patch.object(runtime, "sales_tesseract_slot") as slot, patch.object(
            runtime, "run_bounded_sales_tesseract", side_effect=lambda fn, label: fn()
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):

            @contextmanager
            def _slot(_label="t"):
                yield

            slot.side_effect = _slot
            with self.assertLogs("services.stock_ocr_policy", level="INFO") as logs:
                for i in range(46, 52):
                    img = FakeImage(bytes([i & 0xFF]) * 64)
                    gated.image_to_string(img, config="--psm 6")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(runtime.sales_ocr_calls(), 1)
        joined = "\n".join(logs.output)
        self.assertEqual(joined.count("STOCK_OCR_START"), 1)
        self.assertGreaterEqual(joined.count("STOCK_OCR_REUSE"), 5)


class HandwrittenStockOcrNoCascadeTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()
        from services.stock_ocr_policy import clear_stock_ocr_request
        clear_stock_ocr_request()

    def test_handwritten_vision_uses_original_bytes_without_ocr_loop(self):
        from services.stock_direct_vision import try_stock_direct_vision
        from services.stock_ocr_policy import clear_stock_ocr_request, get_stock_ocr_policy

        clear_stock_ocr_request()
        get_stock_ocr_policy().clear()
        original = b"FAKEJPEG-HANDWRITTEN-BYTES-001"
        seen = {"calls": 0, "payloads": []}
        ocr_calls = {"n": 0}

        items = []
        for i in range(10):
            items.append(
                {
                    "product_name": f"BONNISAN DROPS {i + 1}",
                    "opening_qty": None,
                    "receipts_qty": None,
                    "sales_qty": i + 1,
                    "sales_value": None,
                    "closing_qty": None,
                    "closing_value": None,
                    "extra": {"confidence": "low"},
                }
            )
        payload = {
            "stockist_name": None,
            "company_name": None,
            "period_from": None,
            "period_to": None,
            "report_title": "ORDER FORM",
            "line_items": items,
        }

        def fake_generate(*_a, **kwargs):
            seen["calls"] += 1
            seen["payloads"].append(kwargs.get("payload"))
            return SimpleNamespace(
                json=lambda: {
                    "candidates": [
                        {"content": {"parts": [{"text": json.dumps(payload)}]}}
                    ]
                }
            )

        class CountingTess:
            def image_to_string(self, *a, **k):
                ocr_calls["n"] += 1
                return "Zandra ORDER FORM Qty"

            def image_to_data(self, *a, **k):
                ocr_calls["n"] += 1
                return {"text": []}

        runtime.start_sales_deadline("hw-cascade", limit_seconds=60)
        with patch(
            "services.sales_extraction_runtime.sales_generate_content_via_vertex",
            side_effect=fake_generate,
        ), patch(
            "services.stock_direct_vision._layout_header_text",
            return_value="Zandra ORDER FORM SAP Code Qty",
        ), patch.object(sse, "_a2z_tesseract", return_value=CountingTess()), patch(
            "services.stock_direct_vision.detect_stock_handwriting_signals",
            return_value={
                "handwritten": "true",
                "order_form": True,
                "saleret": False,
                "blue_ink_fraction": 0.2,
                "reasons": ["forced"],
                "header_chars": 40,
            },
        ):
            result = try_stock_direct_vision(
                original,
                "handwritten_order.jpg",
                ".jpg",
                peek_text="Zandra ORDER FORM SAP Code Product Pack Qty",
            )
        self.assertIsNotNone(result)
        self.assertGreaterEqual(len((result or {}).get("line_items") or []), 8)
        self.assertLessEqual(ocr_calls["n"], 1)
        self.assertLessEqual(runtime.sales_ocr_calls(), 1)
        self.assertGreaterEqual(seen["calls"], 1)
        inline = seen["payloads"][0]["contents"][0]["parts"][1]["inline_data"]
        import base64

        self.assertEqual(base64.b64decode(inline["data"]), original)



if __name__ == "__main__":
    unittest.main()
