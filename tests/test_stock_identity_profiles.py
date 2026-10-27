"""Identity profiles are shared by validation and Phase 2 row targeting."""

from __future__ import annotations

import copy
import unittest

from services.recovery_runner import _failing_rows, apply_phase2_recovery
from services.sales_statement_extractor import (
    STOCK_IDENTITY_SALERET,
    STOCK_IDENTITY_SAMPLE,
    _apply_stock_identity_validation,
    empty_line_item,
)
from services.stock_identity_profiles import (
    PROFILE_GENERIC,
    PROFILE_MONTHLY_SS,
    PROFILE_NOT_SCORED,
    PROFILE_SALERET,
    PROFILE_SAMPLE,
    PROFILE_UNKNOWN,
    failed_rows,
    generic_expected_closing,
    monthly_expected_closing,
    saleret_expected_closing,
    sample_expected_closing,
    select_profile,
)


def _item(name, opening, receipts, sales, closing, **extra_fields):
    item = empty_line_item()
    item["product_name"] = name
    item["opening_qty"] = opening
    item["receipts_qty"] = receipts
    item["sales_qty"] = sales
    item["closing_qty"] = closing
    item["extra"].update(extra_fields)
    return item


def _statement(items, *, method="swil_erp", kind="opening_receipts_sales_closing", layout=None):
    extra = {
        "extraction_method": method,
        "stock_identity_kind": kind,
    }
    if layout:
        extra["layout"] = layout
    return {
        "line_items": items,
        "totals": {"sales_value": None, "closing_value": None, "extra": extra},
    }


def _flagged_failures(statement):
    return [
        item["product_name"]
        for item in statement["line_items"]
        if isinstance(item, dict) and (item.get("extra") or {}).get("stock_identity_ok") is False
    ]


