"""Barcode|Name|Pack|Opening|Purchase|Sale|Closing|Pur/Sale/Closing Value.

Spreadsheet Stock And Sales screenshots must keep open+purchase−sale identity
(not SaleRet) and peel full 1X60'S packs off product names.
"""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _main_stock_peel_pack_into_field,
    _ssa_dump_items_look_like_sale_closing_only,
    _ssa_oric_peel_trailing_pack,
    _stock_identity_kind,
    empty_line_item,
    empty_result,
)
from services.stock_vision_table import _apply_fields_to_item, _stamp_vision_result


class BarcodeNamePackOpenPurSaleTests(unittest.TestCase):
    def test_peel_full_1x_pack_from_name(self):
        name, pack = _ssa_oric_peel_trailing_pack("CONFIDO TAB 1X60'S")
        self.assertEqual(name, "CONFIDO TAB")
        self.assertEqual(pack, "1X60'S")
        item = empty_line_item()
        item["product_name"] = "ABANA TAB. 1X60'S"
        item["packing"] = None
        _main_stock_peel_pack_into_field(item)
        self.assertEqual(item["product_name"], "ABANA TAB.")
        self.assertEqual(item["packing"], "1X60'S")

    def test_identity_ignores_null_sale_return_placeholders(self):
        result = empty_result("page_1.jpg", "jpg")
        result["report_title"] = "Stock And Sales"
        for _ in range(3):
            item = empty_line_item()
            item["product_name"] = "CONFIDO TAB"
            item["opening_qty"] = 34.0
            item["receipts_qty"] = 500.0
            item["sales_qty"] = 523.0
            item["closing_qty"] = 11.0
            item["extra"] = {
                "sale_return": None,
                "exp_damage": None,
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                    "sales_return_qty": "missing",
                    "expiry_damage_qty": "missing",
                },
            }
            result["line_items"].append(item)
        result["totals"]["extra"]["extraction_method"] = "vision_table"
        self.assertEqual(
            _stock_identity_kind(result),
            "opening_receipts_sales_closing",
        )

    def test_absent_extra_fields_do_not_invent_null_keys(self):
        item = _apply_fields_to_item(
            {
                "product_name": "CONFIDO TAB",
                "pack": "1X60'S",
                "opening_qty": "34",
                "purchase_qty": "500",
                "sales_qty": "523",
                "closing_qty": "11",
                "purchase_value": "1",
                "sales_value": "2",
                "closing_value": "3",
            },
            unreadable_cols=set(),
            col_to_canon={
                0: "product_name",
                1: "pack",
                2: "opening_qty",
                3: "purchase_qty",
                4: "sales_qty",
                5: "closing_qty",
                6: "purchase_value",
                7: "sales_value",
                8: "closing_value",
            },
            row_index=0,
            printed_fields=(
                "product_name",
                "pack",
                "opening_qty",
                "purchase_qty",
                "sales_qty",
                "closing_qty",
                "purchase_value",
                "sales_value",
                "closing_value",
            ),
        )
        extra = item.get("extra") or {}
        self.assertNotIn("sale_return", extra)
        self.assertNotIn("exp_damage", extra)
        fs = extra.get("field_source") or {}
        self.assertEqual(fs.get("sales_return_qty"), "missing")
        self.assertEqual(fs.get("expiry_damage_qty"), "missing")

    def test_stamp_open_purchase_sale_closing_kind(self):
        column_map = [
            {"col_index": 0, "canonical": "ignore", "header_text": "Barcode"},
            {"col_index": 1, "canonical": "product_name", "header_text": "Name"},
            {"col_index": 2, "canonical": "pack", "header_text": "Pack"},
            {"col_index": 3, "canonical": "opening_qty", "header_text": "Opening"},
            {"col_index": 4, "canonical": "purchase_qty", "header_text": "Purchase"},
            {"col_index": 5, "canonical": "sales_qty", "header_text": "Sale"},
            {"col_index": 6, "canonical": "closing_qty", "header_text": "Closing"},
            {"col_index": 7, "canonical": "purchase_value", "header_text": "Pur Value"},
            {"col_index": 8, "canonical": "sales_value", "header_text": "Sale Value"},
            {"col_index": 9, "canonical": "closing_value", "header_text": "Closing Value"},
        ]
        item = empty_line_item()
        item["product_name"] = "CONFIDO TAB"
        item["opening_qty"] = 34.0
        item["receipts_qty"] = 500.0
        item["sales_qty"] = 523.0
        item["closing_qty"] = 11.0
        stamped = _stamp_vision_result(
            filename="page_1.jpg",
            ext="jpg",
            line_items=[item],
            column_map=column_map,
            classified={"valid_ratio": 1.0, "row_status_counts": {}},
            errors=[],
            gemini_calls=1,
            gemini_budget=2,
            stockist_name="SOUTH DELHI DISTRIBUTORS",
        )
        self.assertEqual(
            stamped["totals"]["extra"].get("stock_identity_kind"),
            "opening_receipts_sales_closing",
        )

    def test_purchase_sheet_not_sale_closing_misfile(self):
        items = []
        for opening, purchase, sales, closing in (
            (34.0, 500.0, 523.0, 11.0),
            (20.0, 500.0, 501.0, 19.0),
            (2699.0, 500.0, 1548.0, 1651.0),
            (0.0, 210.0, 210.0, 0.0),
            (381.0, 0.0, 240.0, 141.0),
        ):
            item = empty_line_item()
            item["opening_qty"] = opening
            item["receipts_qty"] = purchase
            item["sales_qty"] = sales
            item["sales_value"] = 100.0
            item["closing_qty"] = closing
            item["closing_value"] = 50.0
            item["extra"] = {"dump_qty": 0.0, "flags": []}
            items.append(item)
        self.assertFalse(_ssa_dump_items_look_like_sale_closing_only(items))


if __name__ == "__main__":
    unittest.main()
