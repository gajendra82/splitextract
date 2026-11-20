"""STOCK AND SALES STATEMENT photos: BOX must not become opening_qty."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _is_code_item_stock_statement_photo_text,
    _looks_like_opstk_box_shift_misread,
    empty_result,
)


class OpstkBoxShiftPhotoTests(unittest.TestCase):
    def test_code_item_detector_rejects_opstk_opv(self):
        text = (
            "ARKAY MEDICOS\n"
            "STOCK AND SALES STATEMENT\n"
            "PCode Product Pack BOX OPSTK OPV PURC PURCV SALE SALEV STOCK STOCKV\n"
            "HIMALAYA DRUG COMPANY\n"
        )
        self.assertFalse(_is_code_item_stock_statement_photo_text(text))

    def test_code_item_detector_still_accepts_pharma_hub(self):
        text = (
            "THE PHARMA HUB\n"
            "Stock Statment : HIMALAYA DRUGS (ZEAL)\n"
            "Aug. 2026\n"
        )
        self.assertTrue(_is_code_item_stock_statement_photo_text(text))

    def test_box_shift_signature(self):
        bad = empty_result("x.jpg", "jpg")
        bad["totals"]["extra"]["extraction_method"] = (
            "code_item_stock_statement_photo"
        )
        # BOX→opening, OPSTK→receipts, SALE→closing, SALEV→closing_value; no OPV.
        rows = [
            ("ASHWAGANDHA 120TAB", 36, 9, 0, 3, 1064.25),
            ("ASHWAGANDHA 60TAB", 60, 52, 60, 54, 10057.44),
            ("AYURSLIM CAP", 24, 2, 0, 4, 913.39),
            ("BRAHMI", 60, 3, 60, 60, 10590.6),
            ("GASEX TAB", 100, 72, 0, 47, 6343.59),
            ("GOKSHURA TAB", 60, 15, 0, 7, 1306.76),
            ("GUDUCHI TAB", 60, 34, 0, 28, 4942.28),
            ("NEEM TAB", 60, 27, 60, 69, 4298.73),
        ]
        bad["line_items"] = [
            {
                "product_name": name,
                "opening_qty": op,
                "opening_value": None,
                "receipts_qty": rc,
                "sales_qty": sq,
                "closing_qty": cl,
                "closing_value": cv,
                "extra": {},
            }
            for name, op, rc, sq, cl, cv in rows
        ]
        self.assertTrue(_looks_like_opstk_box_shift_misread(bad))

        good = empty_result("y.jpg", "jpg")
        good["line_items"] = [
            {
                "product_name": "ASHWAGANDHA 120TAB",
                "opening_qty": 9,
                "opening_value": 3028.14,
                "receipts_qty": 0,
                "sales_qty": 3,
                "closing_qty": 6,
                "closing_value": 2018.76,
                "extra": {"opening_value": 3028.14},
            }
            for _ in range(8)
        ]
        self.assertFalse(_looks_like_opstk_box_shift_misread(good))


if __name__ == "__main__":
    unittest.main()
