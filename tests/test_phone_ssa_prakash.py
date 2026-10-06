"""Phone PDF STOCK & SALES ANALYSIS — SSA reader routing and fallback guard."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from PIL import Image

from services.gemini_extraction_fallback import maybe_apply_gemini_fallback
from services.sales_statement_extractor import (
    _is_phone_tall_document_screenshot,
    _phone_document_viewer_content_jpeg,
    _peek_suggests_ssa_qty_value,
    _ssa_qty_value_opening_count,
)


def _tall_phone_jpeg() -> bytes:
    image = Image.new("RGB", (1080, 2408), (40, 40, 40))
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


class TestPhoneSsaHelpers(unittest.TestCase):
    def test_peek_suggests_ssa_from_analysis_title(self):
        self.assertTrue(
            _peek_suggests_ssa_qty_value("STOCK & SALES ANALYSIS OPENING QTY")
        )
        self.assertFalse(_peek_suggests_ssa_qty_value("Product Stock Report"))

    def test_tall_phone_aspect_detected(self):
        self.assertTrue(_is_phone_tall_document_screenshot(_tall_phone_jpeg()))
        wide = io.BytesIO()
        Image.new("RGB", (1200, 900), (255, 255, 255)).save(wide, format="JPEG")
        self.assertFalse(_is_phone_tall_document_screenshot(wide.getvalue()))

    def test_content_crop_strips_toolbars(self):
        cropped = _phone_document_viewer_content_jpeg(_tall_phone_jpeg())
        self.assertIsNotNone(cropped)
        out = Image.open(io.BytesIO(cropped))
        self.assertGreater(out.height, 1000)
        self.assertEqual(out.width, 1080)


class TestGeminiFallbackSkipsSsa(unittest.TestCase):
    def test_ssa_qty_value_vision_not_replaced(self):
        result = {
            "report_title": "STOCK & SALES ANALYSIS",
            "stockist_name": "PRAKASH DRUG AGENCY",
            "company_name": "HIMALAYA ZANDRA",
            "line_items": [
                {
                    "product_name": "BONNISAN 100ML",
                    "opening_qty": 1.0,
                    "opening_value": 59.86,
                    "receipts_qty": 56.0,
                    "sales_qty": 8.0,
                    "sales_value": 511.37,
                    "closing_qty": 49.0,
                    "closing_value": 2933.33,
                    "extra": {"opening_value": 59.86, "stock_identity_ok": True},
                }
            ],
            "totals": {
                "sales_value": 96675.69,
                "closing_value": 240235.96,
                "extra": {
                    "extraction_method": "ssa_qty_value_vision",
                    "layout": "ssa_opening_receipt_issue_value",
                    "stock_identity_fail_count": 2,
                },
            },
        }
        with mock.patch(
            "services.gemini_extraction_fallback._call_gemini"
        ) as caller:
            kept = maybe_apply_gemini_fallback(result, b"img", "stmt.jpg", ".jpg")
        caller.assert_not_called()
        self.assertEqual(
            kept["totals"]["extra"]["extraction_method"], "ssa_qty_value_vision"
        )
        self.assertEqual(kept["line_items"][0]["opening_qty"], 1.0)
        self.assertGreaterEqual(_ssa_qty_value_opening_count(kept), 1)


if __name__ == "__main__":
    unittest.main()
