"""ItemCode + STOCK AND SALES (non-CONSOLIDATED) Op.Qty words path.

Ramesh-style PDFs were excluded from the words parser because of ItemCode,
then fell through to OCR and produced garbage product names (NO./From/…).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from services.sales_statement_extractor import (
    _is_meher_stock_sales_consolidated_text,
    _is_stock_sales_op_qty_val_words_text,
    _stockist_from_op_qty_val_letterhead,
    extract_sales_statement,
)


_SAMPLE_TEXT = """
ItemCode
Item
Pack
Op.Qty
Op.Val
P.Qty
PFQty
P.Val
S.Qty
S.Val
Cls.Qty
Cls.Val
Others
7212
ARJUNA CAPSULES
60`S
23
5074.49
-
0
-
4
954.57
19
4191.97
-
RAMESH PHARMA
STOCK AND SALES
NO. 166/A, K-33/A, Mysuru
From  01-Sep-2026  To 30-Sep-2026 (Sale Report Updated Till : 16-Oct-2026)
Company:HIMALAYA WELLNESS CO-ZANDRA
Address:
Page No.- 1
"""


class TestOpQtyValItemCodeRouting(unittest.TestCase):
    def test_non_consolidated_itemcode_uses_words_path(self):
        self.assertFalse(_is_meher_stock_sales_consolidated_text(_SAMPLE_TEXT))
        self.assertTrue(_is_stock_sales_op_qty_val_words_text(_SAMPLE_TEXT))

    def test_consolidated_itemcode_still_excluded_from_words(self):
        blob = _SAMPLE_TEXT.replace(
            "STOCK AND SALES\n", "STOCK AND SALES (CONSOLIDATED)\n"
        )
        self.assertTrue(_is_meher_stock_sales_consolidated_text(blob))
        self.assertFalse(_is_stock_sales_op_qty_val_words_text(blob))

    def test_stockist_above_stock_and_sales_title(self):
        name = _stockist_from_op_qty_val_letterhead(_SAMPLE_TEXT)
        self.assertEqual(name, "RAMESH PHARMA")


_SOURCE = Path("/var/www/html/splitextract/Ramesh.pdf")


class TestRameshOpQtyValSource(unittest.TestCase):
    def test_ramesh_pdf_extracts_products_period_stockist(self):
        if not _SOURCE.is_file():
            self.skipTest("Ramesh.pdf not on this machine")
        with patch.dict(
            "os.environ",
            {"STOCK_OP_QTY_VAL_CONSOLIDATED_NATIVE": "true"},
        ):
            result = extract_sales_statement(_SOURCE.read_bytes(), _SOURCE.name)
        self.assertEqual(
            (result.get("totals") or {}).get("extra", {}).get("extraction_method"),
            "stock_sales_op_qty_val_words",
        )
        self.assertEqual(result.get("stockist_name"), "RAMESH PHARMA")
        self.assertEqual(result.get("period_from"), "2026-09-01")
        self.assertEqual(result.get("period_to"), "2026-09-30")
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 80)
        by_name = {it["product_name"]: it for it in items}
        self.assertNotIn("NO.", by_name)
        self.assertNotIn("From", by_name)
        arjuna = by_name["ARJUNA CAPSULES"]
        self.assertEqual(arjuna.get("opening_qty"), 23.0)
        self.assertEqual(arjuna.get("sales_qty"), 4.0)
        self.assertEqual(arjuna.get("closing_qty"), 19.0)
        self.assertEqual(arjuna.get("sales_value"), 954.57)
        self.assertEqual(arjuna.get("closing_value"), 4191.97)


if __name__ == "__main__":
    unittest.main()
