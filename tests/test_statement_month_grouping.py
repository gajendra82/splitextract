"""Group extracted statements by stockist + calendar month, not by page."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _apply_period_from_fallback,
    _group_statements_by_stockist_month,
    _sanitize_statement_financials,
    empty_line_item,
    empty_result,
)


def _item(name, sales_qty=1):
    item = empty_line_item()
    item["product_name"] = name
    item["packing"] = "60"
    item["opening_qty"] = 10
    item["receipts_qty"] = 0
    item["sales_qty"] = sales_qty
    item["sales_value"] = 0
    item["closing_qty"] = 9
    item["closing_value"] = 0
    return item


def _section(stockist, period_from, period_to, names, sales_value=None, company=None):
    stmt = empty_result("sample.pdf", "pdf")
    stmt["stockist_name"] = stockist
    stmt["company_name"] = company
    stmt["period_from"] = period_from
    stmt["period_to"] = period_to
    stmt["line_items"] = [_item(name) for name in names]
    if sales_value is not None:
        stmt["totals"]["sales_value"] = sales_value
    return stmt


def _wrap(sections):
    return {
        "source_file": "sample.pdf",
        "source_format": "pdf",
        "multi_statement": True,
        "statement_count": len(sections),
        "statements": sections,
    }


class TestStatementMonthGrouping(unittest.TestCase):
    def test_same_stockist_same_month_two_pages_is_one_statement(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section("MEENA PHARMA", "2026-08-01", "2026-08-31", ["A", "B", "C"], 100),
                    _section("MEENA PHARMA", "2026-08-01", "2026-08-31", ["D", "E", "F"], 100),
                ]
            )
        )
        self.assertFalse(result.get("multi_statement"))
        self.assertNotIn("statements", result)
        self.assertEqual(result["stockist_name"], "MEENA PHARMA")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        self.assertEqual(
            [item["product_name"] for item in result["line_items"]],
            ["A", "B", "C", "D", "E", "F"],
        )
        self.assertEqual(result["totals"]["sales_value"], 100)

    def test_same_stockist_same_month_five_pages_merge_without_double_total(self):
        pages = [
            _section("MEENA PHARMA", "2026-08-01", "2026-08-31", [f"P{i}"], 250)
            for i in range(5)
        ]
        result = _group_statements_by_stockist_month(_wrap(pages))
        self.assertEqual(len(result["line_items"]), 5)
        self.assertEqual(result["totals"]["sales_value"], 250)
        self.assertFalse(result.get("multi_statement"))

    def test_partial_august_ranges_merge(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section("MEENA PHARMA", "2026-08-01", "2026-08-15", ["A"]),
                    _section("MEENA PHARMA", "2026-08-16", "2026-08-31", ["B"]),
                ]
            )
        )
        self.assertEqual(
            [item["product_name"] for item in result["line_items"]],
            ["A", "B"],
        )
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")

    def test_same_stockist_different_months_stay_separate(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section("MEENA PHARMA", "2026-08-01", "2026-08-31", ["A"]),
                    _section("MEENA PHARMA", "2026-09-01", "2026-09-30", ["B"]),
                ]
            )
        )
        self.assertTrue(result.get("multi_statement"))
        self.assertEqual(result["statement_count"], 2)
        self.assertEqual(result["statements"][0]["period_from"], "2026-08-01")
        self.assertEqual(result["statements"][1]["period_from"], "2026-09-01")
        self.assertEqual(result["statements"][0]["line_items"][0]["product_name"], "A")
        self.assertEqual(result["statements"][1]["line_items"][0]["product_name"], "B")

    def test_different_stockists_same_month_stay_separate(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section("MEENA PHARMA", "2026-08-01", "2026-08-31", ["A"], company="HIMALAYA"),
                    _section("ABC MEDICAL", "2026-08-01", "2026-08-31", ["B"], company="HIMALAYA"),
                ]
            )
        )
        self.assertEqual(result["statement_count"], 2)
        self.assertEqual(result["statements"][0]["stockist_name"], "MEENA PHARMA")
        self.assertEqual(result["statements"][1]["stockist_name"], "ABC MEDICAL")

    def test_different_stockists_and_months_stay_separate(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section("MEENA PHARMA", "2026-08-01", "2026-08-31", ["A"]),
                    _section("ABC MEDICAL", "2026-09-01", "2026-09-30", ["B"]),
                ]
            )
        )
        self.assertEqual(len(result["statements"]), 2)

    def test_run_date_does_not_change_august_period(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section(
                        "NARAYAN MEDICAL (NEW) 02/09/2026",
                        "2026-08-01",
                        "2026-08-31",
                        ["AACTARIL SOAP"],
                    ),
                    _section(
                        "NARAYAN MEDICAL (NEW)",
                        "2026-08-01",
                        "2026-08-31",
                        ["ABANA TAB"],
                    ),
                ]
            )
        )
        self.assertFalse(result.get("multi_statement"))
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        self.assertNotEqual(result["period_from"][:7], "2026-09")
        self.assertEqual(
            [item["product_name"] for item in result["line_items"]],
            ["AACTARIL SOAP", "ABANA TAB"],
        )

    def test_repeated_header_is_not_added_as_a_product(self):
        page = _section("MEENA PHARMA", "2026-08-01", "2026-08-31", ["LIV 52"])
        result = _group_statements_by_stockist_month(_wrap([page, page]))
        names = [item["product_name"] for item in result["line_items"]]
        self.assertNotIn("Product & Pack", names)
        self.assertNotIn("MEENA PHARMA", names)
        self.assertEqual(names, ["LIV 52", "LIV 52"])

    def test_page_subtotals_are_summed_when_they_differ(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section("MEENA PHARMA", "2026-08-01", "2026-08-31", ["A"], 100),
                    _section("MEENA PHARMA", "2026-08-01", "2026-08-31", ["B"], 40),
                ]
            )
        )
        self.assertEqual(result["totals"]["sales_value"], 140)

    def test_period_from_fallback_survives_grouping(self):
        first = _section("MEENA PHARMA", None, "2026-09-30", ["A"])
        second = _section("ABC MEDICAL", None, "2026-08-31", ["B"])
        payload = _sanitize_statement_financials(_wrap([first, second]))
        result = _group_statements_by_stockist_month(payload)
        self.assertEqual(result["statements"][0]["period_from"], "2026-09-30")
        self.assertEqual(result["statements"][1]["period_from"], "2026-08-31")
        untouched = _apply_period_from_fallback(
            _section("MEENA PHARMA", None, "2026-09-30", ["A"])
        )
        self.assertEqual(untouched["period_from"], "2026-09-30")

    def test_single_statement_object_is_unchanged(self):
        single = _section("MEENA PHARMA", "2026-08-01", "2026-08-31", ["A"])
        self.assertIs(_group_statements_by_stockist_month(single), single)

    def test_missing_dates_are_not_invented_or_merged(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section("MEENA PHARMA", None, None, ["A"]),
                    _section("MEENA PHARMA", None, None, ["B"]),
                ]
            )
        )
        self.assertEqual(result["statement_count"], 2)
        self.assertIsNone(result["statements"][0]["period_from"])
        self.assertIsNone(result["statements"][1]["period_to"])

    def test_single_stockist_single_month_stays_flat(self):
        single = _section("MEENA PHARMA", "2026-08-01", "2026-08-31", ["A", "B"])
        result = _group_statements_by_stockist_month(single)
        self.assertIs(result, single)
        self.assertNotIn("statements", result)
        self.assertNotIn("multi_statement", result)
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")

    def test_three_august_ranges_become_one_month(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section("MEENA PHARMA", "2026-08-01", "2026-08-10", ["A"]),
                    _section("MEENA PHARMA", "2026-08-11", "2026-08-20", ["B"]),
                    _section("MEENA PHARMA", "2026-08-21", "2026-08-31", ["C"]),
                ]
            )
        )
        self.assertNotIn("statements", result)
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        self.assertEqual(
            [item["product_name"] for item in result["line_items"]],
            ["A", "B", "C"],
        )

    def test_ten_pages_same_month_are_one_statement(self):
        pages = [
            _section("MEENA PHARMA", "2026-08-01", "2026-08-31", [f"P{i}"], 900)
            for i in range(10)
        ]
        result = _group_statements_by_stockist_month(_wrap(pages))
        self.assertNotIn("statements", result)
        self.assertEqual(len(result["line_items"]), 10)
        self.assertEqual(result["totals"]["sales_value"], 900)
        self.assertEqual(result["stockist_name"], "MEENA PHARMA")

    def test_two_stockists_two_months_return_every_statement(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section("MEENA PHARMA", "2026-08-01", "2026-08-31", ["A"]),
                    _section("MEENA PHARMA", "2026-09-01", "2026-09-30", ["B"]),
                    _section("ABC MEDICAL", "2026-08-01", "2026-08-31", ["C"]),
                    _section("ABC MEDICAL", "2026-09-01", "2026-09-30", ["D"]),
                ]
            )
        )
        self.assertTrue(result["multi_statement"])
        self.assertEqual(result["statement_count"], 4)
        self.assertEqual(len(result["statements"]), 4)
        self.assertEqual(
            [
                (stmt["stockist_name"], stmt["period_from"][:7])
                for stmt in result["statements"]
            ],
            [
                ("MEENA PHARMA", "2026-08"),
                ("MEENA PHARMA", "2026-09"),
                ("ABC MEDICAL", "2026-08"),
                ("ABC MEDICAL", "2026-09"),
            ],
        )
        self.assertEqual(
            [stmt["line_items"][0]["product_name"] for stmt in result["statements"]],
            ["A", "B", "C", "D"],
        )

    def test_stockist_spacing_and_case_share_one_key_but_keep_original_name(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section(" Meena   Pharma ", "2026-08-01", "2026-08-15", ["A"]),
                    _section("MEENA\u00a0PHARMA", "2026-08-16", "2026-08-31", ["B"]),
                ]
            )
        )
        self.assertNotIn("statements", result)
        self.assertEqual(result["stockist_name"], " Meena   Pharma ")
        self.assertEqual(
            [item["product_name"] for item in result["line_items"]],
            ["A", "B"],
        )

    def test_missing_stockist_is_not_guessed_or_merged(self):
        first = _section(None, "2026-08-01", "2026-08-31", ["A"], company="HIMALAYA")
        second = _section(None, "2026-08-01", "2026-08-31", ["B"], company="HIMALAYA")
        first["stockist_name"] = None
        second["stockist_name"] = None
        original = _wrap([first, second])
        result = _group_statements_by_stockist_month(original)
        self.assertIs(result, original)
        self.assertEqual(result["statement_count"], 2)
        self.assertIsNone(result["statements"][0]["stockist_name"])
        self.assertIsNone(result["statements"][1]["stockist_name"])
        self.assertEqual(result["statements"][0]["line_items"][0]["product_name"], "A")
        self.assertEqual(result["statements"][1]["line_items"][0]["product_name"], "B")

    def test_period_from_month_wins_over_period_to(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section("MEENA PHARMA", "2026-08-01", "2026-09-15", ["A"]),
                    _section("MEENA PHARMA", "2026-09-01", "2026-09-30", ["B"]),
                ]
            )
        )
        self.assertEqual(result["statement_count"], 2)
        self.assertEqual(result["statements"][0]["period_from"], "2026-08-01")
        self.assertEqual(result["statements"][1]["period_from"], "2026-09-01")

    def test_period_to_month_used_when_period_from_missing(self):
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section("MEENA PHARMA", None, "2026-08-31", ["A"]),
                    _section("MEENA PHARMA", "2026-08-16", "2026-08-31", ["B"]),
                ]
            )
        )
        self.assertNotIn("statements", result)
        self.assertEqual(result["period_from"], "2026-08-16")
        self.assertEqual(result["period_to"], "2026-08-31")
        self.assertEqual(
            [item["product_name"] for item in result["line_items"]],
            ["A", "B"],
        )

    def test_nonadjacent_same_month_pages_merge_and_keep_other_stockist(self):
        abc = _section("ABC MEDICAL", "2026-08-01", "2026-08-31", ["C"])
        result = _group_statements_by_stockist_month(
            _wrap(
                [
                    _section("MEENA PHARMA", "2026-08-01", "2026-08-15", ["A"]),
                    abc,
                    _section("MEENA PHARMA", "2026-08-16", "2026-08-31", ["B"]),
                ]
            )
        )
        self.assertEqual(result["statement_count"], 2)
        self.assertEqual(
            [item["product_name"] for item in result["statements"][0]["line_items"]],
            ["A", "B"],
        )
        self.assertIs(result["statements"][1], abc)

    def test_merged_line_item_fields_are_not_rewritten(self):
        first = _section("MEENA PHARMA", "2026-08-01", "2026-08-15", ["LIV 52"])
        second = _section("MEENA PHARMA", "2026-08-16", "2026-08-31", ["ABANA"])
        first["line_items"][0]["product_code"] = "L52"
        first["line_items"][0]["packing"] = "60'S"
        first["line_items"][0]["opening_qty"] = 4
        first["line_items"][0]["receipts_qty"] = 1
        first["line_items"][0]["sales_qty"] = 2
        first["line_items"][0]["sales_value"] = 30
        first["line_items"][0]["closing_qty"] = 3
        first["line_items"][0]["closing_value"] = 45
        first["line_items"][0]["extra"] = {"company_name": "ZEAL"}
        result = _group_statements_by_stockist_month(_wrap([first, second]))
        kept = result["line_items"][0]
        self.assertEqual(kept["product_code"], "L52")
        self.assertEqual(kept["product_name"], "LIV 52")
        self.assertEqual(kept["packing"], "60'S")
        self.assertEqual(kept["opening_qty"], 4)
        self.assertEqual(kept["receipts_qty"], 1)
        self.assertEqual(kept["sales_qty"], 2)
        self.assertEqual(kept["sales_value"], 30)
        self.assertEqual(kept["closing_qty"], 3)
        self.assertEqual(kept["closing_value"], 45)
        self.assertEqual(kept["extra"], {"company_name": "ZEAL"})
        self.assertEqual(result["line_items"][1]["product_name"], "ABANA")


if __name__ == "__main__":
    unittest.main()
