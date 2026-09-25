"""Group Wise Sales Op.Stock PDF format checks."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_group_wise_sales_opstock_format,
    _parse_group_wise_sales_statement,
    extract_sales_statement,
)

SAMPLE = Path(
    r"C:\Users\amnsa\Downloads\ZL_2026_August\0000700209_2026_08_ZL_18_374_04092026060313.pdf"
)

FIXTURE_OCR = """
AMAR PHARMACEUTICAL AGENCIES
AT /P.O. PAKALAVARI STREET,NEAR CITY POST OFFICE, P.S.-BADA BAZAR,BERHAMPUR-760002 DIST:
Group Wise Sales (From 01/08/2026 UpTo 31/08/2026)
Product Name Strength Op.Stock Qty Purchase Qty Purchase Rtn Qty Purchase Free Qty Sales Ret Qty Sales Ret Free Qty Sales Qty Sales Free Cl.Stock As On
HIMALAYA DRUG CO ZEAL (ZENI)
GASEX ELAICHI FLA SYRUP 200 ML 49 0 0 0 0 0 0 2 0 47
GASEX TAB 100'S 4 0 0 0 0 0 0 3 0 1
HADJOD CAPS 60'S 53 0 0 0 0 0 0 3 0 50
LIV-52 SYRUP 200ML 615 0 0 0 0 0 0 123 0 492
PILEX FOTE OINT 30 GR 260 0 0 0 0 0 0 72 0 188
TENTEX FORTE TAB 10'S 73 0 0 0 0 0 0 10 0 63
Page 1 of 2
/31/2026 7:06:52 PM
"""


class TestGroupWiseDetect(unittest.TestCase):
    def test_detects_fixture(self):
        self.assertTrue(_is_group_wise_sales_opstock_format(FIXTURE_OCR))

    def test_rejects_received_issue_variant(self):
        text = """
        Group Wise Sales (From 01-08-2026 UpTo 31-08-2026)
        Product Name Strength Op. Qty Received Qty Total Qty Issue Qty SalesFree Qty Cl.Stock
        """
        self.assertFalse(_is_group_wise_sales_opstock_format(text))


class TestGroupWiseParseFixture(unittest.TestCase):
    def test_maps_validation_rows(self):
        result = _parse_group_wise_sales_statement(FIXTURE_OCR, "sample.pdf", "pdf")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["stockist_name"], "AMAR PHARMACEUTICAL AGENCIES")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        by = {i["product_name"].upper(): i for i in result["line_items"]}
        self.assertNotIn("/31/2026", by)
        gasex = next(i for i in result["line_items"] if "ELAICHI" in i["product_name"].upper())
        self.assertEqual(gasex["opening_qty"], 49.0)
        self.assertEqual(gasex["sales_qty"], 2.0)
        self.assertEqual(gasex["closing_qty"], 47.0)
        self.assertIsNone(gasex["sales_value"])
        tab = next(i for i in result["line_items"] if i["product_name"].upper().startswith("GASEX TAB"))
        self.assertEqual(tab["opening_qty"], 4.0)
        self.assertEqual(tab["sales_qty"], 3.0)
        self.assertEqual(tab["closing_qty"], 1.0)
        had = next(i for i in result["line_items"] if "HADJOD" in i["product_name"].upper())
        self.assertEqual(had["opening_qty"], 53.0)
        self.assertEqual(had["sales_qty"], 3.0)
        self.assertEqual(had["closing_qty"], 50.0)
        liv = next(
            i
            for i in result["line_items"]
            if "LIV-52 SYRUP" in i["product_name"].upper() and i["opening_qty"] == 615.0
        )
        self.assertEqual(liv["sales_qty"], 123.0)
        self.assertEqual(liv["closing_qty"], 492.0)
        pilex = next(i for i in result["line_items"] if "PILEX" in i["product_name"].upper() and "OINT" in i["product_name"].upper())
        self.assertEqual(pilex["opening_qty"], 260.0)
        self.assertEqual(pilex["sales_qty"], 72.0)
        self.assertEqual(pilex["closing_qty"], 188.0)
        tent = next(i for i in result["line_items"] if "TENTEX" in i["product_name"].upper())
        self.assertEqual(tent["opening_qty"], 73.0)
        self.assertEqual(tent["sales_qty"], 10.0)
        self.assertEqual(tent["closing_qty"], 63.0)


@unittest.skipUnless(SAMPLE.exists(), "sample Group Wise PDF not on this machine")
class TestGroupWiseSamplePdf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(SAMPLE.read_bytes(), SAMPLE.name)

    def test_not_date_junk(self):
        names = [i["product_name"] for i in self.result["line_items"]]
        self.assertTrue(len(names) >= 5)
        self.assertFalse(any("/31/2026" in n for n in names))
        self.assertEqual(
            self.result["totals"]["extra"].get("extraction_method"),
            "group_wise_sales_opstock",
        )

    def test_hard_ocr_rows(self):
        by = {
            i["product_name"].upper(): i for i in self.result["line_items"]
        }
        # Normalized names
        had = next(
            i
            for i in self.result["line_items"]
            if "HADJOD" in i["product_name"].upper()
        )
        self.assertEqual(had["opening_qty"], 53.0)
        self.assertEqual(had["sales_qty"], 3.0)
        self.assertEqual(had["closing_qty"], 50.0)

        tent = next(
            i
            for i in self.result["line_items"]
            if "TENTEX" in i["product_name"].upper()
        )
        self.assertEqual(tent["opening_qty"], 73.0)
        self.assertEqual(tent["sales_qty"], 10.0)
        self.assertEqual(tent["closing_qty"], 63.0)

        tab = next(
            i
            for i in self.result["line_items"]
            if i["product_name"].upper().startswith("GASEX TAB")
        )
        self.assertEqual(tab["opening_qty"], 4.0)
        self.assertEqual(tab["sales_qty"], 3.0)
        self.assertEqual(tab["closing_qty"], 1.0)


if __name__ == "__main__":
    unittest.main()
