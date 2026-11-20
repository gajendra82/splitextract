"""Marg SALE PURCHASES / PURCHASE SALES text PDF — qty columns from the two-row header.

The generic semantic table mapped TOTAL STOCK as purchase and left sales/closing
at 0. This layout already has an XLS parser; PDF/TXT dumps reuse it.
"""

from __future__ import annotations

import unittest

import fitz

from services.sales_statement_extractor import (
    _find_marg_sale_purchase_analysis_header,
    _is_ssa_opening_receipt_issue_value_text,
    _parse_marg_sale_purchase_analysis_text,
    extract_sales_statement,
)


HEADER = """\
ARAVALI WELLNESS PVT. LTD.
AMAR COMPLEX,KASAN ROAD,VPO.MANESAR,GURUGRAM-122051
STOCK & SALES ANALYSIS  (HIMALAYA ZEAL) 01/09/2026 - 23/09/2026  Reorder : Sale X 1.50
----------------------------------------------------------------------------------------------------------------------------------------
ITEM DESCRIPTION                              OPENING               SALE  REPL./    TOTAL          PURCHASE   REPL./  CLOSING   RE-
                                               STOCK   PURCHASES  RETURN  OTHERS    STOCK    SALES   RETURN   OTHERS   STOCK   ORDER   AUG N/EXP
----------------------------------------------------------------------------------------------------------------------------------------
"""

ROWS = """\
AACTARIL SOAP 75GM                                61        0        0        0       61        0        0        0       61       -     0     0
ABANA TABLET 60'S                                 44        0        0        0       44        1        0        0       43       -     0     2
CLARINA CREAM 30GM                                24        0        2        0       26        0        0        0       26       -     0    26
HAIRZONE SOLUTION  60ML                           15        0        0        0       15        1        0        0       14       -     0     0
LIV 52 SYRUP 100ML                               273        0        0        0      273      253        0        0       20   359.5     0     0
"""

FOOTER = """\
----------------------------------------------------------------------------------------------------------------------------------------
Total Quantity                                  3093        0       10        0     3103      961        0        0     2142     618             0     0
Value in Rs.                                  381687        0     1047        0   382734   138206        0        0   259092   71000             0K
------------------------------------------------------------------------------------------------------------------------------------
LAST MONTH SALE        AUGUST
QUANTITY                    0
VALUE                    0.00
"""

STATEMENT = HEADER + ROWS + FOOTER

BUSY_SSA = """
SOME MEDICAL
STOCK & SALES ANALYSIS 01-08-2026 - 31-08-2026
ITEM DESCRIPTION          OPENING     PURCHASE      TOTAL       SALES      CLOSING
                           STOCK   QTY   FREE      STOCK    QTY  FREE      STOCK
ABANA TAB 60S                 10     0      0        10      2     0         8
"""

OPENING_RECEIPT = """
SOME MEDICAL
ITEM DESCRIPTION    OPENING    RECEIPT    ISSUE    CLOSING
ABANA TAB 60S           10         0        2         8
"""


def _pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    y = 36
    for line in text.splitlines():
        page.insert_text((20, y), line, fontsize=7, fontname="cour")
        y += 11
    data = doc.tobytes()
    doc.close()
    return data


def _by_name(result):
    return {item["product_name"]: item for item in result["line_items"]}


