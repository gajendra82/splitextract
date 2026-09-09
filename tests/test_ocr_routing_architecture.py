"""OCR routing architecture: heavy-scan Tesseract-first, stockist priority, CPU guard."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as app_module


class FakePage:
    def __init__(self, page_count: int, native_text: str = ""):
        self.parent = SimpleNamespace(page_count=page_count)
        self._native_text = native_text

    def get_text(self, *_args, **_kwargs):
        return self._native_text


class HeavyScanRoutingTests(unittest.TestCase):
    def test_heavy_scan_does_not_skip_tesseract_without_stockist(self):
        page = FakePage(page_count=29, native_text="")
        self.assertFalse(
            app_module._should_skip_tesseract_for_heavy_scan(
                page, pdf_path="/tmp/Split_1_1_to_29.pdf", ocr_hint="",
            )
        )
        self.assertEqual(
            app_module._pdf_routing_type(page, pdf_path="/tmp/Split_1_1_to_29.pdf"),
            "image-only",
        )

    def test_10_page_image_only_attempts_tesseract(self):
        page = FakePage(page_count=10, native_text="")
        self.assertTrue(app_module._is_heavy_multipage_image_scan(page))
        self.assertFalse(
            app_module._should_skip_tesseract_for_heavy_scan(
                page, pdf_path="/tmp/Split_1_1_to_10.pdf", ocr_hint="",
            )
        )

    def test_hyd26_filename_still_requires_tesseract(self):
        page = FakePage(page_count=29, native_text="")
        rule = app_module._stockist_requires_tesseract_ocr(
            "/tmp/HYD-26-12345.pdf", "")
        self.assertEqual(rule, "hyd26_filename")
        self.assertFalse(
            app_module._should_skip_tesseract_for_heavy_scan(
                page, pdf_path="/tmp/HYD-26-12345.pdf", ocr_hint="",
            )
        )

    def test_bharath_hint_still_requires_tesseract(self):
        page = FakePage(page_count=15, native_text="")
        hint = "BHARATH MEDICAL DISTRIBUTORS InvoiceNo 123"
        with patch.object(
            app_module, "ocr_suggests_bharath_medical", return_value=True
        ):
            self.assertEqual(
                app_module._stockist_requires_tesseract_ocr(None, hint),
                "bharath_medical",
            )
            self.assertFalse(
                app_module._should_skip_tesseract_for_heavy_scan(
                    page, pdf_path=None, ocr_hint=hint,
                )
            )

    def test_small_pod_unchanged(self):
        page = FakePage(page_count=5, native_text="")
        self.assertFalse(app_module._is_heavy_multipage_image_scan(page))
        self.assertFalse(
            app_module._should_skip_tesseract_for_heavy_scan(page, ocr_hint="")
        )

    def test_typed_large_pdf_not_heavy_scan(self):
        page = FakePage(
            page_count=30,
            native_text="TAX INVOICE GSTIN 29AAAAA0000A1Z5 " + ("x" * 50),
        )
        self.assertFalse(app_module._is_heavy_multipage_image_scan(page))
        self.assertEqual(app_module._pdf_routing_type(page), "text")
        self.assertFalse(
            app_module._should_skip_tesseract_for_heavy_scan(page, ocr_hint="")
        )

    def test_vision_extractor_still_present_for_fallback(self):
        self.assertTrue(callable(app_module.extract_full_data_from_image_gemini))


class CpuGuardTests(unittest.TestCase):
    def test_cpu_guard_blocked_when_steal_high(self):
        with patch.object(
            app_module, "cpu_steal_blocks_heavy_tesseract", return_value=True
        ):
            self.assertTrue(
                app_module._cpu_guard_blocks_tesseract(None, "")
            )

    def test_cpu_guard_does_not_block_stockist(self):
        with patch.object(
            app_module, "cpu_steal_blocks_heavy_tesseract", return_value=True
        ):
            self.assertFalse(
                app_module._cpu_guard_blocks_tesseract(
                    "/tmp/HYD-26-99999.pdf", "",
                )
            )

    def test_cpu_guard_still_skips_heavy_scan_when_steal_high(self):
        with patch.object(
            app_module, "cpu_steal_blocks_heavy_tesseract", return_value=True
        ):
            self.assertTrue(
                app_module._cpu_guard_blocks_tesseract(
                    "/tmp/Split_1_1_to_29.pdf", "",
                )
            )


class TesseractConcurrencyTests(unittest.TestCase):
    def test_concurrency_cap_unchanged(self):
        self.assertEqual(app_module.MAX_TESSERACT_CONCURRENCY, 2)
        self.assertEqual(app_module.effective_ocr_pool_workers(1), 1)
        self.assertEqual(app_module.effective_ocr_pool_workers(2), 2)
        self.assertEqual(app_module.effective_ocr_pool_workers(8), 2)


if __name__ == "__main__":
    unittest.main()
