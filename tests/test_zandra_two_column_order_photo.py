"""Zandra ORDER FORM photos with a product table on each side."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _zandra_order_form_from_label,
    _zandra_order_side_items,
    _zandra_put_lasuna_qty_on_its_row,
    _zandra_two_column_order_photo,
    _zandra_two_column_order_text,
)


ATUL = Path("0000735937_2026_08_ZA_24_258_01092026065830.jpeg")
SINGH = Path("0000735936_2026_08_ZA_24_256_02092026173543.jpg")
OLDER_SAP = Path("0000732417_2026_08_ZA_07_322_06092026011854.jpeg")
LUCKY_STORE = Path("0000736020_2026_08_ZA_24_259_03092026065900.jpg")
SACHDEVA = Path("0000730316_2026_08_ZA_07_319_06092026005507.jpeg")


class TestZandraTwoColumnOrderText(unittest.TestCase):
    def test_requires_both_words(self):
        self.assertTrue(
            _zandra_two_column_order_text("Zandra\nORDER FORM\nFrom:")
        )
        self.assertTrue(
            _zandra_two_column_order_text("Zandra at\nORDER FORNIN")
        )
        self.assertFalse(_zandra_two_column_order_text("ORDER FORM\nSAP Code"))
        self.assertFalse(_zandra_two_column_order_text("Zandra\nStock and Sale Statement"))

    def test_from_box_matches_when_the_title_does_not_ocr(self):
        self.assertTrue(
            _zandra_order_form_from_label(
                "Zandra\n| fom: FAMOUS MEDICAL AGENCY | To: SALE & STOCK"
            )
        )
        self.assertFalse(
            _zandra_order_form_from_label(
                "Zandra\nStock and Sale Statement\nFrom: 01-Aug-26"
            )
        )
        self.assertFalse(_zandra_order_form_from_label("ORDER FORM\nFrom:"))

    def test_handwritten_1700_is_not_stripped_to_170(self):
        rows = [{"product_name": "Liv.52 DS tablets", "packing": "60s", "qty": 1700}]
        kept = _zandra_order_side_items(rows, keep_handwritten_qty=True)[0]
        stripped = _zandra_order_side_items(rows, keep_handwritten_qty=False)[0]
        self.assertEqual(kept["sales_qty"], 1700)
        self.assertEqual(stripped["sales_qty"], 170)

    def test_dense_handwritten_qty_is_not_treated_as_pack_echo(self):
        """Real Qty values that are not Pack sizes must not look like pack echoes."""
        rows = [
            {"product_name": "Bonnisan drops", "packing": "30 ml", "qty": 42},
            {"product_name": "Bonnisan liquid", "packing": "100 ml", "qty": 22},
            {"product_name": "Bonnisan liquid", "packing": "200 ml", "qty": 62},
            {"product_name": "Bonnispaz drops", "packing": "15 ml", "qty": 12},
            {"product_name": "Bresol syrup", "packing": "200 ml", "qty": 45},
            {"product_name": "Bresol tablets", "packing": "60s", "qty": 1322},
            {"product_name": "Cystone syrup", "packing": "200 ml", "qty": 43},
            {"product_name": "Evecare capsules", "packing": "30s", "qty": 277},
        ]
        items = _zandra_order_side_items(rows, keep_handwritten_qty=False)
        positives = [i for i in items if i["sales_qty"] > 0]
        pack_echoes = sum(
            1
            for i in positives
            if str(int(i["sales_qty"]))
            in re.findall(r"\d+", str(i.get("packing") or ""))
        )
        self.assertGreaterEqual(len(positives), 7)
        self.assertLess(pack_echoes, max(4, int(len(positives) * 0.6)))
        self.assertEqual(
            next(
                i["sales_qty"]
                for i in positives
                if "Bresol tablets" in i["product_name"]
            ),
            1322,
        )

    def test_lasuna_600_is_not_left_on_liv52_drops(self):
        items = [
            {"product_name": "Lasuna tablets", "sales_qty": 0.0},
            {"product_name": "Liv.52 drops", "sales_qty": 600.0},
            {"product_name": "Liv.52 drops", "sales_qty": 0.0},
        ]
        _zandra_put_lasuna_qty_on_its_row(items)
        self.assertEqual(items[0]["sales_qty"], 600)
        self.assertEqual(items[1]["sales_qty"], 0)

    @unittest.skipUnless(LUCKY_STORE.is_file(), "missing Lucky Medical Store order form")
    def test_order_form_matches_when_the_logo_is_not_read(self):
        self.assertTrue(_zandra_two_column_order_photo(LUCKY_STORE.read_bytes()))

    @unittest.skipUnless(ATUL.is_file(), "missing Atul order form")
    def test_this_photo_matches(self):
        self.assertTrue(_zandra_two_column_order_photo(ATUL.read_bytes()))

    @unittest.skipUnless(SINGH.is_file(), "missing Dr Singh order form")
    def test_title_below_the_top_of_the_photo_matches(self):
        self.assertTrue(_zandra_two_column_order_photo(SINGH.read_bytes()))

    @unittest.skipUnless(OLDER_SAP.is_file(), "missing older SAP photo")
    def test_older_right_qty_photo_does_not_match(self):
        self.assertFalse(_zandra_two_column_order_photo(OLDER_SAP.read_bytes()))

    @unittest.skipUnless(SACHDEVA.is_file(), "missing Sachdeva order form")
    def test_sachdeva_dense_handwritten_qtys_are_kept(self):
        from services.sales_statement_extractor import extract_sales_statement

        result = extract_sales_statement(SACHDEVA.read_bytes(), SACHDEVA.name)
        extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertEqual(extra.get("extraction_method"), "zandra_two_column_order_form")
        by_name = {
            str(i.get("product_name") or "").lower(): i
            for i in (result.get("line_items") or [])
        }
        drops = by_name.get("bonnisan drops")
        self.assertIsNotNone(drops)
        self.assertEqual(drops["sales_qty"], 42.0)
        bresol = by_name.get("bresol tablets")
        self.assertIsNotNone(bresol)
        self.assertEqual(bresol["sales_qty"], 1322.0)
        filled = sum(
            1
            for i in (result.get("line_items") or [])
            if (i.get("sales_qty") or 0) not in (0, 0.0, None)
        )
        self.assertGreaterEqual(filled, 20)


if __name__ == "__main__":
    unittest.main()
