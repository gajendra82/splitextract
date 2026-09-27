"""OP / PI / Sale / SI Value / CLQty stock sheet. PTR must not replace SI Value."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _find_stock_sales_op_pi_clqty_header,
    extract_sales_statement,
)

FIXTURE = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August"
    r"\0000734548_2026_08_ZL_12_315_07092026155257.xlsx"
)


class TestOpPiClqtyHeader(unittest.TestCase):
    def test_other_stock_headers_are_not_this_layout(self):
        self.assertIsNone(
            _find_stock_sales_op_pi_clqty_header(
                [["Item", "Pack", "Op.", "Pur", "Sale", "Bal.", "BVal", "SVal"]]
            )
        )
        self.assertIsNone(
            _find_stock_sales_op_pi_clqty_header(
                [["ITEM DESCRIPTION", "OPENING", "RECEIPT", "ISSUE", "CLOSING"]]
            )
        )


@unittest.skipUnless(FIXTURE.is_file(), "missing OP/PI/CLQty workbook")
class TestOpPiClqtyFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            item["product_name"]: item for item in cls.result["line_items"]
        }

    def test_header_and_printed_totals(self):
        self.assertEqual(self.extra.get("extraction_method"), "stock_sales_op_pi_clqty")
        self.assertEqual(self.result["stockist_name"], "ANILA MEDICAL PRIVATE LIMITED")
        self.assertIn("INDORE", self.result["stockist_address"])
        self.assertEqual(self.result["company_name"], "HIMALAYA")
        self.assertEqual(self.result["period_from"], "2026-05-01")
        self.assertEqual(self.result["period_to"], "2026-05-30")
        names = " | ".join(self.by_name).upper()
        self.assertNotIn("DIVISION", names)
        self.assertNotIn("TOTAL VALUE", names)
        self.assertIn("BONNISAN DROPS 30ML", self.by_name)
        self.assertNotIn("......BONNISAN DROPS 30ML", self.by_name)
        self.assertEqual(self.result["totals"]["sales_value"], 196392.95)
        self.assertEqual(self.result["totals"]["closing_value"], 401419.55)
        self.assertEqual(self.extra.get("opening_value"), 353610.92)
        self.assertEqual(self.extra.get("purchase_value"), 231390.19)
        self.assertEqual(self.extra.get("stock_identity_fail_count"), 0)

    def test_si_value_and_clqty_are_not_replaced(self):
        arjuna = self.by_name["ARJUNA CAP (1X60)"]
        self.assertEqual(arjuna["opening_qty"], 36.0)
        self.assertEqual(arjuna["opening_value"], 7942.68)
        self.assertEqual(arjuna["sales_qty"], 18.0)
        self.assertEqual(arjuna["sales_value"], 4152.18)
        self.assertEqual(arjuna["closing_qty"], 18.0)
        self.assertEqual(arjuna["closing_value"], 3971.34)
        self.assertEqual(arjuna["extra"]["mar_qty"], 7.0)
        self.assertEqual(arjuna["extra"]["apr_qty"], 1.0)
        ptr = arjuna["extra"]["ptr"]
        self.assertNotEqual(arjuna["sales_value"], round(18 * ptr, 2))

        brahmi = self.by_name["BRAHMI CAP"]
        self.assertEqual(brahmi["sales_qty"], 0.0)
        self.assertEqual(brahmi["sales_value"], 0.0)
        self.assertEqual(brahmi["closing_qty"], 50.0)
        self.assertEqual(brahmi["closing_value"], 8825.5)

        triphala = self.by_name["TRIPHALA TAB 1X60"]
        self.assertEqual(triphala["opening_qty"], 6.0)
        self.assertEqual(triphala["receipts_qty"], 60.0)
        self.assertEqual(triphala["extra"]["in_qty"], 1.0)
        self.assertEqual(triphala["sales_qty"], 21.0)
        self.assertEqual(triphala["closing_qty"], 46.0)

        liv = self.by_name["LIV 52 SYP 200ML(1 UNIT)"]
        self.assertEqual(liv["opening_qty"], 9.0)
        self.assertEqual(liv["receipts_qty"], 160.0)
        self.assertEqual(liv["extra"]["in_qty"], 2.0)
        self.assertEqual(liv["sales_qty"], 111.0)
        self.assertEqual(liv["extra"]["st_out_qty"], 5.0)
        self.assertEqual(liv["closing_qty"], 55.0)

        confido = self.by_name["CONFIDO TAB (1X60)"]
        self.assertEqual(confido["receipts_qty"], 110.0)
        self.assertEqual(confido["extra"]["in_qty"], 2.0)
        self.assertEqual(confido["sales_qty"], 33.0)
        self.assertEqual(confido["closing_qty"], 79.0)


if __name__ == "__main__":
    unittest.main()
