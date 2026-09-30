"""Phone screenshot of a RATE + qty/value STOCK & SALES ANALYSIS page."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _phone_pdf_viewer_statement_jpeg,
    _phone_rate_ssa_rows_usable,
    empty_line_item,
)


PHOTO = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August\0000734336_2026_08_ZL_24_8273_03092026090403.png"
)
ALPHA = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August\0000734334_2026_08_ZL_34_354_06092026114923.jpg"
)
ZEAL = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August\0000734308_2026_08_ZL_04_341_05092026151343.jpg"
)


def _row(name, rate, opening, receipts, sales, closing, **extra):
    item = empty_line_item()
    item["product_name"] = name
    item["opening_qty"] = opening
    item["receipts_qty"] = receipts
    item["sales_qty"] = sales
    item["closing_qty"] = closing
    item["extra"] = {"unit_rate": rate, "dump_qty": extra.get("dump_qty", 0)}
    return item


class TestPhoneRateSsaScreenshot(unittest.TestCase):
    @unittest.skipUnless(PHOTO.is_file(), "missing Krishna screenshot")
    def test_screenshot_is_cropped(self):
        jpeg = _phone_pdf_viewer_statement_jpeg(PHOTO.read_bytes())
        self.assertIsNotNone(jpeg)
        self.assertGreater(len(jpeg), 1000)

    @unittest.skipUnless(ALPHA.is_file(), "missing Alpha photo")
    def test_paper_photo_is_not_a_phone_pdf(self):
        self.assertIsNone(_phone_pdf_viewer_statement_jpeg(ALPHA.read_bytes()))

    @unittest.skipUnless(ZEAL.is_file(), "missing Zeal photo")
    def test_order_form_photo_is_not_a_phone_pdf(self):
        self.assertIsNone(_phone_pdf_viewer_statement_jpeg(ZEAL.read_bytes()))

    def test_rate_copied_into_opening_is_rejected(self):
        kept = [
            _row(f"ITEM {index}", 40.18, 2, 0, 0, 2) for index in range(10)
        ]
        self.assertTrue(_phone_rate_ssa_rows_usable(kept))
        shifted = [
            _row(f"ITEM {index}", 40.18, 40.18, 0, 0, 40.18) for index in range(10)
        ]
        self.assertFalse(_phone_rate_ssa_rows_usable(shifted))


if __name__ == "__main__":
    unittest.main()
