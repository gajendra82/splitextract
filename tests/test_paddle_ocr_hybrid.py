"""Unit tests for optional PaddleOCR Tier-3 hybrid path (fail-soft, flagged OFF)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from services import paddle_ocr as paddle_mod


class PaddleOcrFlagTests(unittest.TestCase):
    def setUp(self):
        paddle_mod.reset_paddle_ocr_engine_for_tests()
        self._old = os.environ.get("PADDLE_OCR_ENABLED")

    def tearDown(self):
        paddle_mod.reset_paddle_ocr_engine_for_tests()
        if self._old is None:
            os.environ.pop("PADDLE_OCR_ENABLED", None)
        else:
            os.environ["PADDLE_OCR_ENABLED"] = self._old

    def test_disabled_by_default(self):
        os.environ.pop("PADDLE_OCR_ENABLED", None)
        self.assertFalse(paddle_mod.is_paddle_ocr_enabled())

    def test_enabled_true(self):
        os.environ["PADDLE_OCR_ENABLED"] = "true"
        self.assertTrue(paddle_mod.is_paddle_ocr_enabled())

    def test_extract_noop_when_disabled(self):
        os.environ["PADDLE_OCR_ENABLED"] = "false"
        text, conf = paddle_mod.extract_text_with_paddleocr(MagicMock(), page_num=0)
        self.assertIsNone(text)
        self.assertEqual(conf, 0.0)


class PaddleOcrParseTests(unittest.TestCase):
    def test_parse_classic_ocr_layout(self):
        raw = [[
            [[[0, 0], [1, 0], [1, 1], [0, 1]], ("TAX INVOICE", 0.97)],
            [[[0, 2], [1, 2], [1, 3], [0, 3]], ("INV NO 12345", 0.91)],
        ]]
        text, conf = paddle_mod._parse_ocr_result(raw)
        self.assertIn("TAX INVOICE", text)
        self.assertIn("INV NO 12345", text)
        self.assertGreater(conf, 90.0)

    def test_parse_predict_dict_layout(self):
        raw = [{
            "rec_texts": ["GSTIN", "Total 100.00"],
            "rec_scores": [0.88, 0.95],
        }]
        text, conf = paddle_mod._parse_ocr_result(raw)
        self.assertIn("GSTIN", text)
        self.assertIn("Total 100.00", text)
        self.assertGreater(conf, 80.0)

    def test_parse_empty(self):
        text, conf = paddle_mod._parse_ocr_result(None)
        self.assertEqual(text, "")
        self.assertEqual(conf, 0.0)


class ExtractScanOcrTextTests(unittest.TestCase):
    """Routing: Paddle → Tesseract fallback; flag OFF keeps Tesseract-only."""

    def setUp(self):
        paddle_mod.reset_paddle_ocr_engine_for_tests()
        self._old = os.environ.get("PADDLE_OCR_ENABLED")

    def tearDown(self):
        paddle_mod.reset_paddle_ocr_engine_for_tests()
        if self._old is None:
            os.environ.pop("PADDLE_OCR_ENABLED", None)
        else:
            os.environ["PADDLE_OCR_ENABLED"] = self._old

    def test_flag_off_uses_tesseract_only(self):
        os.environ["PADDLE_OCR_ENABLED"] = "false"
        import app as app_mod

        page = MagicMock()
        with patch.object(
            app_mod, "extract_text_with_paddleocr"
        ) as paddle_fn, patch.object(
            app_mod, "extract_text_with_tesseract", return_value=("OCR FROM TESS", 88.0)
        ):
            text, conf, method = app_mod.extract_scan_ocr_text(page, page_num=0)
            paddle_fn.assert_not_called()
            self.assertEqual(text, "OCR FROM TESS")
            self.assertEqual(conf, 88.0)
            self.assertEqual(method, "tesseract")

    def test_flag_on_paddle_success(self):
        os.environ["PADDLE_OCR_ENABLED"] = "true"
        import app as app_mod

        page = MagicMock()
        with patch.object(
            app_mod, "is_paddle_ocr_enabled", return_value=True
        ), patch.object(
            app_mod,
            "extract_text_with_paddleocr",
            return_value=("OCR FROM PADDLE\nTAX INVOICE", 92.0),
        ), patch.object(
            app_mod, "extract_text_with_tesseract"
        ) as tess_fn:
            text, conf, method = app_mod.extract_scan_ocr_text(page, page_num=0)
            tess_fn.assert_not_called()
            self.assertIn("PADDLE", text)
            self.assertEqual(method, "paddleocr")
            self.assertEqual(conf, 92.0)

    def test_flag_on_paddle_fail_falls_back_to_tesseract(self):
        os.environ["PADDLE_OCR_ENABLED"] = "true"
        import app as app_mod

        page = MagicMock()
        with patch.object(
            app_mod, "is_paddle_ocr_enabled", return_value=True
        ), patch.object(
            app_mod, "extract_text_with_paddleocr", return_value=(None, 10.0)
        ), patch.object(
            app_mod, "extract_text_with_tesseract", return_value=("TESS FALLBACK", 70.0)
        ):
            text, conf, method = app_mod.extract_scan_ocr_text(page, page_num=0)
            self.assertEqual(text, "TESS FALLBACK")
            self.assertEqual(method, "tesseract")
            self.assertEqual(conf, 70.0)

    def test_paddle_exception_fail_soft(self):
        os.environ["PADDLE_OCR_ENABLED"] = "true"
        import app as app_mod

        page = MagicMock()
        with patch.object(
            app_mod, "is_paddle_ocr_enabled", return_value=True
        ), patch.object(
            app_mod,
            "extract_text_with_paddleocr",
            side_effect=RuntimeError("boom"),
        ), patch.object(
            app_mod, "extract_text_with_tesseract", return_value=("SAFE TESS", 65.0)
        ):
            text, conf, method = app_mod.extract_scan_ocr_text(page, page_num=0)
            self.assertEqual(text, "SAFE TESS")
            self.assertEqual(method, "tesseract")

    def test_create_ocr_stats_includes_paddle(self):
        import app as app_mod

        stats = app_mod.create_ocr_stats()
        self.assertIn("paddleocr_success", stats)
        self.assertEqual(stats["paddleocr_success"], 0)


class OcrStatsContractTests(unittest.TestCase):
    def test_laravel_shape_keys_unchanged_in_enforce_schema_template(self):
        """Smoke: enforce_schema still exposes invoice_summary + line_items."""
        import app as app_mod

        out = app_mod.enforce_schema({
            "invoice_no": "X-1",
            "vendor": "Test Vendor",
            "customer": "Test Customer",
            "invoice_date": "2026-01-01",
            "total": "100.00",
            "tax": "0",
            "line_items": [],
            "ocr_text": "TAX INVOICE",
        })
        self.assertEqual(out["status"], "success")
        self.assertIn("invoice_summary", out["data"])
        self.assertIn("line_items", out["data"])
        self.assertIn("ocr_text", out["data"])


if __name__ == "__main__":
    unittest.main()
