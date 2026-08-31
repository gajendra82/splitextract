"""Heavy multipage image scans skip Tesseract probe/OCR → Gemini Vision."""

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


class HeavyGarbledScanSkipTests(unittest.TestCase):
    def test_29_page_image_scan_matches(self):
        page = FakePage(page_count=29, native_text="")
        self.assertTrue(app_module._is_heavy_multipage_image_scan(page))

    def test_10_page_image_scan_matches(self):
        page = FakePage(page_count=10, native_text="")
        self.assertTrue(app_module._is_heavy_multipage_image_scan(page))

    def test_small_pod_pdf_does_not_match(self):
        page = FakePage(page_count=9, native_text="")
        self.assertFalse(app_module._is_heavy_multipage_image_scan(page))

    def test_stockist_hyd26_not_skipped_on_heavy_scan(self):
        page = FakePage(page_count=29, native_text="")
        self.assertFalse(
            app_module._should_skip_tesseract_for_heavy_scan(
                page, pdf_path="/tmp/HYD-26-10001.pdf", ocr_hint="",
            )
        )

    def test_large_pdf_with_native_text_does_not_match(self):
        page = FakePage(
            page_count=29,
            native_text="TAX INVOICE GSTIN 29AAAAA0000A1Z5 " + ("x" * 50),
        )
        self.assertFalse(app_module._is_heavy_multipage_image_scan(page))

    def test_uses_request_progress_total_pages(self):
        page = FakePage(page_count=1, native_text="")
        with patch.dict(
            app_module._request_progress,
            {"total_pages": 29},
            clear=False,
        ):
            self.assertTrue(app_module._is_heavy_multipage_image_scan(page))


if __name__ == "__main__":
    unittest.main()
