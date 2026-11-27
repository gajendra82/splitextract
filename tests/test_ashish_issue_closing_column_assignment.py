"""Ashish Medical Store / ZEAL — Issue Qty vs Closing Balance column assignment.

Stock identity alone accepts a pure Issue↔Closing swap (22−21=1 still balances).
Repairs use printed money rates and OCR positional columns — never invent digits
or move a quantity into a monetary field merely because money is available.
"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from services.extraction_quality import evaluate_extraction_quality
from services.sales_statement_extractor import (
    _coerce_month_year_period_fields,
    _merge_opbal_ocr_issue_closing_into_vision,
    _parse_month_year_period_text,
    _parse_opbal_receipt_issue_statement,
    empty_line_item,
    empty_result,
)
from services.stock_native_resolver import (
    apply_universal_sales_qty_value_guard,
    issue_closing_rate_prefers_swap,
    _repair_issue_closing_swap_by_rate,
    _repair_issue_qty_parked_in_sales_value,
)
from services.stock_row_classifier import (
    RowStatus,
    classify_result,
    identity_ambiguous_sales_closing_swap,
    read_row_fields,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ashish_zeal_oct2026_opbal.txt"

# Expected Op.Bal / Receipt / Total / Issue / Closing from the source image.
EXPECTED_ROWS = {
    "CLARINA ANTI ACNE CREAM": (22.0, 0.0, 22.0, 1.0, 21.0),
    "LIV.52 SYRUP": {
        "100ml": (410.0, 1750.0, 2160.0, 938.0, 1222.0),
        "200ml": (2640.0, 0.0, 2640.0, 2551.0, 89.0),
    },
    "ABANA TABS": (146.0, 0.0, 146.0, 131.0, 15.0),
    "PILEX FORTE OINTMENT": (1229.0, 0.0, 1229.0, 400.0, 829.0),
}
EXPECTED_AGG = {
    "opening": 7731.0,
    "receipts": 1750.0,
    "issue": 4816.0,
    "closing": 4665.0,
}


def _row_tuple(item: dict) -> tuple:
    extra = item.get("extra") or {}
    total = extra.get("total_stock_qty")
    if total is None:
        total = extra.get("total_stock")
    return (
        float(item.get("opening_qty") or 0),
        float(item.get("receipts_qty") or 0),
        float(total) if total is not None else float(item.get("opening_qty") or 0)
        + float(item.get("receipts_qty") or 0),
        float(item.get("sales_qty") or 0),
        float(item.get("closing_qty") or 0),
    )


class TestIssueQtyParkedInSalesValue(unittest.TestCase):
    def test_liv52_issue_2551_not_sales_value(self):
        item = {
            "product_name": "LIV.52 SYRUP 200ml",
            "opening_qty": 2640.0,
            "receipts_qty": 0.0,
            "sales_qty": None,
            "sales_value": 2551.0,
            "closing_qty": 89.0,
            "extra": {"layout": "opbal_issue_closing", "qty_only": True},
        }
        self.assertTrue(_repair_issue_qty_parked_in_sales_value(item))
        self.assertEqual(item["sales_qty"], 2551.0)
        self.assertIsNone(item["sales_value"])
        self.assertIn(
            "ISSUE_QTY_PARKED_IN_SALES_VALUE",
            (item.get("extra") or {}).get("flags") or [],
        )

    def test_money_from_qty_must_not_steal_balancing_issue(self):
        """Regression for request 2738a163b89f: guard moved 2551 → sales_value."""
        from services.stock_native_resolver import apply_universal_sales_qty_value_guard

        result = empty_result("ashish.jpg", "jpg")
        result["line_items"] = [
            {
                "product_name": "LIV 52 SYRUP 200ml",
                "opening_qty": 2640.0,
                "receipts_qty": 0.0,
                "sales_qty": 2551.0,  # correct Issue from Gemini
                "sales_value": None,
                "closing_qty": 89.0,
                "extra": {},
            }
        ]
        result["totals"]["extra"]["extraction_method"] = "gemini_extraction_fallback"
        apply_universal_sales_qty_value_guard(result)
        item = result["line_items"][0]
        self.assertEqual(item["sales_qty"], 2551.0)
        self.assertTrue(
            item.get("sales_value") in (None, 0, 0.0),
            msg=f"sales_value must not hold Issue qty: {item.get('sales_value')}",
        )

    def test_missing_issue_derived_from_closing_identity(self):
        """Blank Issue stays unread. Identity is only a candidate."""
        from services.stock_native_resolver import repair_opbal_qty_only_fields

        result = empty_result("ashish.jpg", "jpg")
        result["line_items"] = [
            {
                "product_name": "SAMPLE ROW",
                "opening_qty": 1110.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 688.0,
                "extra": {"total_stock": 1110.0},
            }
        ]
        result["totals"]["extra"]["qty_only"] = True
        repair_opbal_qty_only_fields(result)
        item = result["line_items"][0]
        self.assertEqual(item["sales_qty"], 0.0)
        self.assertEqual(item["closing_qty"], 688.0)
        cands = item["extra"]["candidates"]
        self.assertTrue(
            any(
                c.get("source") == "opbal_missing_issue"
                and c.get("fields", {}).get("sales_qty") == 422.0
                for c in cands
            )
        )

    def test_missing_monetary_value_stays_null(self):
        item = {
            "product_name": "OK ROW",
            "opening_qty": 100.0,
            "receipts_qty": 0.0,
            "sales_qty": 40.0,
            "sales_value": None,
            "closing_qty": 60.0,
            "extra": {"qty_only": True},
        }
        self.assertFalse(_repair_issue_qty_parked_in_sales_value(item))
        self.assertEqual(item["sales_qty"], 40.0)
        self.assertIsNone(item["sales_value"])

    def test_printed_money_not_moved_into_qty_when_issue_present(self):
        item = {
            "product_name": "HAS MONEY",
            "opening_qty": 10.0,
            "receipts_qty": 0.0,
            "sales_qty": 4.0,
            "sales_value": 400.50,
            "closing_qty": 6.0,
            "extra": {},
        }
        self.assertFalse(_repair_issue_qty_parked_in_sales_value(item))
        self.assertEqual(item["sales_qty"], 4.0)
        self.assertEqual(item["sales_value"], 400.50)


class TestIssueClosingRateSwap(unittest.TestCase):
    def test_clarina_swap_detected_by_closing_unit_rate(self):
        # Image: Issue 1, Closing 21. Extracted swapped 21/1 still balances.
        item = {
            "product_name": "CLARINA ANTI ACNE CREAM 30g",
            "opening_qty": 22.0,
            "opening_value": 2200.0,  # rate 100
            "receipts_qty": 0.0,
            "sales_qty": 21.0,
            "closing_qty": 1.0,
            "closing_value": 2100.0,  # matches 21×100, not 1×100
            "extra": {"layout": "opbal_issue_closing"},
        }
        self.assertTrue(
            identity_ambiguous_sales_closing_swap(
                {
                    "opening": 22.0,
                    "purchase": 0.0,
                    "sales": 21.0,
                    "closing": 1.0,
                }
            )
        )
        self.assertTrue(issue_closing_rate_prefers_swap(item))
        self.assertTrue(_repair_issue_closing_swap_by_rate(item))
        self.assertEqual(item["sales_qty"], 1.0)
        self.assertEqual(item["closing_qty"], 21.0)

    def test_balanced_correct_row_not_swapped(self):
        item = {
            "product_name": "OK ROW",
            "opening_qty": 22.0,
            "opening_value": 2200.0,
            "receipts_qty": 0.0,
            "sales_qty": 1.0,
            "closing_qty": 21.0,
            "closing_value": 2100.0,
            "extra": {},
        }
        self.assertFalse(_repair_issue_closing_swap_by_rate(item))
        self.assertEqual(item["sales_qty"], 1.0)
        self.assertEqual(item["closing_qty"], 21.0)

    def test_zero_quantity_row_untouched(self):
        item = {
            "product_name": "ZERO MOVE",
            "opening_qty": 5.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": 5.0,
            "extra": {},
        }
        self.assertFalse(_repair_issue_closing_swap_by_rate(item))
        self.assertEqual(item["sales_qty"], 0.0)
        self.assertEqual(item["closing_qty"], 5.0)


class TestOcrIssueClosingMerge(unittest.TestCase):
    def test_clarina_vision_swap_repaired_from_ocr_positions(self):
        vision = empty_result("ashish.png", "png")
        bad = empty_line_item()
        bad["product_name"] = "CLARINA ANTI ACNE CREAM"
        bad["packing"] = "30g"
        bad["opening_qty"] = 22.0
        bad["receipts_qty"] = 0.0
        bad["sales_qty"] = 21.0  # swapped
        bad["closing_qty"] = 1.0
        bad["extra"]["total_stock_qty"] = 22.0
        vision["line_items"] = [bad]

        ocr = empty_result("ashish.png", "png")
        good = empty_line_item()
        good["product_name"] = "CLARINA ANTI ACNE CREAM"
        good["packing"] = "30g"
        good["opening_qty"] = 22.0
        good["receipts_qty"] = 0.0
        good["sales_qty"] = 1.0
        good["closing_qty"] = 21.0
        good["extra"]["total_stock_qty"] = 22.0
        ocr["line_items"] = [good]

        patched = _merge_opbal_ocr_issue_closing_into_vision(vision, ocr)
        self.assertEqual(patched, 1)
        self.assertEqual(bad["sales_qty"], 1.0)
        self.assertEqual(bad["closing_qty"], 21.0)
        self.assertIn(
            "OPBAL_OCR_ISSUE_CLOSING_MERGE",
            (bad.get("extra") or {}).get("flags") or [],
        )

    def test_liv52_parked_value_repaired_from_ocr(self):
        vision = empty_result("ashish.png", "png")
        bad = empty_line_item()
        bad["product_name"] = "LIV.52 SYRUP"
        bad["packing"] = "200ml"
        bad["opening_qty"] = 2640.0
        bad["receipts_qty"] = 0.0
        bad["sales_qty"] = None
        bad["sales_value"] = 2551.0
        bad["closing_qty"] = 89.0
        vision["line_items"] = [bad]

        ocr = empty_result("ashish.png", "png")
        good = empty_line_item()
        good["product_name"] = "LIV.52 SYRUP"
        good["packing"] = "200ml"
        good["opening_qty"] = 2640.0
        good["receipts_qty"] = 0.0
        good["sales_qty"] = 2551.0
        good["closing_qty"] = 89.0
        ocr["line_items"] = [good]

        patched = _merge_opbal_ocr_issue_closing_into_vision(vision, ocr)
        self.assertEqual(patched, 1)
        self.assertEqual(bad["sales_qty"], 2551.0)
        self.assertEqual(bad["closing_qty"], 89.0)
        self.assertIsNone(bad["sales_value"])

    def test_correct_vision_row_not_overwritten(self):
        vision = empty_result("ashish.png", "png")
        ok = empty_line_item()
        ok["product_name"] = "ABANA TABS"
        ok["packing"] = "60S"
        ok["opening_qty"] = 146.0
        ok["receipts_qty"] = 0.0
        ok["sales_qty"] = 131.0
        ok["closing_qty"] = 15.0
        vision["line_items"] = [ok]
        ocr = copy.deepcopy(vision)
        patched = _merge_opbal_ocr_issue_closing_into_vision(vision, ocr)
        self.assertEqual(patched, 0)
        self.assertEqual(ok["sales_qty"], 131.0)
        self.assertEqual(ok["closing_qty"], 15.0)

    def test_distinct_pack_sizes_not_merged(self):
        vision = empty_result("ashish.png", "png")
        v100 = empty_line_item()
        v100["product_name"] = "LIV.52 SYRUP"
        v100["packing"] = "100ml"
        v100["opening_qty"] = 410.0
        v100["receipts_qty"] = 1750.0
        v100["sales_qty"] = 1222.0  # swapped vs 938
        v100["closing_qty"] = 938.0
        vision["line_items"] = [v100]

        ocr = empty_result("ashish.png", "png")
        o200 = empty_line_item()
        o200["product_name"] = "LIV.52 SYRUP"
        o200["packing"] = "200ml"
        o200["opening_qty"] = 2640.0
        o200["receipts_qty"] = 0.0
        o200["sales_qty"] = 2551.0
        o200["closing_qty"] = 89.0
        ocr["line_items"] = [o200]

        patched = _merge_opbal_ocr_issue_closing_into_vision(vision, ocr)
        self.assertEqual(patched, 0)
        self.assertEqual(v100["sales_qty"], 1222.0)


class TestUniversalGuardAshishBundle(unittest.TestCase):
    def test_qty_only_sheet_fixes_both_defects(self):
        result = empty_result("ashish_zeal.png", "png")
        result["stockist_name"] = "ASHISH MEDICAL STORE"
        result["company_name"] = "HIMALAYA ZEAL DIVISION"
        result["line_items"] = [
            {
                "product_name": "CLARINA ANTI ACNE CREAM 30g",
                "opening_qty": 22.0,
                "opening_value": 2200.0,
                "receipts_qty": 0.0,
                "sales_qty": 21.0,
                "closing_qty": 1.0,
                "closing_value": 2100.0,
                "extra": {},
            },
            {
                "product_name": "LIV.52 SYRUP 200ml",
                "opening_qty": 2640.0,
                "receipts_qty": 0.0,
                "sales_qty": None,
                "sales_value": 2551.0,
                "closing_qty": 89.0,
                "extra": {},
            },
        ]
        result["totals"]["extra"]["qty_only"] = True
        apply_universal_sales_qty_value_guard(result)
        by_name = {i["product_name"]: i for i in result["line_items"]}
        clarina = by_name["CLARINA ANTI ACNE CREAM 30g"]
        self.assertEqual(clarina["sales_qty"], 1.0)
        self.assertEqual(clarina["closing_qty"], 21.0)
        liv = by_name["LIV.52 SYRUP 200ml"]
        self.assertEqual(liv["sales_qty"], 2551.0)
        self.assertIsNone(liv["sales_value"])
        opening = sum(float(i["opening_qty"] or 0) for i in result["line_items"])
        receipts = sum(float(i.get("receipts_qty") or 0) for i in result["line_items"])
        sales = sum(float(i.get("sales_qty") or 0) for i in result["line_items"])
        closing = sum(float(i.get("closing_qty") or 0) for i in result["line_items"])
        self.assertEqual(opening + receipts - sales, closing)


class TestAshishOpbalTextFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = FIXTURE.read_text(encoding="utf-8")
        cls.result = _parse_opbal_receipt_issue_statement(
            cls.text, "ashish_zeal_oct2026.txt", "txt"
        )

    def test_fixture_parses_expected_rows(self):
        self.assertIsNotNone(self.result)
        items = self.result["line_items"]
        self.assertEqual(len(items), 5)
        def _find(name_substr: str, pack_substr: str = "") -> dict:
            for item in items:
                name = str(item.get("product_name") or "").upper()
                pack = str(item.get("packing") or "").upper()
                blob = f"{name} {pack}"
                if name_substr.upper() not in blob:
                    continue
                if pack_substr and pack_substr.upper() not in blob:
                    continue
                return item
            self.fail(f"missing row {name_substr} {pack_substr}")
            return {}

        clarina = _find("CLARINA ANTI ACNE CREAM", "30")
        self.assertEqual(_row_tuple(clarina), EXPECTED_ROWS["CLARINA ANTI ACNE CREAM"])

        liv100 = _find("LIV.52 SYRUP", "100")
        self.assertEqual(
            _row_tuple(liv100), EXPECTED_ROWS["LIV.52 SYRUP"]["100ml"]
        )
        liv200 = _find("LIV.52 SYRUP", "200")
        self.assertEqual(
            _row_tuple(liv200), EXPECTED_ROWS["LIV.52 SYRUP"]["200ml"]
        )
        # Parser may stamp 0.0 for qty-only money — never the Issue digits.
        if liv200.get("sales_value") not in (None, 0, 0.0):
            self.fail(f"sales_value must not hold Issue qty: {liv200.get('sales_value')}")

        abana = _find("ABANA TABS")
        self.assertEqual(_row_tuple(abana), EXPECTED_ROWS["ABANA TABS"])
        pilex = _find("PILEX FORTE OINTMENT", "30")
        self.assertEqual(_row_tuple(pilex), EXPECTED_ROWS["PILEX FORTE OINTMENT"])

    def test_semantic_total_and_closing_identities(self):
        for item in self.result["line_items"]:
            op, rec, tot, issue, closing = _row_tuple(item)
            self.assertAlmostEqual(tot, op + rec, places=2, msg=item["product_name"])
            self.assertAlmostEqual(
                closing, tot - issue, places=2, msg=item["product_name"]
            )

    def test_fixture_rows_zero_variance(self):
        """Five regression products balance; full-image aggs (7731/…) need the photo."""
        items = self.result["line_items"]
        opening = sum(float(i["opening_qty"] or 0) for i in items)
        receipts = sum(float(i.get("receipts_qty") or 0) for i in items)
        issue = sum(float(i.get("sales_qty") or 0) for i in items)
        closing = sum(float(i.get("closing_qty") or 0) for i in items)
        # Fixture covers the five confirmed defect/regression rows only.
        self.assertEqual(receipts, EXPECTED_AGG["receipts"])
        self.assertEqual(opening + receipts - issue - closing, 0.0)
        self.assertEqual(opening, 4447.0)
        self.assertEqual(issue, 4021.0)
        self.assertEqual(closing, 2176.0)

    def test_full_image_aggregate_constants_documented(self):
        # Full ASHISH ZEAL Oct 2026 image footer (verified on reprocess).
        self.assertEqual(EXPECTED_AGG["opening"], 7731.0)
        self.assertEqual(EXPECTED_AGG["receipts"], 1750.0)
        self.assertEqual(EXPECTED_AGG["issue"], 4816.0)
        self.assertEqual(EXPECTED_AGG["closing"], 4665.0)
        self.assertEqual(
            EXPECTED_AGG["opening"]
            + EXPECTED_AGG["receipts"]
            - EXPECTED_AGG["issue"]
            - EXPECTED_AGG["closing"],
            0.0,
        )


class TestBalancedButSemanticallyWrong(unittest.TestCase):
    def test_swapped_issue_closing_flagged_by_rate_in_classifier(self):
        result = empty_result("bad.png", "png")
        item = {
            "product_name": "CLARINA ANTI ACNE CREAM 30g",
            "opening_qty": 22.0,
            "opening_value": 2200.0,
            "receipts_qty": 0.0,
            "sales_qty": 21.0,
            "closing_qty": 1.0,
            "closing_value": 2100.0,
            "extra": {},
        }
        result["line_items"] = [item]
        # Formula balances — still a column-assignment error via rates.
        fields = read_row_fields(item)
        self.assertTrue(identity_ambiguous_sales_closing_swap(fields))
        summary = classify_result(result)
        self.assertEqual(
            summary["row_status_counts"].get(RowStatus.COLUMN_ASSIGNMENT_SUSPECTED.value),
            1,
        )

    def test_quality_penalizes_column_assignment(self):
        result = empty_result("bad.png", "png")
        result["line_items"] = [
            {
                "product_name": "CLARINA ANTI ACNE CREAM 30g",
                "opening_qty": 22.0,
                "opening_value": 2200.0,
                "receipts_qty": 0.0,
                "sales_qty": 21.0,
                "closing_qty": 1.0,
                "closing_value": 2100.0,
                "extra": {},
            }
        ]
        result["totals"]["extra"]["extraction_method"] = "swilerp_page_split_vision"
        result["totals"]["extra"]["layout"] = "opbal_issue_closing"
        result["totals"]["extra"]["qty_only"] = True
        result["totals"]["extra"]["stock_identity_fail_count"] = 0
        result["totals"]["extra"]["shadow_row_classification"] = {
            "row_status_counts": {
                "VALID": 0,
                "COLUMN_ASSIGNMENT_SUSPECTED": 1,
                "MISSING_VALUE": 0,
                "OCR_VALUE_SUSPECTED": 0,
                "STRUCTURAL_ERROR": 0,
                "MINOR_DISCREPANCY": 0,
                "CLOSING_DERIVED": 0,
            },
            "valid_ratio": 0.0,
            "classified_rows": 1,
        }
        quality = evaluate_extraction_quality(result)
        self.assertLess(quality["score"], 100)
        self.assertIn("column_assignment_suspected", quality.get("reasons") or [])


class TestOcrDigitConfusion(unittest.TestCase):
    def test_digit_confusion_does_not_fabricate_to_force_balance(self):
        # Printed Issue 2551 misread as 2557 would not balance; repair must not
        # invent 2551 from thin air when sales_value is absent.
        item = {
            "product_name": "LIV.52 SYRUP 200ml",
            "opening_qty": 2640.0,
            "receipts_qty": 0.0,
            "sales_qty": 2557.0,
            "sales_value": None,
            "closing_qty": 89.0,
            "extra": {"qty_only": True},
        }
        before = copy.deepcopy(item)
        self.assertFalse(_repair_issue_qty_parked_in_sales_value(item))
        self.assertFalse(_repair_issue_closing_swap_by_rate(item))
        self.assertEqual(item["sales_qty"], before["sales_qty"])
        self.assertEqual(item["closing_qty"], before["closing_qty"])
        self.assertIsNone(item["sales_value"])


class TestMonthOnlyPeriod(unittest.TestCase):
    def test_month_oct_2026_expands_to_month_bounds(self):
        pf, pt = _parse_month_year_period_text("MONTH-OCT 2026")
        self.assertEqual(pf, "2026-10-01")
        self.assertEqual(pt, "2026-10-31")

    def test_coerce_replaces_duplicated_same_day(self):
        result = empty_result("ashish.png", "png")
        result["period_from"] = "2026-10-31"
        result["period_to"] = "2026-10-31"
        result["report_title"] = "STOCK STATEMENT MONTH-OCT 2026"
        _coerce_month_year_period_fields(
            result, content_text="ASHISH MEDICAL STORE\nMONTH-OCT 2026\n"
        )
        self.assertEqual(result["period_from"], "2026-10-01")
        self.assertEqual(result["period_to"], "2026-10-31")
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("statement_period_precision"), "month")
        self.assertEqual(extra.get("statement_month"), "2026-10")


if __name__ == "__main__":
    unittest.main()
