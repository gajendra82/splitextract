"""Reject SSA dump / Op_Stk on Stock and Sale Op|Pur|Gd.In trailing-money grids."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.sales_statement_extractor import (
    _op_stk_result_should_defer_stock_and_sale_to_vt,
    _result_title_looks_like_plain_stock_and_sale,
    _ssa_dump_result_looks_like_stock_and_sale_misfile,
    _text_looks_like_stock_and_sale_op_pur_gd_chrome,
)
from services.stock_header_resolver import resolve_columns


def _item(
    *,
    packing="60Tab",
    opening=0.0,
    receipts=61.0,
    sales=0.0,
    closing=61.0,
    opening_value=0.0,
    sales_value=0.0,
    closing_value=1084.8,
    flags=None,
    dump_qty=0.0,
):
    extra = {
        "opening_value": opening_value,
        "dump_qty": dump_qty,
        "flags": list(flags or []),
        "field_source": {},
    }
    return {
        "product_name": "BONNISAN LIQ",
        "packing": packing,
        "opening_qty": opening,
        "receipts_qty": receipts,
        "sales_qty": sales,
        "closing_qty": closing,
        "opening_value": opening_value,
        "sales_value": sales_value,
        "closing_value": closing_value,
        "extra": extra,
    }


class StockAndSaleOpPurSsaRejectTests(unittest.TestCase):
    def test_chrome_detects_op_pur_gd_in_grid(self):
        text = (
            "SUSHRUT AYURVED NIKETAN\nStock and Sale\n"
            "Product Pack Op Pur Gd.In Tot In Sale Gd.Out Closing "
            "Purchase Sale Stock Near Exp"
        )
        self.assertTrue(_text_looks_like_stock_and_sale_op_pur_gd_chrome(text))

    def test_chrome_skips_ssa_analysis_and_detail(self):
        self.assertFalse(
            _text_looks_like_stock_and_sale_op_pur_gd_chrome(
                "STOCK & SALES ANALYSIS\nOPENING RECEIPT ISSUE CLOSING DUMP"
            )
        )
        self.assertFalse(
            _text_looks_like_stock_and_sale_op_pur_gd_chrome(
                "Stock and Sales Detail Report\nOp.Qty P.Qty S.Qty Cl.Qty"
            )
        )

    def test_title_plain_stock_and_sale(self):
        self.assertTrue(
            _result_title_looks_like_plain_stock_and_sale(
                {"report_title": "Stock and Sale"}
            )
        )
        self.assertFalse(
            _result_title_looks_like_plain_stock_and_sale(
                {"report_title": "Stock and Sale Report"}
            )
        )
        self.assertFalse(
            _result_title_looks_like_plain_stock_and_sale(
                {"report_title": "STOCK & SALES ANALYSIS"}
            )
        )

    def test_ssa_misfile_receipt_as_opening_tot_in_close(self):
        items = [
            _item(flags=["SSA_DUMP_RECEIPT_AS_OPENING"]),
            _item(
                packing="200ml",
                opening=0.0,
                receipts=61.0,
                sales=0.0,
                closing=61.0,
                closing_value=571.4,
                flags=["SSA_DUMP_RECEIPT_AS_OPENING"],
            ),
            _item(
                packing="30ml",
                opening=110.0,
                receipts=112.0,
                sales=0.0,
                closing=222.0,
                opening_value=0.0,
                closing_value=5000.0,
            ),
            _item(
                packing="60Tab",
                opening=50.0,
                receipts=50.0,
                sales=0.0,
                closing=100.0,
                opening_value=0.0,
                closing_value=4000.0,
            ),
            _item(
                packing="100ml",
                opening=20.0,
                receipts=10.0,
                sales=0.0,
                closing=30.0,
                opening_value=0.0,
                closing_value=900.0,
            ),
        ]
        result = {"report_title": "STOCK & SALES ANALYSIS", "line_items": items}
        self.assertTrue(_ssa_dump_result_looks_like_stock_and_sale_misfile(result))

    def test_ssa_real_dump_sheet_not_misfile(self):
        items = []
        for i in range(6):
            items.append(
                _item(
                    packing="60",
                    opening=10.0 + i,
                    receipts=0.0,
                    sales=2.0,
                    closing=8.0 + i,
                    opening_value=1000.0 + i,
                    sales_value=200.0,
                    closing_value=800.0 + i,
                    dump_qty=8.0 + i,
                    flags=[],
                )
            )
        result = {"report_title": "STOCK & SALES ANALYSIS", "line_items": items}
        self.assertFalse(_ssa_dump_result_looks_like_stock_and_sale_misfile(result))

    def test_op_stk_defers_when_sale_money_missing(self):
        items = [
            _item(
                opening=61.0,
                receipts=0.0,
                sales=16.0,
                closing=45.0,
                sales_value=0.0,
                closing_value=2704.5,
            )
            for _ in range(6)
        ]
        result = {"report_title": "Stock and Sale", "line_items": items}
        self.assertTrue(_op_stk_result_should_defer_stock_and_sale_to_vt(result))

    def test_flag_off_disables_misfile(self):
        items = [
            _item(flags=["SSA_DUMP_RECEIPT_AS_OPENING"]) for _ in range(5)
        ]
        # Make tot-in-as-close pattern
        for it in items:
            it["opening_qty"] = 0.0
            it["receipts_qty"] = 50.0
            it["sales_qty"] = 0.0
            it["closing_qty"] = 50.0
        result = {"report_title": "STOCK & SALES ANALYSIS", "line_items": items}
        with patch.dict(
            "os.environ",
            {"STOCK_SSA_REJECT_STOCK_AND_SALE_OP_PUR": "false"},
            clear=False,
        ):
            self.assertFalse(
                _ssa_dump_result_looks_like_stock_and_sale_misfile(result)
            )

    def test_gd_in_alias_maps_free(self):
        cells = [
            {
                "text": t,
                "x_center": float(i),
                "col_index": i,
                "subheader_text": None,
            }
            for i, t in enumerate(
                [
                    "Product",
                    "Pack",
                    "Op",
                    "Pur",
                    "Gd.In",
                    "Tot In",
                    "Sale",
                    "Gd.Out",
                    "Closing",
                    "Purchase",
                    "Sale",
                    "Stock",
                ]
            )
        ]
        resolved = resolve_columns(cells)
        by = {c["col_index"]: c for c in resolved["columns"]}
        self.assertEqual(by[2]["canonical"], "opening_qty")
        self.assertEqual(by[3]["canonical"], "purchase_qty")
        # Gd.In → free group (neighbour pass may promote free_in).
        self.assertIn(by[4]["canonical"], {"free", "free_in_qty", "ignore"})
        self.assertEqual(by[5]["canonical"], "total_qty")
        self.assertEqual(by[9]["canonical"], "purchase_value")
        self.assertEqual(by[10]["canonical"], "sales_value")
        self.assertEqual(by[11]["canonical"], "closing_value")


if __name__ == "__main__":
    unittest.main()
