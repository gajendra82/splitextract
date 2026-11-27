"""Phone Excel SALE AND STOCK ANALYSIS grid (#26989 / 466.jpg).

Sale* is sales_qty. Opening is not forced to 0. Footer rupees are not qtys.
Other layouts are left untouched.
"""

from __future__ import annotations

import unittest

from services.sale_and_stock_analysis_grid import (
    apply_sale_and_stock_analysis_grid,
    build_sale_and_stock_analysis_grid_prompt,
    grid_needs_prompt_retry,
    headers_look_like_sale_and_stock_analysis_grid,
    opening_sale_receipt_failure,
    prefer_grid_read,
    restore_clipped_leading_name,
    text_looks_like_sale_and_stock_analysis_grid,
)
from services.stock_vision_table import build_table_prompt

HEADERS = [
    "Item Name",
    "Pack",
    "Opening",
    "Purchase",
    "Purchase Free",
    "Purchase Return",
    "Sale*",
    "Sale Free",
    "Sale Return",
    "Others",
    "Closing",
]

# Printed cells from the phone screenshot (Sale* is column 6).
GROUND = [
    ("CTARIL SOAP", "75GM", 47, 144, None, None, 27, None, None, None, 164, "ACTARIL SOAP"),
    ("JANA TAB", "60'S", 190, None, None, None, 97, None, None, None, 99, "ABANA TAB"),
    ("ONFIDO TAB", "60'S", 514, 200, None, None, 164, None, None, None, 550, "CONFIDO TAB"),
    ("V-52 SYP 200ML", "200ML", 1876, None, None, None, 1255, None, None, None, 623, "LIV-52 SYP 200ML"),
    ("HERBOLAX TAB", "100'S", 188, 200, None, None, 78, None, None, None, 310, "HERBOLAX TAB"),
]


def _item(name, pack, cells, *, opening=0, receipts=0, sales=0, closing=0):
    return {
        "product_name": name,
        "packing": pack,
        "opening_qty": opening,
        "receipts_qty": receipts,
        "purchase_qty": receipts,
        "sales_qty": sales,
        "closing_qty": closing,
        "extra": {
            "vision_column_headers": list(HEADERS),
            "vision_raw_cells": list(cells),
            "field_source": {},
        },
    }


def _bad_result():
    """Observed #26989 shape: opening wiped, Sale* parked in receipts."""
    items = []
    for name, pack, op, pur, pfree, pret, sale, sfree, sret, others, close, _full in GROUND:
        # ABANA-style: Sale* stored as receipts and sales cleared.
        # ACTARIL-style closing rewritten to purchase - sale (144 - 27 = 117).
        stored_close = 117 if name.startswith("CTARIL") else 0
        stored_sales = 27 if name.startswith("CTARIL") else 0
        stored_receipts = sale if not name.startswith("CTARIL") else pur
        items.append(
            _item(
                name,
                pack,
                [name, pack, op, pur, pfree, pret, sale, sfree, sret, others, close],
                opening=0,
                receipts=stored_receipts or 0,
                sales=stored_sales,
                closing=stored_close,
            )
        )
    items.append(
        _item(
            "tal (HM4)",
            None,
            ["tal (HM4)", None, 1525348.73, 425709.11, 0, 0, 793445.19, 0, 0, 0, 1162789.74],
            opening=0,
            receipts=0,
            sales=0,
            closing=0,
        )
    )
    return {
        "report_title": "SALE AND STOCK ANALYSIS",
        "line_items": items,
        "totals": {"sales_qty": 27, "sales_value": None, "closing_value": None, "extra": {}},
    }


class DetectorTests(unittest.TestCase):
    def test_phone_excel_title_and_headers(self):
        peek = (
            "D R DISTRIBUTORS PVT LTD\n"
            "SALE AND STOCK ANALYSIS\n"
            "Item Name Pack Opening Purchase Purchase Free Purchase Return "
            "Sale* Sale Free Sale Return Others Closing\n"
        )
        self.assertTrue(text_looks_like_sale_and_stock_analysis_grid(peek))
        self.assertTrue(headers_look_like_sale_and_stock_analysis_grid(HEADERS))

    def test_other_layouts_do_not_match(self):
        aastha = (
            "STOCK AND SALES ANALYSIS\n"
            "PRODUCT PACK OPENING PURCHASE FREE P.RETURN FREE SALE FREE "
            "S.RETURN FREE OTHERS CLOSING\n"
        )
        pharm = (
            "Stock and Sale Report\n"
            "Op. Pur Sale Bal. BVal SVal Order Jul Jun\n"
        )
        lstsl = "LstSL Open Recd Sales Close Stk.Value"
        opstk = "OPSTK PURCH SALE STK VAL IN/OT STOCK"
        qty_value = (
            "STOCK & SALES ANALYSIS OPENING QTY OPENING VALUE "
            "RECEIPT QTY RECEIPT VALUE ISSUE QTY ISSUE VALUE CLOSING QTY"
        )
        opening_stock = (
            "SALE AND STOCK ANALYSIS OPENING STOCK PURCHASES SALE RETURN "
            "CLOSING STOCK SALES"
        )
        self.assertFalse(text_looks_like_sale_and_stock_analysis_grid(aastha))
        self.assertFalse(text_looks_like_sale_and_stock_analysis_grid(pharm))
        self.assertFalse(text_looks_like_sale_and_stock_analysis_grid(lstsl))
        self.assertFalse(text_looks_like_sale_and_stock_analysis_grid(opstk))
        self.assertFalse(text_looks_like_sale_and_stock_analysis_grid(qty_value))
        self.assertFalse(text_looks_like_sale_and_stock_analysis_grid(opening_stock))

    def test_generic_prompt_is_unchanged(self):
        generic = build_table_prompt()
        dedicated = build_sale_and_stock_analysis_grid_prompt()
        self.assertNotIn("sale_and_stock_analysis_grid", generic)
        self.assertIn("sale_and_stock_analysis_grid", dedicated)
        self.assertIn("Sale*", dedicated)
        self.assertIn("Purchase Free", dedicated)


