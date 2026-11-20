"""Himalaya Drugs Zeal Stock Report keeps Opening/Purchased/Sold/Closing pairs."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _finalize_opening_purchased_sold,
    _looks_like_opening_purchased_sold_report,
    empty_line_item,
    empty_result,
)


def _zeal_result():
    result = empty_result("stmt.jpeg", "jpeg")
    result["report_title"] = "Stock Report"
    result["stockist_name"] = "HIMALAYA DRUGS [ZEAL]"
    result["line_items"] = []
    for name, opening in (
        ("ZEAL AACTARIL SOAP 75 GM", 30),
        ("ZEAL ABANA TAB 60 TAB", 67),
        ("ZEAL ALTHEA LOTION 100 ML", 29),
        ("ZEAL BLEMINOR ANTIBLE CR 30 ML", 46),
        ("ZEAL CONFIDO TAB 60 TAB", 30),
        ("ZEAL CLARINA CREAM 30 GM", 50),
        ("ZEAL DIABECON DS TAB 60 TAB", 116),
        ("ZEAL DIAREX 30 TAB", 107),
    ):
        item = empty_line_item()
        item["product_name"] = name
        item["opening_qty"] = opening
        item["opening_value"] = 1000
        item["closing_qty"] = opening
        item["closing_value"] = 1000
        result["line_items"].append(item)
    return result


class TestOpeningPurchasedSoldDetect(unittest.TestCase):
    def test_detector_accepts_this_stock_report_only(self):
        self.assertTrue(_looks_like_opening_purchased_sold_report(_zeal_result()))
        other = _zeal_result()
        other["report_title"] = "Product Stock Report"
        other["stockist_name"] = "SOME MEDICAL"
        for item in other["line_items"]:
            item["product_name"] = item["product_name"].replace("ZEAL ", "")
        self.assertFalse(_looks_like_opening_purchased_sold_report(other))
        rtl = _zeal_result()
        rtl["totals"]["extra"]["layout"] = "summary_rtl_op_amt"
        self.assertFalse(_looks_like_opening_purchased_sold_report(rtl))

    def test_finalize_keeps_purchased_value_and_drops_fake_packing(self):
        result = empty_result("stmt.jpeg", "jpeg")
        result["stockist_name"] = "HIMALAYA DRUGS [ZEAL]"
        item = empty_line_item()
        item["product_name"] = "ZEAL CONFIDO TAB 60 TAB"
        item["packing"] = "TAB"
        item["opening_qty"] = 30
        item["opening_value"] = 4923.90
        item["receipts_qty"] = 0
        item["sales_qty"] = 6
        item["sales_value"] = 1110.84
        item["closing_qty"] = 24
        item["closing_value"] = 3939.12
        item["extra"] = {"receipts_value": 0}
        result["line_items"] = [item]
        done = _finalize_opening_purchased_sold(result)
        row = done["line_items"][0]
        self.assertIsNone(row["packing"])
        self.assertEqual(row["opening_qty"], 30)
        self.assertEqual(row["opening_value"], 4923.90)
        self.assertEqual(row["sales_qty"], 6)
        self.assertEqual(row["sales_value"], 1110.84)
        self.assertEqual(row["closing_qty"], 24)
        self.assertEqual(row["receipts_value"], 0.0)
        self.assertEqual(done["company_name"], "HIMALAYA DRUGS [ZEAL]")
        self.assertEqual(
            done["totals"]["extra"]["extraction_method"],
            "opening_purchased_sold_vision",
        )


if __name__ == "__main__":
    unittest.main()
