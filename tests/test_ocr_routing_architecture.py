"""OCR routing architecture: heavy-scan skip, stockist priority, CPU guard."""

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
    def test_heavy_scan_skips_tesseract_without_stockist(self):
        page = FakePage(page_count=29, native_text="")
        self.assertTrue(
            app_module._should_skip_tesseract_for_heavy_scan(
                page, pdf_path="/tmp/Split_1_1_to_29.pdf", ocr_hint="",
            )
        )

    def test_hyd26_filename_wins_over_heavy_scan_skip(self):
        page = FakePage(page_count=29, native_text="")
        rule = app_module._stockist_requires_tesseract_ocr(
            "/tmp/HYD-26-12345.pdf", "")
        self.assertEqual(rule, "hyd26_filename")
        self.assertFalse(
            app_module._should_skip_tesseract_for_heavy_scan(
                page, pdf_path="/tmp/HYD-26-12345.pdf", ocr_hint="",
            )
        )

    def test_bharath_hint_wins_over_heavy_scan_skip(self):
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


if __name__ == "__main__":
    unittest.main()