class StockIdentityProfileTests(unittest.TestCase):
    def test_generic_formula_includes_scheme_and_stock_out(self):
        item = _item("ROW", 20, 5, 4, 18, sales_scheme_qty=2, stock_out_qty=1)
        self.assertEqual(generic_expected_closing(item), 18.0)
        statement = _statement([item])
        self.assertEqual(select_profile(statement).profile_id, PROFILE_GENERIC)
        checked = _apply_stock_identity_validation(copy.deepcopy(statement))
        self.assertTrue(checked["line_items"][0]["extra"]["stock_identity_ok"])

    def test_stock_out_balance_passes_and_printed_gap_fails(self):
        balanced = _item("PILEX", 106, 100, 131, 74, stock_out_qty=1)
        also_balanced = _item("MANJISHTHA", 48, 0, 8, 39, stock_out_qty=1)
        clarina = _item("CLARINA", 47, 0, 4, 45, stock_out_qty=0)
        kapikachhu = _item("KAPIKACHHU", 40, 0, 1, 40, stock_out_qty=0)
        statement = _statement(
            [balanced, also_balanced, clarina, kapikachhu],
            method="pharma_hub_jun_jul_photo",
        )
        checked = _apply_stock_identity_validation(copy.deepcopy(statement))
        by_name = {item["product_name"]: item for item in checked["line_items"]}
        self.assertEqual(by_name["PILEX"]["extra"]["expected_closing"], 74.0)
        self.assertTrue(by_name["PILEX"]["extra"]["stock_identity_ok"])
        self.assertEqual(by_name["MANJISHTHA"]["extra"]["expected_closing"], 39.0)
        self.assertTrue(by_name["MANJISHTHA"]["extra"]["stock_identity_ok"])
        self.assertEqual(by_name["CLARINA"]["extra"]["expected_closing"], 43.0)
        self.assertFalse(by_name["CLARINA"]["extra"]["stock_identity_ok"])
        self.assertEqual(by_name["KAPIKACHHU"]["extra"]["expected_closing"], 39.0)
        self.assertFalse(by_name["KAPIKACHHU"]["extra"]["stock_identity_ok"])
        self.assertEqual(_flagged_failures(checked), failed_names(checked))
        self.assertEqual(
            [item["product_name"] for item in _failing_rows(checked)],
            failed_names(checked),
        )

    def test_saleret_formula_is_unchanged(self):
        item = _item("LIQ", 27, 0, 26, 1, total_stock=27, sale_return=2, exp_damage=2)
        self.assertEqual(saleret_expected_closing(item), 1.0)
        statement = _statement(
            [item],
            method="product_stock_report",
            kind=STOCK_IDENTITY_SALERET,
        )
        self.assertEqual(select_profile(statement).profile_id, PROFILE_SALERET)
        checked = _apply_stock_identity_validation(copy.deepcopy(statement))
        self.assertTrue(checked["line_items"][0]["extra"]["stock_identity_ok"])
        broken = _statement(
            [_item("LIQ", 27, 0, 26, 9, total_stock=27, sale_return=2, exp_damage=2)],
            method="product_stock_report",
            kind=STOCK_IDENTITY_SALERET,
        )
        checked_broken = _apply_stock_identity_validation(broken)
        self.assertFalse(checked_broken["line_items"][0]["extra"]["stock_identity_ok"])
        self.assertEqual(failed_names(checked_broken), ["LIQ"])

    def test_sample_formula_is_unchanged(self):
        item = _item("SAMPLE", 10, 0, 3, 5, total_stock=10, sample_qty=1, expiry_qty=1)
        self.assertEqual(sample_expected_closing(item), 5.0)
        statement = _statement([item], kind=STOCK_IDENTITY_SAMPLE)
        self.assertEqual(select_profile(statement).profile_id, PROFILE_SAMPLE)
        checked = _apply_stock_identity_validation(copy.deepcopy(statement))
        self.assertTrue(checked["line_items"][0]["extra"]["stock_identity_ok"])
        self.assertEqual(failed_names(checked), [])

    def test_not_scored_layout_is_not_targeted_by_the_generic_equation(self):
        item = _item("OPENING", 10, 4, 0, 99)
        statement = _statement(
            [item],
            method="zl_secondary_xlsx",
            kind="zl_secondary_xlsx",
            layout="zl_opening_primary_closing",
        )
        profile = select_profile(statement)
        self.assertEqual(profile.profile_id, PROFILE_NOT_SCORED)
        self.assertFalse(profile.supports_recovery)
        self.assertEqual(failed_rows(statement), [])
        checked = _apply_stock_identity_validation(copy.deepcopy(statement))
        self.assertTrue(checked["totals"]["extra"]["stock_validation"]["is_valid"])
        self.assertTrue(checked["line_items"][0]["extra"]["stock_identity_ok"])
        self.assertEqual(_failing_rows(checked), [])

    def test_monthly_ss_does_not_subtract_scheme(self):
        item = _item("MONTH", 10, 0, 2, 8, sales_scheme_qty=5)
        self.assertEqual(monthly_expected_closing(item), 8.0)
        self.assertNotEqual(generic_expected_closing(item), 8.0)
        statement = _statement(
            [item],
            method="monthly_ss_report",
            kind="monthly_ss_report",
            layout="monthly_ss_opening_pur_sale_closing",
        )
        self.assertEqual(select_profile(statement).profile_id, PROFILE_MONTHLY_SS)
        checked = _apply_stock_identity_validation(copy.deepcopy(statement))
        self.assertTrue(checked["line_items"][0]["extra"]["stock_identity_ok"])
        self.assertEqual(checked["line_items"][0]["extra"]["expected_closing"], 8.0)
        self.assertEqual(failed_names(checked), [])

    def test_unknown_layout_does_not_use_the_generic_equation_or_recovery(self):
        item = _item("FUTURE", 47, 0, 4, 45, stock_out_qty=0)
        statement = _statement(
            [item],
            method="unseen_customer_layout",
            kind="unseen_customer_layout",
        )
        profile = select_profile(statement)
        self.assertEqual(profile.profile_id, PROFILE_UNKNOWN)
        self.assertFalse(profile.supports_recovery)
        self.assertEqual(failed_rows(statement), [])
        checked = _apply_stock_identity_validation(copy.deepcopy(statement))
        self.assertNotIn("expected_closing", checked["line_items"][0]["extra"])
        self.assertEqual(checked["totals"]["extra"]["stock_identity_kind"], PROFILE_UNKNOWN)
        self.assertFalse(checked["totals"]["extra"]["stock_validation"]["is_valid"])
        self.assertEqual(
            checked["totals"]["extra"]["stock_validation"]["reason"],
            "identity_profile_unknown",
        )
        checked["phase1"] = {
            "route": {},
            "validation": {
                "accepted": False,
                "reasons": ["stock_identity_failure"],
                "checks": {
                    "schema": True,
                    "quality": True,
                    "stock_identity": False,
                    "rows": True,
                    "duplicates": True,
                },
                "parser": "unseen_customer_layout",
            },
        }

        def caller(_payload):
            raise AssertionError("unknown layout must not start stock recovery")

        recovered = apply_phase2_recovery(
            checked,
            gemini_caller=caller,
            filename="future.xlsx",
        )
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertEqual(recovered["phase2"]["attempts"], 0)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 45)

    def test_validator_failures_match_recovery_targets(self):
        cases = [
            _statement([_item("BAD", 10, 0, 1, 10), _item("GOOD", 10, 0, 1, 9)]),
            _statement(
                [_item("RET", 27, 0, 26, 9, total_stock=27, sale_return=2, exp_damage=2)],
                method="product_stock_report",
                kind=STOCK_IDENTITY_SALERET,
            ),
            _statement(
                [_item("SAMP", 10, 0, 3, 9, total_stock=10, sample_qty=1, expiry_qty=1)],
                kind=STOCK_IDENTITY_SAMPLE,
            ),
            _statement(
                [_item("MONTH", 10, 0, 2, 3, sales_scheme_qty=5)],
                method="monthly_ss_report",
                layout="monthly_ss_opening_pur_sale_closing",
            ),
            _statement(
                [_item("WISE", 10, 2, 4, 9)],
                method="product_wise_stock_statement",
            ),
            _statement(
                [_item("DUMP", 10, 0, 1, 9, shortage_qty=1)],
                method="himalaya_wellness_dump_statement",
                layout="himalaya_dump_statement",
            ),
            _statement(
                [_item("MEDICA", 10, 0, 3, 99)],
                method="medica_opstk_columns",
            ),
        ]
        for statement in cases:
            checked = _apply_stock_identity_validation(copy.deepcopy(statement))
            self.assertEqual(
                _flagged_failures(checked),
                [item["product_name"] for item in _failing_rows(checked)],
                select_profile(statement).profile_id,
            )


def failed_names(statement):
    return [item["product_name"] for item in failed_rows(statement)]


if __name__ == "__main__":
    unittest.main()
