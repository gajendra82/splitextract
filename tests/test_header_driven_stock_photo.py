"""Header-driven stock photos, and Zandra sheets staying off the Jun/Jul reader."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _apply_header_driven_stock_validation,
    _ensure_stock_qty_value_fields,
    _header_driven_expected_closing,
    _header_driven_fix_period,
    _header_driven_is_marg_issue_grid,
    _header_driven_items,
    _header_driven_merge_items,
    _header_driven_separate_issue_closing,
    _repair_marg_mexp_missing_issue,
    _header_driven_is_jun_jul,
    _header_driven_role,
    _header_driven_rows_ok,
    _pharma_hub_jun_jul_needs_reread,
    _pharma_hub_party,
    empty_line_item,
    empty_result,
)


ROLES = {
    "opening_qty",
    "receipts_qty",
    "total_qty",
    "sales_qty",
    "sale_return_qty",
    "expiry_qty",
    "shortage_qty",
    "closing_qty",
    "closing_value",
}


class TestHeaderDrivenStockPhoto(unittest.TestCase):
    def test_roles_follow_printed_headers(self):
        self.assertEqual(_header_driven_role("Opening"), "opening_qty")
        self.assertEqual(_header_driven_role("Purchase"), "receipts_qty")
        self.assertEqual(_header_driven_role("SalesRet"), "sale_return_qty")
        self.assertEqual(_header_driven_role("Sales"), "sales_qty")
        self.assertEqual(_header_driven_role("Exp/Dmg"), "expiry_qty")
        self.assertEqual(_header_driven_role("Closing Amt"), "closing_value")
        self.assertEqual(_header_driven_role("Closing Stock"), "closing_qty")
        self.assertEqual(_header_driven_role("Open Qty"), "order_qty")
        self.assertEqual(_header_driven_role("ISSUE QTY"), "sales_qty")
        self.assertEqual(_header_driven_role("ISSUE VALUE"), "sales_value")
        self.assertEqual(_header_driven_role("CLOSING QTY"), "closing_qty")
        self.assertTrue(
            _header_driven_is_jun_jul(
                [{"name": "Jun", "role": "other"}, {"name": "Jul", "role": "other"}]
            )
        )

    def test_closing_uses_return_expiry_and_shortage(self):
        item = {
            "opening_qty": 44.0,
            "receipts_qty": 0.0,
            "sales_qty": 5.0,
            "closing_qty": 35.0,
            "extra": {"sale_return_qty": 0.0, "expiry_qty": 4.0, "shortage_qty": 0.0},
        }
        self.assertEqual(_header_driven_expected_closing(item, ROLES), 35.0)

    def test_items_keep_closing_amount_and_drop_the_banner(self):
        parsed = {
            "line_items": [
                {
                    "product_name": "HIMALAYA DIVISION (ZANDRA)",
                    "opening_qty": 0,
                    "closing_qty": 0,
                },
                {
                    "product_name": "BONNISAN DROPS 30ML",
                    "opening_qty": 44,
                    "receipts_qty": 0,
                    "sales_qty": 5,
                    "closing_qty": 35,
                    "closing_value": 2432.85,
                    "extra": {
                        "total_stock": 44,
                        "sale_return_qty": 0,
                        "expiry_qty": 4,
                        "shortage_qty": 0,
                    },
                },
            ]
        }
        items = _header_driven_items(parsed, ROLES)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sales_qty"], 5.0)
        self.assertEqual(items[0]["closing_qty"], 35.0)
        self.assertEqual(items[0]["closing_value"], 2432.85)
        self.assertIsNone(items[0]["sales_value"])

    def test_rows_require_a_majority_that_balance(self):
        good = []
        for index in range(8):
            good.append(
                {
                    "opening_qty": 20.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 2.0,
                    "closing_qty": 18.0,
                    "extra": {
                        "sale_return_qty": 0.0,
                        "expiry_qty": 0.0,
                        "shortage_qty": 0.0,
                    },
                }
            )
        self.assertTrue(_header_driven_rows_ok(good, ROLES))
        shifted = [dict(item, sales_qty=0.0, closing_qty=20.0) for item in good]
        shifted[0] = dict(shifted[0], closing_qty=9.0)
        shifted[1] = dict(shifted[1], closing_qty=8.0)
        shifted[2] = dict(shifted[2], closing_qty=7.0)
        shifted[3] = dict(shifted[3], closing_qty=6.0)
        self.assertFalse(_header_driven_rows_ok(shifted, ROLES))

    def test_validation_does_not_turn_closing_amount_into_zero(self):
        result = empty_result("sheet.png", "png")
        item = empty_line_item()
        item["product_name"] = "ARJUNA TAB 60'S"
        item["opening_qty"] = 21.0
        item["receipts_qty"] = 0.0
        item["sales_qty"] = 0.0
        item["sales_value"] = None
        item["closing_qty"] = 21.0
        item["closing_value"] = 5677.29
        item["extra"] = {
            "sale_return_qty": 0.0,
            "expiry_qty": 0.0,
            "shortage_qty": 0.0,
            "total_stock": 21.0,
        }
        result["line_items"] = [item]
        result["totals"]["extra"]["extraction_method"] = "header_driven_stock_photo"
        result["totals"]["extra"]["column_roles"] = sorted(ROLES)
        result["totals"]["extra"]["no_sales_value"] = True
        result = _apply_header_driven_stock_validation(result)
        result = _ensure_stock_qty_value_fields(result)
        kept = result["line_items"][0]
        self.assertEqual(kept["closing_value"], 5677.29)
        self.assertIsNone(kept["sales_value"])
        self.assertTrue(kept["extra"]["stock_identity_ok"])
        self.assertEqual(
            result["totals"]["extra"]["stock_identity_kind"],
            "header_driven_stock_columns",
        )

    def test_borderless_issue_and_closing_swap_back_when_rates_agree(self):
        swapped = {
            "product_name": "MENTAT TAB 1*50",
            "opening_qty": 65.0,
            "receipts_qty": 0.0,
            "sales_qty": 8.0,
            "sales_value": 9044.1,
            "closing_qty": 57.0,
            "closing_value": 1255.68,
            "extra": {},
        }
        steady = {
            "product_name": "EVECAR CAP 1*30",
            "opening_qty": 27.0,
            "receipts_qty": 0.0,
            "sales_qty": 21.0,
            "sales_value": 3525.06,
            "closing_qty": 6.0,
            "closing_value": 1000.02,
            "extra": {},
        }
        roles = {"sales_value", "closing_value", "sales_qty", "closing_qty"}
        _header_driven_separate_issue_closing([swapped, steady], roles)
        self.assertEqual(swapped["sales_qty"], 57.0)
        self.assertEqual(swapped["closing_qty"], 8.0)
        self.assertEqual(steady["sales_qty"], 21.0)
        self.assertEqual(steady["closing_qty"], 6.0)
        _header_driven_separate_issue_closing([swapped], {"closing_qty", "sales_qty"})
        self.assertEqual(swapped["sales_qty"], 57.0)

    def test_same_product_with_another_pack_is_kept(self):
        first = {
            "product_name": "BONNISAN SYP",
            "packing": "1*100M",
            "opening_qty": 50.0,
            "receipts_qty": 0.0,
            "sales_qty": 4.0,
            "closing_qty": 46.0,
            "extra": {},
        }
        second = {
            "product_name": "BONNISAN SYP",
            "packing": "1*200M",
            "opening_qty": 46.0,
            "receipts_qty": 0.0,
            "sales_qty": 3.0,
            "closing_qty": 43.0,
            "extra": {},
        }
        kept = _header_driven_merge_items([[first, second]], ROLES)
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[1]["packing"], "1*200M")

    def test_issue_closing_grid_keeps_total_qty_and_a_dropped_issue(self):
        items = []
        for index in range(8):
            items.append(
                {
                    "product_name": f"ITEM {index}",
                    "packing": "1*10ML",
                    "opening_qty": 10.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 2.0,
                    "closing_qty": 8.0,
                    "extra": {},
                }
            )
        items.append(
            {
                "product_name": "BONNISPAZ DROPS",
                "packing": "1*10ML",
                "opening_qty": 2.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 0.0,
                "extra": {},
            }
        )
        roles = {"opening_qty", "receipts_qty", "sales_qty", "closing_qty", "product"}
        self.assertTrue(_header_driven_is_marg_issue_grid(items, roles))
        _repair_marg_mexp_missing_issue(items)
        paz = items[-1]
        self.assertEqual(paz["sales_qty"], 2.0)
        self.assertEqual(paz["closing_qty"], 0.0)
        parsed = _header_driven_items(
            {
                "line_items": [
                    {
                        "product_name": "BONNISAN DROP",
                        "packing": "1*30ML",
                        "opening_qty": 15,
                        "receipts_qty": 40,
                        "sales_qty": 26,
                        "closing_qty": 29,
                        "total_qty": 55,
                        "extra": {"total_value": 1200},
                    }
                ]
            },
            roles | {"total_qty"},
        )
        self.assertEqual(parsed[0]["extra"]["total_stock"], 55.0)
        self.assertEqual(parsed[0]["extra"]["total_value"], 1200.0)

    def test_impossible_photo_year_uses_the_filename_month(self):
        result = empty_result(
            "0000733344_2026_08_ZA_06_333_04092026082024.png", "png"
        )
        result["period_from"] = "2006-08-01"
        result["period_to"] = "2078-08-31"
        _header_driven_fix_period(result, result["source_file"])
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")

    def test_zandra_sheet_is_not_a_jun_jul_reread(self):
        result = empty_result("zandra.png", "png")
        result["report_title"] = "Stock Statement"
        result["company_name"] = "HIMALAYA DRUGS (ZANDRA)"
        result["stockist_name"] = "VINAYAK AGENCIES"
        result["line_items"] = []
        for index in range(8):
            item = empty_line_item()
            item["product_name"] = f"ITEM {index}"
            item["opening_qty"] = 40
            item["sales_qty"] = 0
            item["closing_qty"] = 27
            result["line_items"].append(item)
        self.assertFalse(_pharma_hub_jun_jul_needs_reread(result))
        self.assertFalse(_pharma_hub_party(result))

        zeal = empty_result("zeal.jpg", "jpg")
        zeal["report_title"] = "Stock Statement"
        zeal["company_name"] = "HIMALAYA DRUGS (ZEAL)"
        zeal["line_items"] = result["line_items"]
        self.assertTrue(_pharma_hub_jun_jul_needs_reread(zeal))


if __name__ == "__main__":
    unittest.main()