class TestMargSalePurchaseAnalysisText(unittest.TestCase):
    def test_other_ssa_headers_are_not_claimed(self):
        self.assertIsNone(
            _parse_marg_sale_purchase_analysis_text(BUSY_SSA, "busy.pdf", "pdf")
        )
        self.assertIsNone(
            _parse_marg_sale_purchase_analysis_text(
                OPENING_RECEIPT, "ori.pdf", "pdf"
            )
        )
        self.assertFalse(_is_ssa_opening_receipt_issue_value_text(STATEMENT))

    def test_aug_nexp_child_columns_do_not_block_the_header(self):
        rows = [
            ["ITEM DESCRIPTION", "OPENING", "SALE", "REPL", "TOTAL", "PURCHASE", "REPL", "CLOSING", "RE"],
            ["", "STOCK", "PURCHASES", "RETURN", "OTHERS", "STOCK", "SALES", "RETURN", "OTHERS", "STOCK", "ORDER", "AUG", "N/EXP"],
        ]
        found = _find_marg_sale_purchase_analysis_header(rows)
        self.assertIsNotNone(found)
        _idx, roles = found
        self.assertEqual(roles["opening_qty"], 1)
        self.assertEqual(roles["sales_qty"], 6)
        self.assertEqual(roles["closing_qty"], 9)
        self.assertNotIn("aug", roles)

    def test_text_maps_sale_purchases_and_purchase_sales(self):
        parsed = _parse_marg_sale_purchase_analysis_text(
            STATEMENT, "aravali.pdf", "pdf"
        )
        self.assertIsNotNone(parsed)
        extra = (parsed.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "marg_sale_purchase_analysis")
        self.assertEqual(parsed["stockist_name"], "ARAVALI WELLNESS PVT. LTD.")
        self.assertIn("AMAR COMPLEX", parsed["stockist_address"] or "")
        self.assertEqual(parsed["company_name"], "HIMALAYA ZEAL")
        self.assertEqual(parsed["period_from"], "2026-09-01")
        self.assertEqual(parsed["period_to"], "2026-09-23")
        names = [item["product_name"] for item in parsed["line_items"]]
        self.assertEqual(
            names,
            [
                "AACTARIL SOAP 75GM",
                "ABANA TABLET 60'S",
                "CLARINA CREAM 30GM",
                "HAIRZONE SOLUTION",
                "LIV 52 SYRUP 100ML",
            ],
        )
        by_name = _by_name(parsed)
        aactaril = by_name["AACTARIL SOAP 75GM"]
        self.assertEqual(aactaril["opening_qty"], 61.0)
        self.assertEqual(aactaril["receipts_qty"], 0.0)
        self.assertEqual(aactaril["sales_qty"], 0.0)
        self.assertEqual(aactaril["closing_qty"], 61.0)
        self.assertIsNone(aactaril["sales_value"])
        self.assertIsNone(aactaril["closing_value"])
        abana = by_name["ABANA TABLET 60'S"]
        self.assertEqual(abana["opening_qty"], 44.0)
        self.assertEqual(abana["sales_qty"], 1.0)
        self.assertEqual(abana["closing_qty"], 43.0)
        clarina = by_name["CLARINA CREAM 30GM"]
        self.assertEqual(clarina["sales_return_qty"], 2.0)
        self.assertEqual(clarina["subtotal_qty"], 26.0)
        self.assertEqual(clarina["closing_qty"], 26.0)
        hairzone = by_name["HAIRZONE SOLUTION"]
        self.assertEqual(hairzone["packing"], "60ML")
        self.assertEqual(hairzone["opening_qty"], 15.0)
        self.assertEqual(hairzone["sales_qty"], 1.0)
        self.assertEqual(hairzone["closing_qty"], 14.0)
        liv = by_name["LIV 52 SYRUP 100ML"]
        self.assertEqual(liv["opening_qty"], 273.0)
        self.assertEqual(liv["sales_qty"], 253.0)
        self.assertEqual(liv["closing_qty"], 20.0)
        self.assertEqual(liv["reorder_qty"], 359.5)
        self.assertEqual(parsed["totals"]["opening_qty"], 3093.0)
        self.assertEqual(parsed["totals"]["sales_qty"], 961.0)
        self.assertEqual(parsed["totals"]["closing_qty"], 2142.0)
        self.assertEqual(parsed["totals"]["sales_value"], 138206.0)
        self.assertEqual(parsed["totals"]["closing_value"], 259092.0)
        self.assertEqual(extra.get("opening_value"), 381687.0)
        self.assertEqual(extra.get("purchase_value"), 0.0)


class TestMargSalePurchaseAnalysisPdfExtract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(
            _pdf_bytes(STATEMENT), "aravali-zeal.pdf"
        )
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = _by_name(cls.result)

    def test_extract_uses_marg_parser_not_semantic_table(self):
        self.assertEqual(
            self.extra.get("extraction_method"), "marg_sale_purchase_analysis"
        )
        names = " | ".join(self.by_name).upper()
        self.assertNotIn("QUANTITY", names)
        self.assertNotIn("LAST MONTH", names)
        self.assertNotIn("VALUE", names)

    def test_printed_qtys_are_not_zeroed(self):
        abana = self.by_name["ABANA TABLET 60'S"]
        self.assertEqual(abana["opening_qty"], 44.0)
        self.assertEqual(abana["receipts_qty"], 0.0)
        self.assertEqual(abana["sales_qty"], 1.0)
        self.assertEqual(abana["closing_qty"], 43.0)
        liv = self.by_name["LIV 52 SYRUP 100ML"]
        self.assertEqual(liv["sales_qty"], 253.0)
        self.assertEqual(liv["closing_qty"], 20.0)
        self.assertIsNone(abana.get("sales_value"))
        self.assertIsNone(abana.get("closing_value"))
        self.assertEqual(self.result["totals"]["sales_value"], 138206.0)
        self.assertEqual(self.result["totals"]["closing_value"], 259092.0)


if __name__ == "__main__":
    unittest.main()
