"""Product-wise stock statement (Bindal / Himalaya ZANDRA) PDF extraction."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_product_wise_stock_statement_format,
    _parse_product_wise_stock_statement_pdf,
    _pwss_pdf_is_vacuous,
    _pwss_qty_band,
    empty_line_item,
    empty_result,
    extract_sales_statement,
)

SAMPLE = Path(
    r"C:\Users\anuja\Downloads\ZA_2026_August\0000700238_2026_08_ZA_18_226_05092026030138.pdf"
)


class TestProductWiseStockStatementHelpers(unittest.TestCase):
    def test_format_detector(self):
        text = (
            "BINDAL PHARMACEUTICALS, BAMRA\n"
            "Product wise stock statement from 01/08/2026 to 29/08/2026\n"
            "Receipt Issues Goods Stocks\n"
            "Opening Qty Free Jul Jun Qty Free Repl. Issue Closing Sh.Exp Liqudation\n"
        )
        self.assertTrue(_is_product_wise_stock_statement_format(text))
        self.assertFalse(
            _is_product_wise_stock_statement_format(
                "OpBal Receipt Total Issue Closing Dump"
            )
        )

    def test_opening_receipt_issues_without_jul_jun_is_not_bindal_bands(self):
        text = (
            "SRI MADHABA AGENCIES, ASKA\n"
            "Product wise stock statement from 01/09/2026 to 30/09/2026\n"
            "Product Name Packing Opening Receipt Issues Closing Sh.Exp Sale amount\n"
        )
        self.assertFalse(_is_product_wise_stock_statement_format(text))

    def test_all_zero_rows_with_footer_money_are_vacuous(self):
        item = empty_line_item()
        item["product_name"] = "PUREHANDS ADV"
        item["packing"] = "2X500ML"
        result = empty_result("Madhab agency.pdf", "pdf")
        result["totals"]["sales_value"] = 20755.0
        result["totals"]["closing_value"] = 1412.0
        self.assertTrue(_pwss_pdf_is_vacuous([item], result))
        item["opening_qty"] = 12.0
        self.assertFalse(_pwss_pdf_is_vacuous([item], result))


class _FakePage:
    def __init__(self, text: str, words=None):
        self._text = text
        self._words = words or []

    def get_text(self, kind: str = "text"):
        if kind == "words":
            return self._words
        return self._text


class TestPwssPdfVacuousFallsThrough(unittest.TestCase):
    def test_parser_returns_none_when_numbers_miss_hardcoded_bands(self):
        text = (
            "SRI MADHABA AGENCIES, ASKA\n"
            "Product wise stock statement from 01/09/2026 to 30/09/2026\n"
            "Total sales in this period : 20755.00\n"
            "Total closing stock : 1412.00\n"
        )
        # Name+pack match Bindal windows; qty sits far right of the Bindal x-bands.
        words = [
            (20.0, 120.0, 90.0, 132.0, "PUREHANDS", 0, 0, 0),
            (92.0, 120.0, 105.0, 132.0, "ADV", 0, 0, 1),
            (112.0, 120.0, 144.0, 132.0, "2X500ML", 0, 0, 2),
            (520.0, 120.0, 545.0, 132.0, "12", 0, 0, 3),
            (560.0, 120.0, 590.0, 132.0, "9", 0, 0, 4),
            (620.0, 120.0, 650.0, 132.0, "3", 0, 0, 5),
            (700.0, 120.0, 740.0, 132.0, "18", 0, 0, 6),
        ]
        doc = [_FakePage(text, words)]
        self.assertIsNone(
            _parse_product_wise_stock_statement_pdf(doc, "Madhab agency.pdf")
        )

    def test_qty_bands(self):
        self.assertEqual(_pwss_qty_band(176.2), "opening_qty")
        self.assertEqual(_pwss_qty_band(198.0), "receipts_qty")
        self.assertEqual(_pwss_qty_band(241.5), "jul_qty")
        self.assertEqual(_pwss_qty_band(263.3), "jun_qty")
        self.assertEqual(_pwss_qty_band(285.7), "sales_qty")
        self.assertEqual(_pwss_qty_band(386.2), "closing_qty")
        self.assertEqual(_pwss_qty_band(539.2), "sales_value")


@unittest.skipUnless(SAMPLE.exists(), "sample Product wise stock statement PDF missing")
class TestBindalProductWiseStockStatement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(
            SAMPLE.read_bytes(), SAMPLE.name
        )

    def test_metadata_and_method(self):
        self.assertEqual(
            self.result["totals"]["extra"]["extraction_method"],
            "product_wise_stock_statement_pdf",
        )
        self.assertEqual(self.result["stockist_name"], "BINDAL PHARMACEUTICALS, BAMRA")
        self.assertEqual(self.result["company_name"], "THE HIMALAYA DRUG(ZANDRA)")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-29")
        self.assertEqual(self.result["totals"]["sales_value"], 35892.0)
        self.assertEqual(self.result["totals"]["closing_value"], 74995.0)
        self.assertEqual(len(self.result["line_items"]), 51)

    def test_key_rows_not_scrambled(self):
        by_key = {
            (i["product_name"], i["packing"]): i for i in self.result["line_items"]
        }

        arjuna = by_key[("ARJUNA TAB", "60X60'S")]
        self.assertEqual(arjuna["opening_qty"], 0.0)
        self.assertEqual(arjuna["sales_qty"], 0.0)
        self.assertEqual(arjuna["closing_qty"], 0.0)

        drops = by_key[("BONNISAN DROPS", "50X30ML")]
        self.assertEqual(drops["opening_qty"], 43.0)
        self.assertEqual(drops["receipts_qty"], 0.0)
        self.assertEqual(drops["sales_qty"], 0.0)
        self.assertEqual(drops["closing_qty"], 43.0)
        self.assertEqual(drops["extra"].get("jul_qty"), 4.0)
        self.assertEqual(drops["extra"].get("jun_qty"), 5.0)

        liquid = by_key[("BONNISAN LIQUID", "28X200M")]
        self.assertEqual(liquid["opening_qty"], 46.0)
        self.assertEqual(liquid["sales_qty"], 22.0)
        self.assertEqual(liquid["closing_qty"], 24.0)
        self.assertEqual(liquid["sales_value"], 2145.06)

        liv = by_key[("LIV.52 DS TABLETS", "100X60'")]
        self.assertEqual(liv["receipts_qty"], 200.0)
        self.assertEqual(liv["sales_qty"], 102.0)
        self.assertEqual(liv["closing_qty"], 98.0)
        self.assertEqual(liv["sales_value"], 20729.28)

    def test_opening_receipts_sales_close_balance(self):
        for item in self.result["line_items"]:
            bal = (
                item["opening_qty"]
                + item["receipts_qty"]
                - item["sales_qty"]
            )
            self.assertAlmostEqual(
                bal,
                item["closing_qty"],
                places=2,
                msg=f"{item['product_name']} {item['packing']}",
            )


if __name__ == "__main__":
    unittest.main()
