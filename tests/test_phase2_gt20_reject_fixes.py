"""Regression: GT-20 Phase 2 discard bugs (dynamic qty + Rcpt header).

Uses the real Gemini response shape observed on
``0000700084_2026_08_ZL_06_8236_03092026155136.pdf`` where printed receipt
quantities were returned under dynamic ``Rcpt Qty`` / ``OpStk`` / ``Clstk``
while fixed canonical fields stayed null. Merge must promote those values
onto failing rows without touching healthy rows.
"""

from __future__ import annotations

import os
import unittest

from services.hybrid_schema import SalesExtractionOutput
from services.html_table.column_matcher import match_headers
from services.recovery_runner import (
    _candidate_accepted,
    _document_recovery_mapping,
    _promote_dynamic_qty_into_fixed,
    _stock_prompt,
    merge_stock_recovery,
)
from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    empty_line_item,
)
from services.base_validator import validate_sales_result


def _item(name, opening, receipts, sales, closing):
    item = empty_line_item()
    item["product_name"] = name
    item["opening_qty"] = opening
    item["receipts_qty"] = receipts
    item["sales_qty"] = sales
    item["closing_qty"] = closing
    item["extra"] = {}
    return item


def _statement(items):
    statement = {
        "source_file": "0000700084_2026_08_ZL_06_8236_03092026155136.pdf",
        "stockist_name": "TEST",
        "line_items": items,
        "totals": {
            "extra": {
                "extraction_method": "vl_html_table_parse",
                "mapping_source": "fuzzy",
                "columns_matched": {
                    "1": "product_name",
                    "3": "opening_qty",
                    "5": "sales_qty",
                    "6": "closing_qty",
                },
                "columns_unmatched": [
                    "Sr. No.",
                    "Packing",
                    "Rcpt Qty",
                    "Early Exp.",
                    "Rem",
                    "Avg. Sal 3 Months",
                    "Order",
                ],
                "ocr_table_html": (
                    "<table><tr>"
                    "<th>Sr. No.</th><th>Product</th><th>Packing</th>"
                    "<th>OpStk</th><th>Rcpt Qty</th><th>Sales</th><th>Clstk</th>"
                    "</tr></table>"
                ),
            }
        },
    }
    statement = _apply_stock_identity_validation(statement)
    statement["phase1"] = {"validation": validate_sales_result(statement)}
    return statement


class RcptHeaderMatcherTests(unittest.TestCase):
    def test_rcpt_qty_maps_to_receive_qty(self):
        matched = match_headers(
            ["Sr. No.", "Product", "Packing", "OpStk", "Rcpt Qty", "Sales", "Clstk"]
        )
        self.assertEqual(matched.get(4), "receive_qty")
        self.assertEqual(matched.get(3), "opening_qty")
        self.assertEqual(matched.get(6), "closing_qty")


class DynamicQtyPromotionTests(unittest.TestCase):
    def test_promote_null_fixed_from_dynamic_headers(self):
        parsed = SalesExtractionOutput.model_validate(
            {
                "line_items": [
                    {
                        "fixed": {
                            "product_name": "CONFIDO TAB.",
                            "sales_qty": 40.0,
                        },
                        "dynamic": {
                            "OpStk": "8.0",
                            "Rcpt Qty": "100.0",
                            "Clstk": "68.0",
                        },
                    }
                ]
            }
        )
        line = parsed.line_items[0]
        _promote_dynamic_qty_into_fixed(line, {})
        self.assertEqual(line.fixed.opening_qty, 8.0)
        self.assertEqual(line.fixed.receipts_qty, 100.0)
        self.assertEqual(line.fixed.closing_qty, 68.0)
        self.assertEqual(line.fixed.sales_qty, 40.0)

    def test_promote_does_not_overwrite_non_null_fixed(self):
        parsed = SalesExtractionOutput.model_validate(
            {
                "line_items": [
                    {
                        "fixed": {
                            "product_name": "CONFIDO TAB.",
                            "opening_qty": 9.0,
                            "sales_qty": 40.0,
                        },
                        "dynamic": {"OpStk": "8.0", "Rcpt Qty": "100.0"},
                    }
                ]
            }
        )
        line = parsed.line_items[0]
        _promote_dynamic_qty_into_fixed(line, {"OpStk": "opening_qty", "Rcpt Qty": "receipts_qty"})
        self.assertEqual(line.fixed.opening_qty, 9.0)
        self.assertEqual(line.fixed.receipts_qty, 100.0)


