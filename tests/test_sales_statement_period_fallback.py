"""period_from fallback to period_to in sales-statement normalization."""

from __future__ import annotations

import json
import unittest

from services.sales_statement_extractor import (
    _sanitize_statement_financials,
    extract_sales_statement,
)


def _statement(**overrides):
    base = {
        "source_file": "stmt.pdf",
        "source_format": "pdf",
        "stockist_name": "TEST STOCKIST",
        "period_from": None,
        "period_to": None,
        "line_items": [],
        "totals": {"sales_value": None, "closing_value": None, "extra": {}},
    }
    base.update(overrides)
    return base


class TestPeriodFromFallback(unittest.TestCase):
    def test_valid_period_from_unchanged(self):
        result = _sanitize_statement_financials(
            _statement(period_from="2026-09-01", period_to="2026-09-30")
        )
        self.assertEqual(result["period_from"], "2026-09-01")
        self.assertEqual(result["period_to"], "2026-09-30")

    def test_null_period_from_uses_period_to(self):
        result = _sanitize_statement_financials(
            _statement(period_from=None, period_to="2026-09-30")
        )
        self.assertEqual(result["period_from"], "2026-09-30")
        self.assertEqual(result["period_to"], "2026-09-30")

    def test_empty_period_from_uses_period_to(self):
        result = _sanitize_statement_financials(
            _statement(period_from="", period_to="2026-08-31")
        )
        self.assertEqual(result["period_from"], "2026-08-31")
        self.assertEqual(result["period_to"], "2026-08-31")

    def test_whitespace_period_from_uses_period_to(self):
        result = _sanitize_statement_financials(
            _statement(period_from="   ", period_to="2026-07-31")
        )
        self.assertEqual(result["period_from"], "2026-07-31")
        self.assertEqual(result["period_to"], "2026-07-31")

    def test_missing_period_from_uses_period_to(self):
        stmt = _statement(period_to="2026-06-30")
        del stmt["period_from"]
        result = _sanitize_statement_financials(stmt)
        self.assertEqual(result["period_from"], "2026-06-30")
        self.assertEqual(result["period_to"], "2026-06-30")

    def test_both_dates_missing_preserved(self):
        result = _sanitize_statement_financials(
            _statement(period_from=None, period_to=None)
        )
        self.assertIsNone(result["period_from"])
        self.assertIsNone(result["period_to"])

    def test_both_dates_empty_preserved(self):
        result = _sanitize_statement_financials(
            _statement(period_from="", period_to="  ")
        )
        self.assertEqual(result["period_from"], "")
        self.assertEqual(result["period_to"], "  ")

    def test_valid_period_from_not_replaced_when_different(self):
        result = _sanitize_statement_financials(
            _statement(period_from="2026-09-01", period_to="2026-09-30")
        )
        self.assertEqual(result["period_from"], "2026-09-01")
        self.assertNotEqual(result["period_from"], result["period_to"])

    def test_unparseable_period_from_uses_period_to(self):
        result = _sanitize_statement_financials(
            _statement(period_from="not-a-date", period_to="2026-05-31")
        )
        self.assertEqual(result["period_from"], "2026-05-31")
        self.assertEqual(result["period_to"], "2026-05-31")

    def test_multi_statement_fallback_applied_independently(self):
        payload = {
            "source_file": "multi.pdf",
            "source_format": "pdf",
            "multi_statement": True,
            "statement_count": 3,
            "statements": [
                _statement(period_from=None, period_to="2026-09-30"),
                _statement(period_from="2026-08-01", period_to="2026-08-31"),
                _statement(period_from="", period_to=None),
            ],
        }
        result = _sanitize_statement_financials(payload)
        stmts = result["statements"]
        self.assertEqual(stmts[0]["period_from"], "2026-09-30")
        self.assertEqual(stmts[0]["period_to"], "2026-09-30")
        self.assertEqual(stmts[1]["period_from"], "2026-08-01")
        self.assertEqual(stmts[1]["period_to"], "2026-08-31")
        self.assertEqual(stmts[2]["period_from"], "")
        self.assertIsNone(stmts[2]["period_to"])

    def test_json_response_contains_corrected_period_from(self):
        result = _sanitize_statement_financials(
            _statement(period_from=None, period_to="2026-09-30")
        )
        # Same encoding path as FastAPI JSONResponse(content=result)
        body = json.loads(json.dumps(result))
        self.assertEqual(body["period_from"], "2026-09-30")
        self.assertEqual(body["period_to"], "2026-09-30")
        self.assertIsInstance(body["period_from"], str)
        self.assertNotIn(body["period_from"], (None, "", "null"))

    def test_extract_sales_statement_keeps_valid_from_to_range(self):
        text = (
            "KAVERI MEDICALS\n"
            "MONTHLY STOCK & SALES\n"
            "COMPANY NAME : AUROBINDO\n"
            "FROM : 01/09/2026 TO 30/09/2026\n"
        )
        result = extract_sales_statement(text.encode("utf-8"), "kaveri.txt")
        self.assertEqual(result["period_from"], "2026-09-01")
        self.assertEqual(result["period_to"], "2026-09-30")


if __name__ == "__main__":
    unittest.main()
