"""Saleable Stock Report Particular | Opn | Rec | Issue | Bal PDF."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_saleable_stock_report_text,
    _parse_saleable_stock_report,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000720977_2026_08_ZL_07_223_05092026080848.pdf"
)

HEADER = (
    "PARAS MEDICOS\n"
    "(From 01/08/2026 To 31/08/2026)\n"
    "Saleable Stock Report\n"
    "Particular | | Opn | Rec | Issue | Bal\n"
)

OTHER = (
    "GANESH AGENCY\n"
    "Stock Statment : HIMALAYA ZEAL\n"
    "Code  Item Description  Packing  Opening Purchase  Sales  Closing\n"
)


class TestSaleableStockReportDetection(unittest.TestCase):
    def test_detects_layout_from_headers_not_filename(self):
        self.assertTrue(_is_saleable_stock_report_text(HEADER))
        self.assertFalse(_is_saleable_stock_report_text(OTHER))
        self.assertFalse(_is_saleable_stock_report_text("Product Stock Report"))
        self.assertFalse(_is_saleable_stock_report_text(""))

    def test_grand_total_is_not_a_product_line(self):
        """Printed Grand Total qty/value rows stay in totals, never line_items."""
        text = (
            HEADER
            + "AACTARIL SOAP             |75 GM  | |      21.00|         —|       5.00|      16.00\n"
            + "Grand Total               |       | |    7092.00|     591.00|    2665.00|    5018.00\n"
            + "                          |       |A|  176496.51|    7435.70|   39498.46|  663700.61\n"
        )
        result = _parse_saleable_stock_report(text, "rad3DF82.pdf", "pdf")
        self.assertIsNotNone(result)
        names = [str(i.get("product_name") or "").upper() for i in result["line_items"]]
        self.assertEqual(names, ["AACTARIL SOAP"])
        self.assertFalse(any("TOTAL" in n for n in names))
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "saleable_stock_report")
        self.assertEqual(extra.get("total_row_source"), "saleable_grand_total")
        self.assertEqual(extra.get("opening_qty"), 7092.0)
        self.assertEqual(extra.get("receipts_qty"), 591.0)
        self.assertEqual(extra.get("sales_qty"), 2665.0)
        self.assertEqual(extra.get("closing_qty"), 5018.0)
        self.assertEqual(extra.get("opening_value"), 176496.51)
        self.assertEqual(extra.get("receipts_value"), 7435.70)
        self.assertEqual(extra.get("printed_sales_value"), 39498.46)
        self.assertEqual(extra.get("printed_closing_value"), 663700.61)

    def test_1x_pack_and_amount_are_not_opening_or_closing_qty(self):
        text = (
            HEADER
            + "SHIGRU TAB 60TAB-HIMALAY 1 26 0 0 0 26 2852.64\n"
            + "TULASI TAB 60TAB-HIMALAY 1 23 0 2 0 21 1854.12\n"
        )
        result = _parse_saleable_stock_report(text, "rad111EC.pdf", "pdf")
        self.assertIsNotNone(result)
        by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in result["line_items"]
        }
        shigru = next(item for key, item in by_name.items() if "SHIGRU" in key)
        self.assertEqual(shigru["opening_qty"], 26.0)
        self.assertEqual(shigru["receipts_qty"], 0.0)
        self.assertEqual(shigru["sales_qty"], 0.0)
        self.assertEqual(shigru["closing_qty"], 26.0)
        self.assertNotEqual(shigru["closing_qty"], 2852.64)
        self.assertNotEqual(shigru["opening_qty"], 1.0)
        tulasi = next(item for key, item in by_name.items() if "TULASI" in key)
        self.assertEqual(tulasi["opening_qty"], 23.0)
        self.assertEqual(tulasi["sales_qty"], 2.0)
        self.assertEqual(tulasi["closing_qty"], 21.0)

    def test_qty_amt_pairs_are_not_shifted_into_qty(self):
        """Opn/Rec/Issue/Bal each print QTY then Amount; Amount is never qty."""
        text = (
            "SARVAHITHA AYURVEDALAYA PVT. LTD.\n"
            "(From 01/09/2026 To 30/09/2026)\n"
            "Saleable Stock Report\n"
            "Particular |Packing| Opn | Opn | Rec | Rec | Issue | Issue | Bal | Bal\n"
            "           |       | (Q+F)| Amt(Q+F)| (Q+F)| Amt | (Q+F)| Amt | (Q+F)| Amt(Q+F)\n"
            "AACTARIL SOAP 75GM-HIMALAY |1 | 84.00| — | 72.00| 5592.97| 63.00| 5253.68| 93.00| 7224.25\n"
            "ALTHEA LOTION 100ML-HIMALAY |1 | 51.00| 164.13| — | — | 7.00| 1245.99| 44.00| 7221.72\n"
            "ARJUNA TAB 60CAPS-HIMALAY |1 | 75.00| 3309.45| 60.00| 13237.83| 67.00| 15864.79| 68.00| 15002.87\n"
            "AMALAKI TAB 60TAB-HIMALAY |1 | 18.00| —— | 60.00| 13237.74| 19.00| 4475.00| 59.00| 13017.11\n"
            "KOFLET-H LOZENGES (ORANGE) 6TAB-HIMALAY |1 | 195.00| 672.00| 160.00| 3072.01| 95.00| 1856.50| 260.00| 4896.01\n"
            "TULASI SYRUP 200ML-HIMALAY |1 | 25.00| 2885.00| — | — | 5.00| 615.22| 20.00| 2308.00\n"
            "LIV 52 SYRUP S.F 100 ML - HIMALAYA |100 ML | 68.00| — | — | — | 5.00| 608.00| 63.00| 7148.61\n"
        )
        result = _parse_saleable_stock_report(text, "rad111EC.pdf", "pdf")
        self.assertIsNotNone(result)
        by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in result["line_items"]
        }
        aactaril = next(item for key, item in by_name.items() if "AACTARIL" in key)
        self.assertEqual(aactaril["opening_qty"], 84.0)
        self.assertEqual(aactaril["receipts_qty"], 72.0)
        self.assertEqual(aactaril["sales_qty"], 63.0)
        self.assertEqual(aactaril["closing_qty"], 93.0)
        self.assertEqual(aactaril["sales_value"], 5253.68)
        self.assertEqual(aactaril["closing_value"], 7224.25)
        self.assertNotEqual(aactaril["opening_qty"], 1.0)
        althea = next(item for key, item in by_name.items() if "ALTHEA" in key)
        self.assertEqual(althea["opening_qty"], 51.0)
        self.assertEqual(althea["receipts_qty"], 0.0)
        self.assertEqual(althea["sales_qty"], 7.0)
        self.assertEqual(althea["closing_qty"], 44.0)
        arjuna = next(item for key, item in by_name.items() if "ARJUNA" in key)
        self.assertEqual(arjuna["opening_qty"], 75.0)
        self.assertEqual(arjuna["receipts_qty"], 60.0)
        self.assertEqual(arjuna["sales_qty"], 67.0)
        self.assertEqual(arjuna["closing_qty"], 68.0)
        amalaki = next(item for key, item in by_name.items() if "AMALAKI" in key)
        self.assertEqual(amalaki["opening_qty"], 18.0)
        self.assertEqual(amalaki["receipts_qty"], 60.0)
        self.assertEqual(amalaki["sales_qty"], 19.0)
        self.assertEqual(amalaki["closing_qty"], 59.0)
        koflet = next(item for key, item in by_name.items() if "KOFLET-H" in key)
        self.assertEqual(koflet["opening_qty"], 195.0)
        self.assertEqual(koflet["receipts_qty"], 160.0)
        self.assertEqual(koflet["sales_qty"], 95.0)
        self.assertEqual(koflet["closing_qty"], 260.0)
        self.assertNotEqual(koflet["opening_qty"], 672.0)
        tulasi = next(item for key, item in by_name.items() if "TULASI SYRUP" in key)
        self.assertEqual(tulasi["opening_qty"], 25.0)
        self.assertEqual(tulasi["receipts_qty"], 0.0)
        self.assertEqual(tulasi["sales_qty"], 5.0)
        self.assertEqual(tulasi["closing_qty"], 20.0)
        self.assertNotEqual(tulasi["receipts_qty"], 2885.0)
        liv = next(item for key, item in by_name.items() if "LIV 52" in key)
        self.assertEqual(liv["packing"], "100 ML")
        self.assertEqual(liv["opening_qty"], 68.0)
        self.assertEqual(liv["receipts_qty"], 0.0)
        self.assertEqual(liv["sales_qty"], 5.0)
        self.assertEqual(liv["closing_qty"], 63.0)


@unittest.skipUnless(FIXTURE.is_file(), "missing Paras Medicos fixture")
class TestSaleableStockReportFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in cls.result["line_items"]
        }
        cls.names = [
            str(item.get("product_name") or "") for item in cls.result["line_items"]
        ]

    def test_uses_saleable_parser(self):
        self.assertEqual(self.extra.get("extraction_method"), "saleable_stock_report")
        self.assertEqual(len(self.result["line_items"]), 15)
        self.assertEqual(self.result["stockist_name"], "PARAS MEDICOS")
        self.assertIn("HIMALAYA", str(self.result.get("company_name") or "").upper())
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertNotIn("Invoices", self.result)

    def test_date_header_and_totals_are_not_products(self):
        joined = " | ".join(self.names).upper()
        self.assertNotIn("(FROM", joined)
        self.assertNotIn("FROM 01", joined)
        self.assertNotIn("PARTICULAR", joined)
        self.assertNotIn("COMPANY TOTAL", joined)
        self.assertNotIn("FIRM TOTAL", joined)
        self.assertNotIn("GRAND TOTAL", joined)
        self.assertFalse(any(name.strip().startswith("(") for name in self.names))

    def test_sample_rows_qty_not_shifted(self):
        confido = self.by_name["CONFIDO TAB."]
        self.assertEqual(confido["packing"], "1X60 TAB")
        self.assertEqual(confido["opening_qty"], 25.0)
        self.assertEqual(confido["receipts_qty"], 0.0)
        self.assertEqual(confido["sales_qty"], 3.0)
        self.assertEqual(confido["closing_qty"], 22.0)

        gasex = self.by_name["GASEX TAB"]
        self.assertEqual(gasex["packing"], "60 TAB")
        self.assertEqual(gasex["opening_qty"], 54.0)
        self.assertEqual(gasex["sales_qty"], 1.0)
        self.assertEqual(gasex["closing_qty"], 53.0)

        hadjod = self.by_name["HADJOD TAB"]
        self.assertEqual(hadjod["opening_qty"], 23.0)
        self.assertEqual(hadjod["sales_qty"], 6.0)
        self.assertEqual(hadjod["closing_qty"], 17.0)

        liv_rows = [
            item
            for item in self.result["line_items"]
            if str(item.get("product_name") or "").upper() == "LIV.52 SYP"
        ]
        self.assertEqual(len(liv_rows), 2)
        liv200 = next(r for r in liv_rows if r.get("packing") == "200 ML")
        self.assertEqual(liv200["opening_qty"], 303.0)
        self.assertEqual(liv200["receipts_qty"], 1120.0)
        self.assertEqual(liv200["sales_qty"], 307.0)
        self.assertEqual(liv200["closing_qty"], 1116.0)

        bleminor = self.by_name["BLEMINOR ANTIBLCR"]
        self.assertEqual(bleminor["opening_qty"], 15.0)
        self.assertEqual(bleminor["sales_qty"], 0.0)
        self.assertEqual(bleminor["closing_qty"], 15.0)

        tentex = next(
            item
            for item in self.result["line_items"]
            if "TENTEX" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(tentex["opening_qty"], 54.0)
        self.assertEqual(tentex["sales_qty"], 4.0)
        self.assertEqual(tentex["closing_qty"], 50.0)

    def test_printed_company_totals_not_allocated_to_lines(self):
        extra = self.extra
        self.assertEqual(extra.get("opening_qty"), 942.0)
        self.assertEqual(extra.get("receipts_qty"), 1130.0)
        self.assertEqual(extra.get("sales_qty"), 401.0)
        self.assertEqual(extra.get("closing_qty"), 1671.0)
        self.assertEqual(extra.get("opening_value"), 153640.3)
        self.assertEqual(extra.get("receipts_value"), 202161.3)
        self.assertEqual(extra.get("printed_sales_value"), 68099.63)
        self.assertEqual(extra.get("printed_closing_value"), 285333.70)
        for item in self.result["line_items"]:
            self.assertFalse(item.get("sales_value"))
            self.assertFalse(item.get("closing_value"))


if __name__ == "__main__":
    unittest.main()
