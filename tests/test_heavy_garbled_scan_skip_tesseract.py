"""Heavy multipage image scans attempt Tesseract; page count must not skip it."""

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
    def test_29_page_image_scan_matches_classification(self):
        page = FakePage(page_count=29, native_text="")
        self.assertTrue(app_module._is_heavy_multipage_image_scan(page))

    def test_10_page_image_scan_matches_classification(self):
        page = FakePage(page_count=10, native_text="")
        self.assertTrue(app_module._is_heavy_multipage_image_scan(page))

    def test_small_pod_pdf_does_not_match(self):
        page = FakePage(page_count=9, native_text="")
        self.assertFalse(app_module._is_heavy_multipage_image_scan(page))

    def test_10_page_image_scan_does_not_skip_tesseract(self):
        page = FakePage(page_count=10, native_text="")
        self.assertFalse(
            app_module._should_skip_tesseract_for_heavy_scan(
                page, pdf_path="/tmp/Split_1_1_to_10.pdf", ocr_hint="",
            )
        )

    def test_29_page_image_scan_does_not_skip_tesseract(self):
        page = FakePage(page_count=29, native_text="")
        self.assertFalse(
            app_module._should_skip_tesseract_for_heavy_scan(
                page, pdf_path="/tmp/Split_1_1_to_29.pdf", ocr_hint="",
            )
        )

    def test_regression_page_count_must_not_skip_tesseract(self):
        """image-only >=10 pages must NOT skip_tesseract via heavy_multipage_scan."""
        for pages in (10, 29, 30):
            page = FakePage(page_count=pages, native_text="")
            self.assertTrue(
                app_module._is_heavy_multipage_image_scan(page),
                msg=f"{pages}-page image-only should still classify as heavy",
            )
            self.assertFalse(
                app_module._should_skip_tesseract_for_heavy_scan(
                    page, pdf_path=f"/tmp/Split_1_1_to_{pages}.pdf", ocr_hint="",
                ),
                msg=f"{pages}-page image-only must attempt Tesseract",
            )

    def test_stockist_hyd26_still_requires_tesseract(self):
        page = FakePage(page_count=29, native_text="")
        self.assertEqual(
            app_module._stockist_requires_tesseract_ocr(
                "/tmp/HYD-26-10001.pdf", ""),
            "hyd26_filename",
        )
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
        self.assertFalse(
            app_module._should_skip_tesseract_for_heavy_scan(page, ocr_hint="")
        )

    def test_uses_request_progress_total_pages(self):
        page = FakePage(page_count=1, native_text="")
        with patch.dict(
            app_module._request_progress,
            {"total_pages": 29},
            clear=False,
        ):
            self.assertTrue(app_module._is_heavy_multipage_image_scan(page))
            self.assertFalse(
                app_module._should_skip_tesseract_for_heavy_scan(
                    page, ocr_hint="",
                )
            )


if __name__ == "__main__":
    unittest.main()
