"""Sales & Stock Receipt/Pur Value — keep printed purchase amount."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _apply_swil_receipt_value_fields,
    _dedupe_swil_receipt_overlap_items,
    _is_swil_opening_receipt_value_statement,
    _looks_like_swil_receipt_pur_result,
    _parse_swil_receipt_printed_totals,
    _swil_receipt_value_count,
    _swil_receipt_value_missing,
    empty_result,
)


SWIL_HEADER = (
    "Page No.1 Sales & Stock Statement (From 01/08/2026 Upto 31/08/2026)\n"
    "HIMALAYA WELLNESS COMPANY\n"
    "PRODUCT NAME PACKING Opening Qty Opening Value Receipt Qty "
    "Receipt/Pur Value Total Issue/Sales Closing\n"
)

OTHER = (
    "GANESH AGENCY\n"
    "Stock Statment : HIMALAYA ZEAL\n"
    "Code  Item Description  Packing  Opening Purchase  Sales  Closing\n"
)


def _swil_result_without_receipt_value():
    result = empty_result("stmt.png", "png")
    result["report_title"] = "Sales & Stock Statement"
    result["line_items"] = [
        {
            "product_name": "BONNISAN DROPS",
            "packing": "30ML",
            "opening_qty": 0.0,
            "receipts_qty": 50.0,
            "sales_qty": 20.0,
            "sales_value": 1493.20,
            "closing_qty": 30.0,
            "closing_value": 2084.99,
            "extra": {},
        },
        {
            "product_name": "CLARINA ANTI ACNE CR",
            "packing": "30GMS",
            "opening_qty": 0.0,
            "receipts_qty": 50.0,
            "sales_qty": 3.0,
            "sales_value": 438.84,
            "closing_qty": 47.0,
            "closing_value": 6399.71,
            "extra": {},
        },
        {
            "product_name": "CONFIDO TABLETS",
            "packing": "60'S",
            "opening_qty": 0.0,
            "receipts_qty": 200.0,
            "sales_qty": 0.0,
            "sales_value": 0.0,
            "closing_qty": 200.0,
            "closing_value": 32743.20,
            "extra": {},
        },
    ]
    return result


class TestSwilReceiptPurDetection(unittest.TestCase):
    def test_detects_receipt_pur_header_not_other_formats(self):
        self.assertTrue(_is_swil_opening_receipt_value_statement(SWIL_HEADER))
        self.assertFalse(_is_swil_opening_receipt_value_statement(OTHER))
        self.assertFalse(_is_swil_opening_receipt_value_statement(""))

    def test_missing_receipt_value_on_sales_stock_result(self):
        result = _swil_result_without_receipt_value()
        self.assertTrue(_looks_like_swil_receipt_pur_result(result))
        self.assertTrue(_swil_receipt_value_missing(result))
        self.assertEqual(_swil_receipt_value_count(result), 0)

    def test_does_not_steal_qty_only_or_other_titles(self):
        qty_only = empty_result("busy.png", "png")
        qty_only["report_title"] = "Sales & Stock Statement"
        qty_only["line_items"] = [
            {
                "product_name": "LIV 52 TAB",
                "opening_qty": 10.0,
                "receipts_qty": 2.0,
                "sales_qty": 1.0,
                "sales_value": 0.0,
                "closing_value": 0.0,
                "extra": {},
            }
        ] * 3
        self.assertFalse(_looks_like_swil_receipt_pur_result(qty_only))

        psr = empty_result("psr.png", "png")
        psr["report_title"] = "Product Stock Report"
        psr["line_items"] = _swil_result_without_receipt_value()["line_items"]
        self.assertFalse(_looks_like_swil_receipt_pur_result(psr))

    def test_existing_swil_parser_method_is_left_alone(self):
        result = _swil_result_without_receipt_value()
        result["totals"]["extra"]["extraction_method"] = "swil_opening_receipt_value"
        self.assertFalse(_looks_like_swil_receipt_pur_result(result))


class TestSwilReceiptPurApply(unittest.TestCase):
    def test_apply_puts_receipts_value_on_line_and_extra(self):
        result = empty_result("stmt.png", "png")
        parsed = {
            "report_title": "Sales & Stock Statement",
            "line_items": [
                {
                    "product_name": "BONNISAN DROPS",
                    "packing": "30ML",
                    "opening_qty": 0,
                    "opening_value": 0,
                    "receipts_qty": 50,
                    "receipts_value": 3309.30,
                    "sales_qty": 20,
                    "sales_value": 1493.20,
                    "closing_qty": 30,
                    "closing_value": 2084.99,
                },
                {
                    "product_name": "CLARINA ANTI ACNE CR",
                    "packing": "30GMS",
                    "opening_qty": 0,
                    "receipts_qty": 50,
                    "receipts_value": 6483.86,
                    "sales_qty": 3,
                    "sales_value": 438.84,
                    "closing_qty": 47,
                    "closing_value": 6399.71,
                    "extra": {"opening_value": 0},
                },
            ],
            "totals": {"receipts_value": 9793.16, "sales_value": 1932.04},
        }
        applied = _apply_swil_receipt_value_fields(result, parsed)
        extra = (applied.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "swil_receipt_pur_value_vision")
        first = applied["line_items"][0]
        self.assertEqual(first["product_name"], "BONNISAN DROPS")
        self.assertEqual(first["receipts_qty"], 50.0)
        self.assertEqual(first["receipts_value"], 3309.30)
        self.assertEqual(first["extra"]["receipts_value"], 3309.30)
        self.assertEqual(first["extra"]["purchase_value"], 3309.30)
        self.assertEqual(first["extra"]["opening_value"], 0.0)
        keys = list(first.keys())
        self.assertLess(keys.index("receipts_qty"), keys.index("receipts_value"))
        second = applied["line_items"][1]
        self.assertEqual(second["receipts_value"], 6483.86)
        self.assertEqual(applied["totals"].get("receipts_value"), 9793.16)
        self.assertFalse(_swil_receipt_value_missing(applied))
        self.assertEqual(_swil_receipt_value_count(applied), 2)


class TestSwilReceiptPurTotals(unittest.TestCase):
    def test_parses_printed_total_value_row(self):
        text = (
            "TENTEX ROYEL CAPSULE 10'S 100 18979.97 100 0.00 100 19929.00\n"
            "| TOTAL (value IN Rs.) 0.00 545989.74 74925.80 504905. 88 I |\n"
        )
        parsed = _parse_swil_receipt_printed_totals(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["opening_value"], 0.0)
        self.assertEqual(parsed["receipts_value"], 545989.74)
        self.assertEqual(parsed["sales_value"], 74925.80)
        self.assertEqual(parsed["closing_value"], 504905.88)

    def test_overlap_rows_are_dropped_once(self):
        items = [
            {
                "product_name": "HIORA GA GEL 15ML",
                "packing": "15ML",
                "receipts_qty": 50.0,
                "sales_qty": 0.0,
                "closing_qty": 50.0,
                "closing_value": 3652.42,
                "receipts_value": 3478.63,
                "extra": {"receipts_value": 3478.63},
            },
            {
                "product_name": "ORO-T ORAL RINSE",
                "packing": "100ML",
                "receipts_qty": 50.0,
                "sales_qty": 0.0,
                "closing_qty": 50.0,
                "closing_value": 5957.18,
                "receipts_value": 5673.60,
                "extra": {"receipts_value": 5673.60},
            },
            {
                "product_name": "HIORA GA GEL",
                "packing": None,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 50.0,
                "closing_value": 3652.42,
                "receipts_value": 0.0,
                "extra": {},
            },
        ]
        deduped = _dedupe_swil_receipt_overlap_items(items)
        self.assertEqual(len(deduped), 2)
        names = [i["product_name"] for i in deduped]
        self.assertEqual(names, ["HIORA GA GEL 15ML", "ORO-T ORAL RINSE"])
        self.assertAlmostEqual(
            sum(i["closing_value"] for i in deduped), 3652.42 + 5957.18
        )

    def test_pack_ocr_noise_does_not_keep_same_closing_row(self):
        items = [
            {
                "product_name": "HIORA K TOOTH PASTE",
                "packing": "50GMS",
                "receipts_qty": 50.0,
                "sales_qty": 4.0,
                "closing_qty": 46.0,
                "closing_value": 3284.88,
                "extra": {},
            },
            {
                "product_name": "HIORA K TOOTH PASTE",
                "packing": "50GM5",
                "receipts_qty": 0.0,
                "sales_qty": 50.0,
                "closing_qty": 46.0,
                "closing_value": 3284.88,
                "extra": {},
            },
        ]
        self.assertEqual(len(_dedupe_swil_receipt_overlap_items(items)), 1)


if __name__ == "__main__":
    unittest.main()

