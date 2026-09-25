"""Scanned portrait Sales & Stock Qty/Value pairs (image-only PDF)."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_swil_qty_value_pair_text,
    _parse_swil_scanned_qty_value_pair_doc,
    _parse_swil_scanned_qty_value_pair_text,
    _swil_scan_pair_parse_line,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000736211_2026_08_ZA_03_302_05092026094339 (1).pdf"
)
MILAN = (
    Path(__file__).resolve().parents[1]
    / "0000729398_2026_08_ZL_04_8208_01092026090620.pdf"
)

OCR_HEADER = (
    "Sales & Stock Statement(From 01/08/2026 Upto 28/08/2026)\n"
    "HIMALAYA ZANDRA DIVI\n"
    "PRODUCT NAME PACKING Op. Opening Bal Receipt Receipt/Pur Total "
    "Issue Issue/Sales Closing Closing Bala Near\n"
    "Qty. Value Qty. Value Qty. Qty. Value Qty. Value Expiry\n"
)


class TestScannedPairLineParser(unittest.TestCase):
    def test_maps_qty_value_pairs_not_opening_value_as_qty(self):
        parsed = _swil_scan_pair_parse_line(
            "ARJUNA TAB 60'S 19 4191.97 0 0.00 19 4 990.48 15 3309.45 0"
        )
        self.assertIsNotNone(parsed)
        name, pack, core = parsed
        self.assertIn("ARJUNA", name.upper())
        self.assertEqual(pack, "60'S")
        self.assertEqual(core[0], 19.0)
        self.assertEqual(core[1], 4191.97)
        self.assertEqual(core[5], 4.0)
        self.assertEqual(core[6], 990.48)
        self.assertEqual(core[7], 15.0)
        self.assertEqual(core[8], 3309.45)
        self.assertNotEqual(core[0], 4191.97)

    def test_joins_split_receipt_value_and_infers_lost_total(self):
        split = _swil_scan_pair_parse_line(
            "BONNISAN SYP- 100MI 136 8173.60 112 S 6 731.20 248 126 8271.60 122 7332.20 0"
        )
        self.assertIsNotNone(split)
        self.assertEqual(split[2][0], 136.0)
        self.assertEqual(split[2][2], 112.0)
        self.assertEqual(split[2][3], 6731.20)
        self.assertEqual(split[2][4], 248.0)

        lost_total = _swil_scan_pair_parse_line(
            "BRESOL TAB 60'S 21 3517.71 50 8375.50 7 23 4346.08 48 8040.48 0"
        )
        self.assertIsNotNone(lost_total)
        self.assertEqual(lost_total[2][0], 21.0)
        self.assertEqual(lost_total[2][2], 50.0)
        self.assertEqual(lost_total[2][4], 71.0)
        self.assertEqual(lost_total[2][5], 23.0)
        self.assertEqual(lost_total[2][7], 48.0)

    def test_skips_headers(self):
        self.assertIsNone(
            _swil_scan_pair_parse_line(
                "PRODUCT NAME PACKING Op. Opening Bal Receipt Receipt/Pur"
            )
        )
        self.assertFalse(_is_swil_qty_value_pair_text("Opening Purchase Sales Closing"))


class TestScannedPairText(unittest.TestCase):
    def test_builds_statement_from_ocr_lines(self):
        text = OCR_HEADER + (
            "ARJUNA TAB 60'S 19 4191.97 0 0.00 19 4 990.48 15 3309.45 0\n"
            "BONNISAN DROP 30ML 94 6221.86 0 0.00 94 32 2314.46 62 4103.78 0\n"
            "BONNISAN SYP 200ML 104 10536.24 0 0.00 104 13 1485.64 91 9219.21 0\n"
            "CYSTONE TAB 60'S 131 21501.03 0 0.00 131 33 6109.62 98 16084.74 0\n"
            "EVECARE SYP 200ML 247 35866.87 0 0.00 247 25 4095.00 222 32236.62 0\n"
            "HIMPLASIA TAB 60'S 49 16217.53 0 0.00 49 7 2613.38 42 13900.74 0\n"
            "LASUNA TAB 60'S 55 12134.65 0 0.00 55 1 247.62 54 11914.02 0\n"
            "SEPTILIN TAB 60'S 148 25689.84 0 0.00 148 56 10964.80 92 15969.36 0\n"
            "GRAND TOTAL 11593 1785244.65 845 105856.09 12438 3086 501888.88 9352 1434411.82 278\n"
        )
        result = _parse_swil_scanned_qty_value_pair_text([text], "scan.pdf")
        self.assertIsNotNone(result)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "swil_scanned_qty_value_pair")
        self.assertFalse(extra.get("vertex_ai_used"))
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-28")
        self.assertEqual(result["totals"].get("sales_value"), 501888.88)
        self.assertEqual(result["totals"].get("closing_value"), 1434411.82)
        by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in result["line_items"]
        }
        drop = next(item for key, item in by_name.items() if "BONNISAN DROP" in key)
        self.assertEqual(drop["opening_qty"], 94.0)
        self.assertEqual(drop["extra"]["opening_value"], 6221.86)
        self.assertEqual(drop["sales_qty"], 32.0)
        self.assertEqual(drop["closing_qty"], 62.0)
        self.assertNotIn("Invoices", result)


@unittest.skipUnless(FIXTURE.is_file(), "missing scanned pair fixture")
class TestScannedPairFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in cls.result["line_items"]
        }

    def test_uses_scanned_pair_not_shifted_vision(self):
        self.assertEqual(self.extra.get("extraction_method"), "swil_scanned_qty_value_pair")
        self.assertFalse(self.extra.get("vertex_ai_used"))
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-28")
        self.assertGreaterEqual(len(self.result["line_items"]), 50)
        self.assertNotIn("Invoices", self.result)
        self.assertEqual(self.result["totals"].get("sales_value"), 501888.88)
        self.assertEqual(self.result["totals"].get("closing_value"), 1434411.82)

        arjuna = next(
            item
            for item in self.result["line_items"]
            if "ARJUNA" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(arjuna["opening_qty"], 19.0)
        self.assertEqual(arjuna["extra"]["opening_value"], 4191.97)
        self.assertEqual(arjuna["sales_qty"], 4.0)
        self.assertEqual(arjuna["sales_value"], 990.48)
        self.assertEqual(arjuna["closing_qty"], 15.0)
        self.assertEqual(arjuna["closing_value"], 3309.45)

        drop = next(
            item
            for item in self.result["line_items"]
            if "BONNISAN DROP" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(drop["opening_qty"], 94.0)
        self.assertEqual(drop["sales_qty"], 32.0)
        self.assertEqual(drop["closing_qty"], 62.0)
        self.assertNotEqual(drop["opening_qty"], 104.0)

        cystone = next(
            item
            for item in self.result["line_items"]
            if str(item.get("product_name") or "").upper().startswith("CYSTONE TAB")
        )
        self.assertEqual(cystone["opening_qty"], 131.0)
        self.assertEqual(cystone["sales_qty"], 33.0)
        self.assertEqual(cystone["closing_qty"], 98.0)
        self.assertEqual(cystone["closing_value"], 16084.74)


@unittest.skipUnless(MILAN.is_file(), "missing digital pair fixture")
class TestDigitalPairNotStolen(unittest.TestCase):
    def test_embedded_text_pair_stays_on_existing_parser(self):
        import fitz

        doc = fitz.open(MILAN)
        try:
            self.assertIsNone(_parse_swil_scanned_qty_value_pair_doc(doc, MILAN.name))
        finally:
            doc.close()
        result = extract_sales_statement(MILAN.read_bytes(), MILAN.name)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "swil_qty_value_pair")


if __name__ == "__main__":
    unittest.main()
