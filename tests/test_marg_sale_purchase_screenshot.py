"""Phone screenshot of Marg OPENING / SALE PURCHASES / CLOSING STOCK.

Gemini was assigning the next row's quantities to ABANA TAB. The grid
reader keeps each product on its own row. Other layouts are not this grid.
"""
import unittest
from pathlib import Path

from services.sales_statement_extractor import extract_sales_statement


SHOT = Path(
    "/var/www/html/splitextract/0000729776_2026_08_ZL_24_8273_07092026131726.png"
)


def _item(result, name):
    needle = name.upper()
    return next(
        item
        for item in result["line_items"]
        if needle in (item.get("product_name") or "").upper()
    )


@unittest.skipUnless(SHOT.is_file(), "missing Ambrish Enterprises screenshot")
class TestMargSalePurchaseScreenshot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(SHOT.read_bytes(), SHOT.name)

    def test_abana_keeps_its_own_row(self):
        self.assertEqual(
            (self.result.get("totals") or {}).get("extra", {}).get("extraction_method"),
            "marg_sale_purchase_analysis",
        )
        self.assertEqual(self.result["stockist_name"], "AMBRISH ENTERPRISES")
        self.assertIn("VASUDEV PLAZA", self.result["stockist_address"] or "")
        self.assertIn("HIMALAYA", self.result["company_name"] or "")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")

        aactaril = _item(self.result, "AACTARIL")
        self.assertEqual(aactaril["opening_qty"], 58.0)
        self.assertEqual(aactaril["sales_qty"], 0.0)
        self.assertEqual(aactaril["closing_qty"], 58.0)

        abana = _item(self.result, "ABANA")
        self.assertEqual(abana["opening_qty"], 85.0)
        self.assertEqual(abana["receipts_qty"], 0.0)
        self.assertEqual(abana["sales_qty"], 1.0)
        self.assertEqual(abana["closing_qty"], 84.0)

        althea = _item(self.result, "ALTHEA")
        self.assertEqual(althea["opening_qty"], 3.0)
        self.assertEqual(althea["sales_qty"], 0.0)
        self.assertEqual(althea["closing_qty"], 3.0)

        bonnisan_d = _item(self.result, "BONNISAN D")
        self.assertEqual(bonnisan_d["opening_qty"], 45.0)
        self.assertEqual(bonnisan_d["sales_qty"], 6.0)
        self.assertEqual(bonnisan_d["closing_qty"], 39.0)

        bonnisan_l = _item(self.result, "BONNISAN L")
        self.assertEqual(bonnisan_l["opening_qty"], 20.0)
        self.assertEqual(bonnisan_l["sales_qty"], 0.0)
        self.assertEqual(bonnisan_l["closing_qty"], 20.0)

        gasex = _item(self.result, "GASEX TAB")
        self.assertEqual(gasex["opening_qty"], 94.0)
        self.assertEqual(gasex["sales_qty"], 41.0)
        self.assertEqual(gasex["closing_qty"], 53.0)

        hiora = _item(self.result, "HIORA K TP")
        self.assertEqual(hiora["opening_qty"], 14.0)
        self.assertEqual(hiora["sales_qty"], 10.0)
        self.assertEqual(hiora["closing_qty"], 4.0)

        lukol = _item(self.result, "LUKOL TAB")
        self.assertEqual(lukol["opening_qty"], 41.0)
        self.assertEqual(lukol["sales_qty"], 9.0)
        self.assertEqual(lukol["closing_qty"], 32.0)
