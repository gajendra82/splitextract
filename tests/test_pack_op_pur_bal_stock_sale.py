"""Abbreviated pack/Op/Pur/Bal Val Stock and Sale Statement photo helper."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _finalize_pack_op_pur_bal_stock_sale,
    _looks_like_pack_op_pur_bal_stock_sale_text,
    _pack_op_pur_bal_stock_sale_money_weak,
    _strip_trailing_packing_from_name,
    empty_result,
)


class TestPackOpPurBalStockSale(unittest.TestCase):
    def test_text_detector_requires_abbreviated_headers(self):
        self.assertTrue(
            _looks_like_pack_op_pur_bal_stock_sale_text(
                "J R SHAH AND COMPANY\n"
                "Stock and Sale Statement From 28-Jul-26 to 28-Aug-26\n"
                "Item Name pack Op Pur SP Pur Val Sale SS Sp Qty Sale Val Bal. Bal Val\n"
            )
        )
        # Classic ZANDRA Op Stk / Cl Stk must not take this path.
        self.assertFalse(
            _looks_like_pack_op_pur_bal_stock_sale_text(
                "Stock and Sale Statement\n"
                "Item Cd Item Name Op Stk P Qty P Val S Qty S Val Cl Stk Cl Val\n"
            )
        )

    def test_money_weak_detector(self):
        result = empty_result("x.jpeg", "jpeg")
        result["report_title"] = "Stock and Sale Statement"
        result["line_items"] = [
            {
                "product_name": f"ITEM {i}",
                "packing": "60TAB",
                "opening_qty": 1.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 1.0,
                "closing_value": 0.0,
            }
            for i in range(10)
        ]
        self.assertTrue(_pack_op_pur_bal_stock_sale_money_weak(result))
        result["line_items"][0]["sales_value"] = 100.0
        result["line_items"][1]["closing_value"] = 200.0
        result["line_items"][2]["sales_value"] = 50.0
        self.assertFalse(_pack_op_pur_bal_stock_sale_money_weak(result))

    def test_strip_pack_from_name(self):
        self.assertEqual(
            _strip_trailing_packing_from_name("H AACTARIL SOAP 75GM", "75GM"),
            "H AACTARIL SOAP",
        )
        self.assertEqual(
            _strip_trailing_packing_from_name("HABANA 60TABLETS", "60TAB"),
            "HABANA",
        )

    def test_finalize_sets_method_and_skips_banner(self):
        result = empty_result("x.jpeg", "jpeg")
        result["line_items"] = [
            {
                "product_name": "THE HIMALAYA DRUG (ZEAL)",
                "packing": None,
                "opening_qty": 0,
                "sales_qty": 0,
                "closing_qty": 0,
            },
            {
                "product_name": "H AACTARIL SOAP 75GM",
                "packing": "75GM",
                "opening_qty": 53,
                "receipts_qty": 0,
                "sales_qty": 22,
                "sales_value": 1919,
                "closing_qty": 31,
                "closing_value": 2167,
                "extra": {"purchase_value": 0},
            },
        ]
        out = _finalize_pack_op_pur_bal_stock_sale(result)
        self.assertEqual(
            out["totals"]["extra"]["extraction_method"],
            "pack_op_pur_bal_stock_sale_vision",
        )
        self.assertEqual(len(out["line_items"]), 1)
        self.assertEqual(out["line_items"][0]["product_name"], "H AACTARIL SOAP")
        self.assertIsNone(out["line_items"][0]["opening_value"])
        self.assertIsNone(out["line_items"][0]["receipts_value"])

    def test_repair_dropped_purchase_qty(self):
        from services.sales_statement_extractor import (
            _repair_pack_op_pur_bal_dropped_receipts,
        )

        result = empty_result("x.jpeg", "jpeg")
        result["line_items"] = [
            {
                "product_name": "HABANA",
                "packing": "60TAB",
                "opening_qty": 15.0,
                "receipts_qty": 0.0,
                "sales_qty": 27.0,
                "closing_qty": 88.0,
            }
        ]
        out = _repair_pack_op_pur_bal_dropped_receipts(result)
        self.assertEqual(out["line_items"][0]["receipts_qty"], 100.0)

    def test_repair_money_filed_as_pur_qty(self):
        from services.sales_statement_extractor import (
            _repair_pack_op_pur_bal_money_as_qty,
        )

        result = empty_result("x.jpeg", "jpeg")
        result["line_items"] = [
            {
                "product_name": "H CONFIDO TABLETS",
                "packing": "60TAB",
                "opening_qty": 0.0,
                "receipts_qty": 13995.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 13995.0,
                "closing_value": 0.0,
                "extra": {"purchase_value": 0},
            }
        ]
        out = _repair_pack_op_pur_bal_money_as_qty(result)
        item = out["line_items"][0]
        self.assertEqual(item["receipts_qty"], 0.0)
        self.assertEqual(item["closing_qty"], 0.0)
        self.assertEqual(item["extra"]["purchase_value"], 13995.0)

    def test_ss_filed_as_sp_is_relocated(self):
        from services.sales_statement_extractor import _repair_pack_op_pur_bal_ss_filed_as_sp

        result = empty_result("x.jpeg", "jpeg")
        result["line_items"] = [
            {
                "product_name": "H MANJISHTHA TAB",
                "packing": "60TAB",
                "opening_qty": 48.0,
                "receipts_qty": 0.0,
                "sales_qty": 8.0,
                "closing_qty": 39.0,
                "extra": {"purchase_scheme_qty": 1, "sales_scheme_qty": 0},
            },
            {
                "product_name": "H PILEX TABLETS",
                "packing": "60TAB",
                "opening_qty": 106.0,
                "receipts_qty": 100.0,
                "sales_qty": 131.0,
                "closing_qty": 74.0,
                "extra": {"purchase_scheme_qty": 1, "sales_scheme_qty": 0},
            },
        ]
        out = _repair_pack_op_pur_bal_ss_filed_as_sp(result)
        for item in out["line_items"]:
            self.assertEqual(item["extra"]["sales_scheme_qty"], 1.0)
            self.assertEqual(item["extra"]["purchase_scheme_qty"], 0.0)
            self.assertTrue(item["extra"].get("ss_moved_from_sp"))

    def test_tiny_implied_pur_not_fabricated(self):
        from services.sales_statement_extractor import (
            _repair_pack_op_pur_bal_dropped_receipts,
        )

        result = empty_result("x.jpeg", "jpeg")
        result["line_items"] = [
            {
                "product_name": "H KAPIKACHHU TABLETS",
                "packing": "60 TAB",
                "opening_qty": 40.0,
                "receipts_qty": 0.0,
                "sales_qty": 1.0,
                "closing_qty": 40.0,
                "extra": {},
            }
        ]
        out = _repair_pack_op_pur_bal_dropped_receipts(result)
        self.assertEqual(out["line_items"][0]["receipts_qty"], 0.0)
        self.assertFalse(
            (out["line_items"][0].get("extra") or {}).get("receipts_qty_repaired")
        )
    def test_footer_bal_val_leak_cleared_and_restored(self):
        from services.sales_statement_extractor import (
            _repair_pack_op_pur_bal_footer_leaked_into_rows,
            _reconcile_pack_op_pur_bal_totals,
        )

        result = empty_result("x.jpeg", "jpeg")
        result["line_items"] = [
            {
                "product_name": "H ABANA",
                "packing": "60TAB",
                "opening_qty": 15.0,
                "receipts_qty": 100.0,
                "sales_qty": 27.0,
                "sales_value": 4311.0,
                "closing_qty": 88.0,
                "closing_value": 12423.0,
                "extra": {"purchase_value": 14117},
            },
            {
                "product_name": "H TALAKT TAB",
                "packing": "60TAB",
                "opening_qty": 50.0,
                "receipts_qty": 0.0,
                "sales_qty": 4.0,
                "sales_value": 0.0,
                "closing_qty": 46.0,
                "closing_value": 278899.0,
                "extra": {"purchase_value": 0},
            },
            {
                "product_name": "H VRIKSHAMLA TAB",
                "packing": "60TAB",
                "opening_qty": 63.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 63.0,
                "closing_value": 278899.0,
                "extra": {"purchase_value": 0},
            },
        ]
        result["totals"] = {
            "sales_value": 209502.0,
            "closing_value": 570221.0,
            "extra": {"purchase_value": 125277.0, "closing_qty": 197.0},
        }
        out = _repair_pack_op_pur_bal_footer_leaked_into_rows(result)
        self.assertEqual(out["line_items"][1]["closing_value"], 0.0)
        self.assertEqual(out["line_items"][2]["closing_value"], 0.0)
        self.assertEqual(out["totals"]["closing_value"], 278899.0)
        out = _reconcile_pack_op_pur_bal_totals(out)
        self.assertEqual(out["totals"]["closing_value"], 278899.0)
        self.assertEqual(out["totals"]["sales_value"], 209502.0)

    def test_score_ignores_all_zero_identity(self):
        from services.sales_statement_extractor import _pack_op_pur_bal_score

        weak = empty_result("x.jpeg", "jpeg")
        weak["report_title"] = "Stock and Sale Statement"
        weak["line_items"] = [
            {
                "product_name": f"ITEM {i}",
                "packing": "60TAB",
                "opening_qty": 0.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 0.0,
                "closing_value": 0.0,
            }
            for i in range(48)
        ]
        self.assertLess(_pack_op_pur_bal_score(weak), 40)

    def test_jr_shah_text_detector_without_pur_val_ocr(self):
        self.assertTrue(
            _looks_like_pack_op_pur_bal_stock_sale_text(
                "J R SHAH AND COMPANY\n"
                "Stock and Sale Statement From 28-Jul-26 to 28-Aug-26\n"
                "THE HIMALAYA DRUG (ZEAL)\n"
            )
        )


if __name__ == "__main__":
    unittest.main()
