"""Qty-only STOCK & SALES ANALYSIS OPENING|RECEIPT|ISSUE|CLOSING OCR path."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.sales_statement_extractor import (
    _extract_ssa_oric_qty_only_photo,
    _is_ssa_oric_qty_only_text,
    _parse_ps_pharma_statement,
    _peek_suggests_ssa_qty_value,
)


DATTA_ORIC = """
M/S DATTA MEDICAL AGENCY
STOCK & SALES ANALYSIS (HIMALAYA WELLNESS CO (ZENITH)) 01-09-2026 - 29-09-2026
ITEM DESCRIPTION                 OPENING   RECEIPT     ISSUE   CLOSING
HIMALAYA WELLNESS CO (ZENITH)
ASHVAGANDHA CAP        60'S         12         -         4         8
CONFIDO TAB            60'S        138         -        63        75
PILEX FORTE OINTMENT   30GM          3       100        33        70
TENTEX FORTE TAB       10'S        124       300        41       383
LIV.52 SYP            200ML        167         -        40       127
TOTAL                            60738     78171     61309     80537
"""

QTY_VALUE_ANALYSIS = """
ADARSH AGENCY
STOCK & SALES ANALYSIS 01-09-2026 - 30-09-2026
ITEM DESCRIPTION OPENING QTY VALUE RECEIPT QTY VALUE ISSUE QTY VALUE CLOSING QTY VALUE DUMP
PRODUCT A 10S 5 50.00 2 20.00 1 10.00 6 60.00 -
"""


class TestSsaOricQtyOnlyDetector(unittest.TestCase):
    def test_datta_qty_only_matches(self):
        self.assertTrue(_is_ssa_oric_qty_only_text(DATTA_ORIC))

    def test_qty_value_and_dump_do_not_match(self):
        self.assertFalse(_is_ssa_oric_qty_only_text(QTY_VALUE_ANALYSIS))
        dump = DATTA_ORIC.replace("CLOSING", "CLOSING DUMP")
        self.assertFalse(_is_ssa_oric_qty_only_text(dump))

    def test_peek_ssa_qty_value_excludes_oric_qty_only(self):
        self.assertFalse(_peek_suggests_ssa_qty_value(DATTA_ORIC))
        self.assertTrue(_peek_suggests_ssa_qty_value(QTY_VALUE_ANALYSIS))


SARAF_PCS_ORIC = """
SARAF BROTHERS
STOCK & SALES ANALYSIS 01-10-2026 - 31-10-2026
CODE ITEM DESCRIPTION OPENING RECEIPT ISSUE CLOSING
07000004 ABANA TABS 60'S PCS 20 - 20 -
BRESL BRESOL SYRUP 200ML PCS -1 - -1 -
BREST BRESOL TABS 60'S PCS 10 - 3 7
CYST CYSTONE TABS 60'S PCS 27 - 27 -
7000349 RUMALAYA LINIME 60ML PCS 40 - 40 -
7002239 LIV 52 DS SYRUP 200ML PCS 2 - 2 -
LIVDT LIV 52 DS TABS 60'S PCS 185 100 178 107
TOTAL 71778 22281 58429 39932
"""


class TestSsaOricQtyOnlyParser(unittest.TestCase):
    def test_opening_not_shifted_into_receipt(self):
        result = _parse_ps_pharma_statement(DATTA_ORIC, "datta.pdf")
        self.assertIsNotNone(result)
        by_name = {item["product_name"]: item for item in result["line_items"]}
        confido = by_name["CONFIDO TAB"]
        self.assertEqual(confido["opening_qty"], 138.0)
        self.assertEqual(confido["receipts_qty"], 0.0)
        self.assertEqual(confido["sales_qty"], 63.0)
        self.assertEqual(confido["closing_qty"], 75.0)
        liv = by_name["LIV.52 SYP"]
        self.assertEqual(liv["opening_qty"], 167.0)
        self.assertEqual(liv["receipts_qty"], 0.0)
        self.assertEqual(liv["sales_qty"], 40.0)
        self.assertEqual(liv["closing_qty"], 127.0)
        pilex = by_name["PILEX FORTE OINTMENT"]
        self.assertEqual(pilex["opening_qty"], 3.0)
        self.assertEqual(pilex["receipts_qty"], 100.0)
        self.assertEqual(result.get("company_name"), "HIMALAYA WELLNESS CO (ZENITH)")

    def test_pcs_pack_unit_not_stolen_as_opening(self):
        """PCS matches qty empty-frag; must not shift OPENING into RECEIPT."""
        with patch.dict(
            os.environ, {"STOCK_PS_PHARMA_LEADING_PACK_UNIT": "true"}, clear=False
        ):
            result = _parse_ps_pharma_statement(SARAF_PCS_ORIC, "saraf.pdf")
        self.assertIsNotNone(result)
        by_name = {item["product_name"]: item for item in result["line_items"]}
        abana = next(i for n, i in by_name.items() if "ABANA" in n)
        self.assertEqual(abana["opening_qty"], 20.0)
        self.assertEqual(abana["receipts_qty"], 0.0)
        self.assertEqual(abana["sales_qty"], 20.0)
        self.assertEqual(abana["closing_qty"], 0.0)
        self.assertIn("PCS", str(abana.get("packing") or "").upper())
        cyst = next(i for n, i in by_name.items() if "CYSTONE" in n)
        self.assertEqual(cyst["opening_qty"], 27.0)
        self.assertEqual(cyst["receipts_qty"], 0.0)
        self.assertEqual(cyst["sales_qty"], 27.0)
        self.assertEqual(cyst["closing_qty"], 0.0)
        liv = next(i for n, i in by_name.items() if "LIV 52 DS TABS" in n)
        self.assertEqual(liv["opening_qty"], 185.0)
        self.assertEqual(liv["receipts_qty"], 100.0)
        self.assertEqual(liv["sales_qty"], 178.0)
        self.assertEqual(liv["closing_qty"], 107.0)
        bresol = next(i for n, i in by_name.items() if "BRESOL TABS" in n)
        self.assertEqual(bresol["opening_qty"], 10.0)
        self.assertEqual(bresol["sales_qty"], 3.0)
        self.assertEqual(bresol["closing_qty"], 7.0)


class TestSsaOricQtyOnlyPhotoExtract(unittest.TestCase):
    def test_extract_uses_ocr_and_marks_method(self):
        with patch(
            "services.sales_statement_extractor._ocr_image_to_text",
            return_value=DATTA_ORIC,
        ):
            result = _extract_ssa_oric_qty_only_photo(b"fake", "datta.jpg", ".jpg")
        self.assertIsNotNone(result)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "ssa_oric_qty_only_ocr")
        self.assertTrue(extra.get("qty_only"))
        by_name = {item["product_name"]: item for item in result["line_items"]}
        self.assertEqual(by_name["CONFIDO TAB"]["opening_qty"], 138.0)

    def test_flag_off_returns_none(self):
        with patch.dict(os.environ, {"STOCK_SSA_ORIC_QTY_ONLY_OCR": "false"}):
            with patch(
                "services.sales_statement_extractor._ocr_image_to_text",
                return_value=DATTA_ORIC,
            ):
                self.assertIsNone(
                    _extract_ssa_oric_qty_only_photo(b"fake", "datta.jpg", ".jpg")
                )

    def test_qty_value_hint_returns_none(self):
        self.assertIsNone(
            _extract_ssa_oric_qty_only_photo(
                b"fake", "adarsh.jpg", ".jpg", ocr_hint=QTY_VALUE_ANALYSIS
            )
        )


if __name__ == "__main__":
    unittest.main()
