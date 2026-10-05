"""Statement period must come from report content — never filename/upload."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from services.sales_statement_extractor import (
    _accept_vision_statement_period,
    _extract_report_period_from_content,
    _finalize_statement_period_fields,
    empty_result,
    extract_sales_statement,
)


SAMPLE_TXT = Path(
    "/var/www/html/splitextract/0000700697_2026_08_ZA_25_379_07092026151037.txt"
)

CLEAR_HEADER = """
M/S SAMPLE AGENCY
STOCK & SALES ANALYSIS 01/08/2026 - 31/08/2026
ITEM DESCRIPTION                       OPENING   RECEIPT     ISSUE   CLOSING
LIV 52 TAB                 60'S             10         5         2        13
TOTAL                                    10         5         2        13
"""

NO_PERIOD = """
M/S SAMPLE AGENCY
STOCK & SALES ANALYSIS
ITEM DESCRIPTION                       OPENING   RECEIPT     ISSUE   CLOSING
LIV 52 TAB                 60'S             10         5         2        13
TOTAL                                    10         5         2        13
"""

TXN_DATES_ONLY = """
M/S SAMPLE AGENCY
STOCK & SALES ANALYSIS
ITEM DESCRIPTION                       OPENING   RECEIPT     ISSUE   CLOSING
Invoice 12/07/2026 LIV 52 TAB 60'S          10         5         2        13
Printed on 07/09/2026
TOTAL                                    10         5         2        13
"""


class TestReportPeriodFromContent(unittest.TestCase):
    def test_clear_report_period_extracted(self):
        pf, pt, source = _extract_report_period_from_content(CLEAR_HEADER)
        self.assertEqual(pf, "2026-08-01")
        self.assertEqual(pt, "2026-08-31")
        self.assertIn(source, {"report_header", "report_period", "report_from_to"})

    def test_no_report_period_returns_null(self):
        pf, pt, source = _extract_report_period_from_content(NO_PERIOD)
        self.assertIsNone(pf)
        self.assertIsNone(pt)
        self.assertIsNone(source)

    def test_unrelated_transaction_dates_ignored(self):
        pf, pt, source = _extract_report_period_from_content(TXN_DATES_ONLY)
        self.assertIsNone(pf)
        self.assertIsNone(pt)
        self.assertIsNone(source)

    def test_filename_date_not_used_by_content_extractor(self):
        # Content has no period; filename-like string in text is not a report header.
        text = "upload 0000700697_2026_08_ZA_25_379_07092026151037\nITEM OPENING\n"
        pf, pt, source = _extract_report_period_from_content(text)
        self.assertIsNone(pf)
        self.assertIsNone(pt)


class TestVisionPeriodValidation(unittest.TestCase):
    def test_range_accepted(self):
        pf, pt = _accept_vision_statement_period("01/08/2026 - 31/08/2026")
        self.assertEqual(pf, "2026-08-01")
        self.assertEqual(pt, "2026-08-31")

    def test_single_date_rejected(self):
        pf, pt = _accept_vision_statement_period("07/09/2026")
        self.assertIsNone(pf)
        self.assertIsNone(pt)


class TestFinalizePeriodFields(unittest.TestCase):
    def test_missing_period_stays_null_and_logs(self):
        result = empty_result("x.txt", "txt")
        result["line_items"] = [
            {
                "product_name": "LIV 52",
                "opening_qty": 1,
                "receipts_qty": 0,
                "sales_qty": 0,
                "closing_qty": 1,
            }
        ]
        with self.assertLogs("services.sales_statement_extractor", level="INFO") as cm:
            out = _finalize_statement_period_fields(result, content_text=NO_PERIOD)
        self.assertIsNone(out.get("period_from"))
        self.assertIsNone(out.get("period_to"))
        extra = (out.get("totals") or {}).get("extra") or {}
        self.assertIsNone(extra.get("statement_month"))
        self.assertTrue(any("STATEMENT_PERIOD_NOT_FOUND" in m for m in cm.output))
        # Products preserved.
        self.assertEqual(len(out.get("line_items") or []), 1)

    def test_content_period_fills_when_missing(self):
        result = empty_result("x.txt", "txt")
        out = _finalize_statement_period_fields(result, content_text=CLEAR_HEADER)
        self.assertEqual(out.get("period_from"), "2026-08-01")
        self.assertEqual(out.get("period_to"), "2026-08-31")
        extra = (out.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("statement_month"), "2026-08")


class TestSampleTxtStatementPeriod(unittest.TestCase):
    @unittest.skipUnless(SAMPLE_TXT.is_file(), "sample txt missing")
    def test_sample_has_printed_period_and_products(self):
        """Sample body prints STOCK & SALES ANALYSIS 01-08-2026 - 31-08-2026."""
        result = extract_sales_statement(SAMPLE_TXT.read_bytes(), SAMPLE_TXT.name)
        self.assertEqual(result.get("period_from"), "2026-08-01")
        self.assertEqual(result.get("period_to"), "2026-08-31")
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("statement_month"), "2026-08")
        self.assertEqual(extra.get("statement_period_source"), "report_header")
        items = result.get("line_items") or []
        self.assertGreater(len(items), 20)
        # Filename stamp 07092026 must not replace the printed August period.
        self.assertNotEqual(result.get("period_from"), "2026-09-07")
        self.assertNotEqual(result.get("period_to"), "2026-09-07")

    def test_filename_only_period_stays_null(self):
        text = NO_PERIOD
        result = extract_sales_statement(
            text.encode("utf-8"),
            "0000700697_2026_08_ZA_25_379_07092026151037.txt",
        )
        self.assertIsNone(result.get("period_from"))
        self.assertIsNone(result.get("period_to"))
        # Products still extracted when period is absent.
        self.assertGreater(len(result.get("line_items") or []), 0)


class TestZlScanNoFilenamePeriod(unittest.TestCase):
    def test_filename_fallback_disabled(self):
        # Unit-level: content-only flag rejects inventing from filename pattern.
        from services.sales_statement_extractor import (
            _statement_period_content_only_enabled,
        )

        with mock.patch.dict(
            "os.environ", {"STOCK_STATEMENT_PERIOD_CONTENT_ONLY": "true"}, clear=False
        ):
            self.assertTrue(_statement_period_content_only_enabled())


if __name__ == "__main__":
    unittest.main()
