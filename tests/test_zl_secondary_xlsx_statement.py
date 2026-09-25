"""ZL secondary stock tabular .xlsx extraction checks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from services.sales_statement_extractor import (
    _find_zl_secondary_xlsx_header,
    extract_sales_statement,
)

SAMPLE = Path(
    r"C:\Users\amnsa\Downloads\ZL_2026_August\0000700210_2026_08_ZL_18_8184_05092026091719.xlsx"
)


def _write_zl_xlsx(rows) -> str:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    import os

    os.close(fd)
    wb.save(path)
    return path


class TestZlSecondaryHeaderDetect(unittest.TestCase):
    def test_detects_header(self):
        rows = [
            [
                "Customer_name",
                "Material_code",
                "Material_name",
                "Mrp",
                "Secondaryrate",
                "Opening_bal_qty",
                "Primary_qty",
                "Closing_bal_qty",
                "Year",
                "Month",
                "Division",
                "Customer_code",
            ]
        ]
        found = _find_zl_secondary_xlsx_header(rows)
        self.assertIsNotNone(found)
        idx, keys = found
        self.assertEqual(idx, 0)
        self.assertEqual(keys["materialname"], 2)
        self.assertEqual(keys["openingbalqty"], 5)

    def test_marg_header_not_detected(self):
        rows = [
            [
                "PRODUCT DESCRIPTION",
                "OPENING\nSTOCK",
                "PURCHASE\nQUANTITY",
                "CLOSING\nSTOCK",
                "RATE",
            ]
        ]
        self.assertIsNone(_find_zl_secondary_xlsx_header(rows))


class TestZlSecondaryParse(unittest.TestCase):
    def setUp(self):
        self.path = _write_zl_xlsx(
            [
                [
                    "Customer_name",
                    "Material_code",
                    "Material_name",
                    "Mrp",
                    "Secondaryrate",
                    "Opening_bal_qty",
                    "Primary_qty",
                    "Closing_bal_qty",
                    "Year",
                    "Month",
                    "Division",
                    "Customer_code",
                ],
                [
                    "AMBICA AGENCIES",
                    "7002319",
                    "AACTARIL SOAP 75G INDIA",
                    "115",
                    77.68,
                    "60",
                    "0.00",
                    "0",
                    "2026",
                    "8",
                    "ZL",
                    "0000700210",
                ],
                [
                    "AMBICA AGENCIES",
                    "7000004",
                    "ABANA TABS 60 s",
                    "220",
                    148.6,
                    "0",
                    "0.00",
                    "0",
                    "2026",
                    "8",
                    "ZL",
                    "0000700210",
                ],
            ]
        )

    def tearDown(self):
        Path(self.path).unlink(missing_ok=True)

    def test_maps_fields_and_null_sales(self):
        result = extract_sales_statement(
            Path(self.path).read_bytes(), "zl_sample.xlsx"
        )
        self.assertEqual(result["stockist_name"], "AMBICA AGENCIES")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        self.assertEqual(result["totals"]["extra"].get("customer_code"), "0000700210")
        self.assertEqual(result["totals"]["extra"].get("division"), "ZL")
        self.assertIsNone(result["totals"]["sales_value"])
        self.assertIsNone(result["totals"]["closing_value"])
        self.assertEqual(len(result["line_items"]), 2)

        first = result["line_items"][0]
        self.assertEqual(first["product_code"], "7002319")
        self.assertEqual(first["product_name"], "AACTARIL SOAP 75G INDIA")
        self.assertEqual(first["opening_qty"], 60.0)
        self.assertEqual(first["receipts_qty"], 0.0)
        self.assertEqual(first["closing_qty"], 0.0)
        self.assertIsNone(first["sales_qty"])
        self.assertIsNone(first["sales_value"])
        self.assertIsNone(first["closing_value"])
        self.assertEqual(first["extra"]["mrp"], 115.0)
        self.assertEqual(first["extra"]["secondary_rate"], 77.68)

        zero_row = result["line_items"][1]
        self.assertEqual(zero_row["opening_qty"], 0.0)
        self.assertEqual(zero_row["receipts_qty"], 0.0)
        self.assertEqual(zero_row["closing_qty"], 0.0)
        # Must not use Customer_name as product
        self.assertNotEqual(zero_row["product_name"], "AMBICA AGENCIES")


@unittest.skipUnless(SAMPLE.exists(), "ZL sample xlsx not on this machine")
class TestZlSecondarySampleFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(SAMPLE.read_bytes(), SAMPLE.name)

    def test_sample(self):
        self.assertEqual(self.result["stockist_name"], "AMBICA AGENCIES")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertEqual(len(self.result["line_items"]), 71)
        self.assertEqual(self.result["totals"]["extra"].get("customer_code"), "0000700210")
        aact = next(
            i
            for i in self.result["line_items"]
            if i["product_code"] == "7002319"
        )
        self.assertEqual(aact["product_name"], "AACTARIL SOAP 75G INDIA")
        self.assertEqual(aact["opening_qty"], 60.0)
        self.assertEqual(aact["receipts_qty"], 0.0)
        self.assertIsNone(aact["sales_qty"])
        self.assertIsNone(aact["sales_value"])
        self.assertIsNone(aact["closing_value"])


if __name__ == "__main__":
    unittest.main()
