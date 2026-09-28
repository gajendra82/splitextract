"""STOCK REPORT with Particular / Packing / Op / Pur / Sl / Cl stays on its own reader."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _particular_packing_header_fields,
    _particular_packing_stock_report_text,
)

_LUCKY = """
LUCKY MEDICALS
VIDYAPATI CHOWK,BENIPATTI
MADHUBANI
STOCK REPORT
01/08/26 To :31/08/26
For HIMALAYA ZEAL
"""


class TestParticularPackingStockReportText(unittest.TestCase):
    def test_header_is_stockist_not_company(self):
        self.assertTrue(_particular_packing_stock_report_text(_LUCKY))
        fields = _particular_packing_header_fields(_LUCKY)
        self.assertEqual(fields["stockist_name"], "LUCKY MEDICALS")
        self.assertIn("VIDYAPATI", fields["stockist_address"])
        self.assertEqual(fields["company_name"], "HIMALAYA (ZEAL)")
        self.assertEqual(fields["period_from"], "2026-08-01")
        self.assertEqual(fields["period_to"], "2026-08-31")

    def test_other_layouts_do_not_match(self):
        self.assertFalse(
            _particular_packing_stock_report_text(
                "HIMALAYA DRUGS [ZEAL]\nStock Report From 01-08-2026 To 31-08-2026"
            )
        )
        self.assertFalse(
            _particular_packing_stock_report_text(
                "ZANDRA\nORDER FORM\n01/08/26 To 31/08/26\nZEAL"
            )
        )
        self.assertFalse(
            _particular_packing_stock_report_text(
                "STOCK & SALES AND M.EXP\n01/08/26 To 31/08/26"
            )
        )


if __name__ == "__main__":
    unittest.main()
