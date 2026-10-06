"""Photographed STOCK & SALES ANALYSIS with RATE and QTY/VALUE columns."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_rate_qty_value_header_text,
    _is_rate_qty_value_header_text_fuzzy,
    _ocr_qty_token,
    _parse_rate_qty_value_image,
    _sanitize_numeric_overrides,
    empty_line_item,
    empty_result,
)


PHOTO = Path("/var/www/html/splitextract/0000734021_2026_08_ZL_04_342_03092026164551.jpg")
VAISHNO = Path(
    "/var/www/html/splitextract/0000707312_2026_08_ZL_35_260_02092026170425.jpg"
)


class TestRateQtyValueHeader(unittest.TestCase):
    def test_requires_rate_and_qty_value(self):
        self.assertTrue(
            _is_rate_qty_value_header_text(
                "STOCK & SALES ANALYSIS\nRATE OPENING RECEIPT\nQTY VALUE QTY VALUE"
            )
        )
        self.assertFalse(
            _is_rate_qty_value_header_text(
                "STOCK & SALES ANALYSIS\nITEM DESCRIPTION OPENING RECEIPT ISSUE CLOSING M.EXP"
            )
        )
        self.assertFalse(
            _is_rate_qty_value_header_text("Zandra\nORDER FORM\nFrom:")
        )

    def test_fuzzy_accepts_ocr_rate_without_qty_token(self):
        self.assertTrue(
            _is_rate_qty_value_header_text_fuzzy(
                "STOCK & SALES ANALYSIS\nITEM DESCRIPTION Rate OPENING RECEIPT VALUE"
            )
        )
        self.assertFalse(
            _is_rate_qty_value_header_text_fuzzy(
                "STOCK & SALES ANALYSIS\nOPENING RECEIPT ISSUE CLOSING\nQTY VALUE"
            )
        )

    def test_negative_closing_value_not_zeroed(self):
        """Printed negative closing amounts must stay (CYSTONE FORTE style)."""
        result = empty_result("shree.jpg", "jpg")
        result["totals"]["extra"]["extraction_method"] = "rate_qty_value_columns"
        item = empty_line_item()
        item["product_name"] = "CYSTONE FORTE TAB"
        item["opening_qty"] = 156.0
        item["sales_qty"] = 157.0
        item["closing_qty"] = -1.0
        item["closing_value"] = -99.63
        item["sales_value"] = 17027.14
        item["extra"] = {"layout": "rate_qty_value"}
        result["line_items"] = [item]
        out = _sanitize_numeric_overrides(result, report_only=False, stage="lines")
        cystone = out["line_items"][0]
        self.assertEqual(cystone["closing_qty"], -1.0)
        self.assertAlmostEqual(cystone["closing_value"], -99.63, places=2)
        self.assertNotIn("rejected_closing_value", cystone.get("extra") or {})

    def test_negative_issue_qty_token_parsed(self):
        self.assertEqual(_ocr_qty_token("-8"), -8.0)
        self.assertEqual(_ocr_qty_token("\u22128"), -8.0)
        self.assertEqual(_ocr_qty_token("-1"), -1.0)


@unittest.skipUnless(PHOTO.is_file(), "missing Dawaghar photo")
class TestDawagharQtyValuePhoto(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _parse_rate_qty_value_image(PHOTO.read_bytes(), PHOTO.name, ".jpg")

    def test_bonnisan_drops_keeps_rate_out_of_receipts(self):
        self.assertIsNotNone(self.result)
        drops = next(
            item
            for item in self.result["line_items"]
            if "BONNISAN DROPS" in item["product_name"].upper()
        )
        self.assertEqual(drops["opening_qty"], 50.0)
        self.assertEqual(drops["receipts_qty"], 0.0)
        self.assertEqual(drops["sales_qty"], 10.0)
        self.assertEqual(drops["closing_qty"], 40.0)
        self.assertAlmostEqual(drops["opening_value"], 3120.79, places=2)
        self.assertAlmostEqual(drops["sales_value"], 667.06, places=2)
        self.assertAlmostEqual(drops["closing_value"], 2496.63, places=2)
        self.assertNotEqual(drops["receipts_qty"], 62.42)
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertIn("DAWAGHAR", self.result["stockist_name"])
        names = " ".join(item["product_name"].upper() for item in self.result["line_items"])
        self.assertNotIn("STOCK & SALES", names)


@unittest.skipUnless(VAISHNO.is_file(), "missing Vaishno RATE photo")
class TestVaishnoRateQtyValueVision(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from services.sales_statement_extractor import extract_sales_statement

        cls.result = extract_sales_statement(VAISHNO.read_bytes(), VAISHNO.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in cls.result.get("line_items") or []
        }

    def test_uses_rate_aware_path_not_generic_gemini(self):
        method = str(self.extra.get("extraction_method") or "")
        self.assertTrue(
            method in {"rate_qty_value_vision", "rate_qty_value_columns"},
            method,
        )
        self.assertFalse(self.result.get("multi_statement"))
        names = " | ".join(self.by_name)
        self.assertNotIn("HIMALAYA [ZEAL]", names)
        stockist = str(self.result.get("stockist_name") or "")
        self.assertNotRegex(stockist, r"HIMALAYA", re.I)
        self.assertNotRegex(stockist, r"KICHHA|VIKAS", re.I)
        if stockist:
            self.assertRegex(stockist, r"MEDICO|MEDICAL|AGENC|PHARMA|STORE", re.I)

    def test_aactaril_and_abana_columns(self):
        aact = next(
            item
            for key, item in self.by_name.items()
            if key.startswith("AACTARIL")
        )
        self.assertEqual(aact["opening_qty"], 118.0)
        self.assertEqual(aact["receipts_qty"], 0.0)
        self.assertEqual(aact["sales_qty"], 27.0)
        self.assertEqual(aact["closing_qty"], 91.0)
        self.assertAlmostEqual(aact["sales_value"], 2217.66, places=2)
        self.assertAlmostEqual(aact["closing_value"], 6715.44, places=2)
        self.assertNotEqual(aact["closing_qty"], aact["sales_qty"])

        abana = next(
            item for key, item in self.by_name.items() if key.startswith("ABANA")
        )
        self.assertEqual(abana["opening_qty"], 23.0)
        self.assertEqual(abana["receipts_qty"], 100.0)
        self.assertEqual(abana["sales_qty"], 35.0)
        self.assertEqual(abana["closing_qty"], 88.0)
        self.assertAlmostEqual(
            float(
                abana.get("receipts_value")
                or (abana.get("extra") or {}).get("receipts_value")
                or 0
            ),
            14117.0,
            places=2,
        )
        self.assertNotEqual(abana["receipts_qty"], 14117.0)


if __name__ == "__main__":
    unittest.main()