class Gt20ConfidoMergeTests(unittest.TestCase):
    def setUp(self):
        os.environ["PHASE2_RECOVERY_ENABLED"] = "false"

    def test_stock_prompt_declares_rcpt_mapping(self):
        statement = _statement(
            [
                _item("GOOD ROW", 10, 0, 0, 10),
                _item("CONFIDO TAB.", 8, 0.0, 40, 68),
                _item("LIV 52 SYP. 200", 110, 0.0, 100, 81),
                _item("LIV 52 TAB.", 117, 0.0, 70, 147),
            ]
        )
        mapping = _document_recovery_mapping(statement)
        self.assertEqual(mapping.get("Rcpt Qty"), "receipts_qty")
        prompt = _stock_prompt(statement)
        self.assertIn("Column mapping:", prompt)
        self.assertIn("Rcpt Qty = receipts_qty", prompt)
        self.assertIn("ONLY for the product rows listed below", prompt)
        self.assertNotIn("Leave canonical quantity fields null", prompt)

    def test_merge_promotes_dynamic_receipts_and_accepts(self):
        good = _item("GOOD ROW", 10, 0, 0, 10)
        statement = _statement(
            [
                good,
                _item("CONFIDO TAB.", 8, 0.0, 40, 68),
                _item("LIV 52 SYP. 200", 110, 0.0, 100, 81),
                _item("LIV 52 TAB.", 117, 0.0, 70, 147),
            ]
        )
        self.assertFalse((statement.get("phase1") or {}).get("validation", {}).get("accepted"))

        # Exact Gemini JSON shape from the GT-20 0084 live recovery call.
        recovered = SalesExtractionOutput.model_validate_json(
            """
            {"line_items": [
              {"fixed": {"product_name": "CONFIDO TAB.", "sales_qty": 40.0, "sales_value": null},
               "dynamic": {"Sr. No.": "8", "Packing": "60TAB", "OpStk": "8.0", "Rcpt Qty": "100.0", "Clstk": "68.0"}},
              {"fixed": {"product_name": "LIV 52 SYP. 200", "sales_qty": 100.0, "sales_value": null},
               "dynamic": {"OpStk": "110.0", "Rcpt Qty": "71.0", "Clstk": "81.0"}},
              {"fixed": {"product_name": "LIV 52 TAB.", "sales_qty": 70.0, "sales_value": null},
               "dynamic": {"OpStk": "117.0", "Rcpt Qty": "100.0", "Clstk": "147.0"}}
            ]}
            """
        )
        candidate = merge_stock_recovery(statement, recovered)
        by_name = {
            item["product_name"]: item
            for item in candidate["line_items"]
            if isinstance(item, dict)
        }
        self.assertEqual(by_name["CONFIDO TAB."]["receipts_qty"], 100.0)
        self.assertEqual(by_name["LIV 52 SYP. 200"]["receipts_qty"], 71.0)
        self.assertEqual(by_name["LIV 52 TAB."]["receipts_qty"], 100.0)
        # Healthy row untouched.
        self.assertEqual(by_name["GOOD ROW"]["opening_qty"], 10)
        self.assertEqual(by_name["GOOD ROW"]["closing_qty"], 10)
        self.assertNotEqual(
            ((by_name["GOOD ROW"].get("extra") or {}).get("field_sources") or {}).get(
                "opening_qty"
            ),
            "gemini_phase2",
        )
        self.assertEqual(
            (by_name["CONFIDO TAB."].get("extra") or {}).get("field_sources", {}).get(
                "receipts_qty"
            ),
            "gemini_phase2",
        )
        self.assertTrue(_candidate_accepted(candidate))


if __name__ == "__main__":
    unittest.main()
