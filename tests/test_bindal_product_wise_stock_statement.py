"""Product-wise stock statement (Bindal / Himalaya ZANDRA) PDF extraction."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_product_wise_stock_statement_format,
    _pwss_qty_band,
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


class TestProductWisePhotoAmounts(unittest.TestCase):
    def test_sales_and_closing_amounts_stay_off_qty_columns(self):
        from services.sales_statement_extractor import (
            _parse_product_wise_stock_statement_photo_panel,
            empty_result,
        )

        def word(x, y, text, w=40):
            return (x, y, x + w, y + 12, text)

        header = [
            word(80, 10, "Packing"),
            word(160, 10, "Opening"),
            word(230, 10, "Receipt"),
            word(300, 10, "Issues"),
            word(370, 10, "Closing"),
            word(450, 10, "Sh-Exp"),
            word(520, 10, "Liquidation"),
            word(620, 10, "Sales"),
            word(670, 10, "Amount"),
            word(760, 10, "Closing"),
            word(820, 10, "Amount"),
        ]
        row = [
            word(10, 40, "LIV"),
            word(40, 40, "52"),
            word(70, 40, "TAB", 30),
            word(120, 40, "100'S"),
            word(160, 40, "100"),
            word(300, 40, "2"),
            word(370, 40, "98"),
            word(500, 40, "1372"),
            word(560, 40, "Days"),
            word(640, 40, "352.00"),
            word(780, 40, "17,258.78", 70),
        ]
        result = empty_result("photo.png", "png")
        items = _parse_product_wise_stock_statement_photo_panel(
            header + row, "", result, set()
        )
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["opening_qty"], 100)
        self.assertIsNone(item["receipts_qty"])
        self.assertEqual(item["sales_qty"], 2)
        self.assertEqual(item["closing_qty"], 98)
        self.assertEqual(item["sales_value"], 352)
        self.assertEqual(item["closing_value"], 17258.78)
        self.assertEqual(item["extra"].get("liquidation_days"), 1372)

    def test_blank_value_columns_stay_null(self):
        from services.sales_statement_extractor import (
            _parse_product_wise_stock_statement_photo_panel,
            empty_result,
        )

        def word(x, y, text, w=40):
            return (x, y, x + w, y + 12, text)

        header = [
            word(80, 10, "Packing"),
            word(160, 10, "Opening"),
            word(230, 10, "Receipt"),
            word(300, 10, "Issues"),
            word(370, 10, "Closing"),
            word(460, 10, "Liquidation"),
        ]
        row = [
            word(10, 40, "BLEMINOR"),
            word(80, 40, "30ML"),
            word(160, 40, "17"),
            word(370, 40, "17"),
            word(460, 40, "128"),
            word(510, 40, "Days"),
        ]
        result = empty_result("photo.jpg", "jpg")
        items = _parse_product_wise_stock_statement_photo_panel(
            header + row, "", result, set()
        )
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["opening_qty"], 17)
        self.assertIsNone(item["receipts_qty"])
        self.assertIsNone(item["sales_qty"])
        self.assertEqual(item["closing_qty"], 17)
        self.assertIsNone(item["sales_value"])
        self.assertIsNone(item["closing_value"])


if __name__ == "__main__":
    unittest.main()
