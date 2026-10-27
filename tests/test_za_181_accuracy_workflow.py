"""Ground-truth rules for the 181-image accuracy workflow."""

from __future__ import annotations

import unittest

from scripts.za_181_accuracy_workflow import (
    apply_vision_proposal,
    match_rows,
    rows_from_verified_document,
    score_document,
)


def _qty(value, status="verified"):
    return {"raw": value, "normalized": value, "status": status}


def _name(value, status="verified"):
    return {"raw": value, "normalized": value, "status": status}


def _row(row_id, product, **quantities):
    base = {
        "row_id": row_id,
        "row_index": int(row_id[1:]),
        "product_name": _name(product),
        "packing": {"raw": None, "normalized": None, "status": "absent"},
        "opening_qty": {"raw": None, "normalized": None, "status": "absent"},
        "receipt_qty": {"raw": None, "normalized": None, "status": "absent"},
        "sales_qty": {"raw": None, "normalized": None, "status": "absent"},
        "sales_scheme_qty": {"raw": None, "normalized": None, "status": "absent"},
        "stock_out_qty": {"raw": None, "normalized": None, "status": "absent"},
        "closing_qty": {"raw": None, "normalized": None, "status": "absent"},
    }
    for key, value in quantities.items():
        base[key] = value
    return base


class AccuracyRuleTests(unittest.TestCase):
    def test_duplicate_rows_stay_separate(self):
        gt = [
            _row("r01", "Cystone Tab", closing_qty=_qty(4)),
            _row("r02", "Cystone Tab", closing_qty=_qty(9)),
        ]
        pred = [
            {"product": "Cystone Tab", "fields": {"closing_qty": 4}},
            {"product": "Cystone Tab", "fields": {"closing_qty": 9}},
        ]
        pairs, extras = match_rows(gt, pred)
        self.assertEqual([pair["name_result"] for pair in pairs], ["correct", "correct"])
        self.assertEqual(extras, [])
        scored = score_document(
            {"document_id": "a.jpg", "verification_status": "verified", "rows": gt},
            pred,
            "Cystone Tab 4\nCystone Tab 9",
        )
        self.assertEqual(scored["product_correct"], 2)
        self.assertEqual(scored["quantity_correct"], 2)
        self.assertEqual(scored["product_missing"], 0)

    def test_uncertain_quantity_is_not_scored(self):
        gt = [_row("r01", "Cystone Tab", closing_qty=_qty(4, status="uncertain"))]
        pred = [{"product": "Cystone Tab", "fields": {"closing_qty": 99}}]
        scored = score_document(
            {"document_id": "a.jpg", "verification_status": "verified", "rows": gt},
            pred,
            "Cystone Tab 99",
        )
        self.assertEqual(scored["quantity_scored"], 0)
        self.assertEqual(scored["quantity_incorrect"], 0)
        self.assertEqual(scored["product_correct"], 1)

    def test_missing_prediction_is_not_zero(self):
        gt = [_row("r01", "Cystone Tab", closing_qty=_qty(0))]
        pred = [{"product": "Cystone Tab", "fields": {}}]
        scored = score_document(
            {"document_id": "a.jpg", "verification_status": "verified", "rows": gt},
            pred,
            "Cystone Tab",
        )
        self.assertEqual(scored["quantity_missing"], 1)
        self.assertEqual(scored["quantity_correct"], 0)

    def test_out_qty_imports_as_sales_without_inventing_opening(self):
        payload = {
            "fields": [
                {
                    "field_id": "r01.product_name",
                    "expected_raw": "Cystone Tab",
                    "expected_normalized": "Cystone Tab",
                    "verified": True,
                    "source_verified": True,
                },
                {
                    "field_id": "r01.out_qty",
                    "expected_raw": "5",
                    "expected_normalized": 5,
                    "verified": True,
                    "source_verified": True,
                },
            ]
        }
        rows, _headers = rows_from_verified_document(payload)
        self.assertEqual(rows[0]["sales_qty"]["normalized"], 5)
        self.assertEqual(rows[0]["opening_qty"]["status"], "absent")

    def test_headerless_numbers_stay_unknown_and_blank_is_not_zero(self):
        rows = apply_vision_proposal(
            [
                {
                    "product_name": "Cystone Tab",
                    "packing": "60'S",
                    "columns": [
                        {"position": 1, "printed_header": None, "semantic": "opening_qty", "raw": "4", "status": "ai_proposed"},
                        {"position": 2, "printed_header": "Opening Qty", "semantic": "opening_qty", "raw": None, "status": "blank"},
                        {"position": 3, "printed_header": "Closing Value", "semantic": "closing_qty", "raw": "9", "status": "ai_proposed"},
                    ],
                }
            ]
        )
        self.assertEqual(rows[0]["opening_qty"]["status"], "absent")
        self.assertEqual(rows[0]["columns"][0]["semantic"], "unknown")
        self.assertEqual(rows[0]["columns"][1]["status"], "blank")
        self.assertIsNone(rows[0]["columns"][1]["normalized"])
        self.assertEqual(rows[0]["columns"][2]["semantic"], "not_quantity")
        self.assertEqual(rows[0]["closing_qty"]["status"], "absent")


if __name__ == "__main__":
    unittest.main()