class NormalizeGroundTruthTests(unittest.TestCase):
    def test_before_signature_and_after_five_skus(self):
        before = _bad_result()
        self.assertTrue(opening_sale_receipt_failure(before))
        self.assertEqual(before["totals"]["sales_qty"], 27)
        by_before = {i["product_name"]: i for i in before["line_items"]}
        self.assertEqual(by_before["JANA TAB"]["receipts_qty"], 97)
        self.assertEqual(by_before["JANA TAB"]["sales_qty"], 0)
        self.assertEqual(by_before["CTARIL SOAP"]["opening_qty"], 0)
        self.assertEqual(by_before["CTARIL SOAP"]["closing_qty"], 117)
        self.assertEqual(by_before["V-52 SYP 200ML"]["sales_qty"], 0)

        after = apply_sale_and_stock_analysis_grid(before)
        extra = after["totals"]["extra"]
        self.assertEqual(extra.get("layout"), "sale_and_stock_analysis_grid")
        names = [i["product_name"] for i in after["line_items"]]
        self.assertNotIn("tal (HM4)", names)
        by = {i["product_name"]: i for i in after["line_items"]}
        expect = {
            "ACTARIL SOAP": (47, 144, 27, 164),
            "ABANA TAB": (190, None, 97, 99),
            "CONFIDO TAB": (514, 200, 164, 550),
            "LIV-52 SYP 200ML": (1876, None, 1255, 623),
            "HERBOLAX TAB": (188, 200, 78, 310),
        }
        for name, (op, pur, sale, close) in expect.items():
            row = by[name]
            self.assertEqual(row["opening_qty"], op, name)
            self.assertEqual(row["purchase_qty"], pur, name)
            self.assertEqual(row["receipts_qty"], pur, name)
            self.assertEqual(row["sales_qty"], sale, name)
            self.assertEqual(row["closing_qty"], close, name)
        self.assertEqual(after["totals"]["sales_qty"], 27 + 97 + 164 + 1255 + 78)
        self.assertFalse(opening_sale_receipt_failure(after))
        self.assertEqual(after["totals"].get("sales_value"), 793445.19)

    def test_clipped_names_need_matching_pack(self):
        self.assertEqual(restore_clipped_leading_name("CTARIL SOAP", "75GM"), "ACTARIL SOAP")
        self.assertEqual(restore_clipped_leading_name("JANA TAB", "60'S"), "ABANA TAB")
        self.assertEqual(restore_clipped_leading_name("ONFIDO TAB", "60'S"), "CONFIDO TAB")
        self.assertEqual(
            restore_clipped_leading_name("V-52 SYP 200ML", "200ML"),
            "LIV-52 SYP 200ML",
        )
        self.assertIsNone(restore_clipped_leading_name("JANA TAB", "200ML"))
        self.assertIsNone(restore_clipped_leading_name("ACTARIL SOAP", "75GM"))

    def test_other_layout_is_not_rewritten(self):
        item = {
            "product_name": "ONFIDO TAB",
            "packing": "60'S",
            "opening_qty": 10,
            "receipts_qty": 4,
            "sales_qty": 3,
            "closing_qty": 11,
            "extra": {
                "vision_column_headers": ["Op.Qty", "Pur.Qty", "Sale.Qty", "Bal"],
            },
        }
        result = {
            "report_title": "Mfac SAS Summary",
            "line_items": [item],
            "totals": {"sales_qty": 3, "extra": {"layout": "op_qty_pur_sale_bal"}},
        }
        out = apply_sale_and_stock_analysis_grid(result)
        self.assertEqual(out["line_items"][0]["opening_qty"], 10)
        self.assertEqual(out["line_items"][0]["sales_qty"], 3)
        self.assertEqual(out["line_items"][0]["product_name"], "ONFIDO TAB")
        self.assertEqual(out["totals"]["extra"]["layout"], "op_qty_pur_sale_bal")

    def test_retry_only_when_signature_and_budget_remain(self):
        bad = _bad_result()
        self.assertTrue(
            grid_needs_prompt_retry(
                bad, used_dedicated_prompt=False, calls=1, budget=2
            )
        )
        self.assertFalse(
            grid_needs_prompt_retry(
                bad, used_dedicated_prompt=True, calls=1, budget=2
            )
        )
        self.assertFalse(
            grid_needs_prompt_retry(
                bad, used_dedicated_prompt=False, calls=2, budget=2
            )
        )
        good = apply_sale_and_stock_analysis_grid(_bad_result())
        self.assertFalse(
            grid_needs_prompt_retry(
                good, used_dedicated_prompt=False, calls=1, budget=2
            )
        )
        self.assertTrue(prefer_grid_read(bad, good))
        self.assertFalse(prefer_grid_read(good, bad))


if __name__ == "__main__":
    unittest.main()
