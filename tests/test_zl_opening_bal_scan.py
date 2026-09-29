"""Scanned TapScanner ZL Opening_bal dumps (OCR-tolerant gate + vision route)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_zl_opening_bal_scan_text,
    extract_sales_statement,
)


# Garbled OCR sample from 0000737553 TapScanner PDF.
SCAN_OCR = """
Dt STOCKIST - Bhure lalpahariya
3 |Materialname Mrp Secondaryr Opening_bal Primary_qty Closing_bal_aty
4 |BONNISANDROPS30mi | 8 S19] OY 75
5 |LVL52DSSYRUP100mi | S225] 5.96] SO CC 40
"""

FIXTURE = Path(
    "/var/www/html/0000737553_2026_08_ZA_24_611_06092026050958.pdf"
)


class TestZlOpeningBalScan(unittest.TestCase):
    def test_gate_accepts_ocr_garbled_headers(self):
        self.assertTrue(_is_zl_opening_bal_scan_text(SCAN_OCR))
        self.assertFalse(
            _is_zl_opening_bal_scan_text(
                "Sales & Stock Statement Op.Bal Receipt Issue Closing\n"
            )
        )
        self.assertFalse(
            _is_zl_opening_bal_scan_text(
                "Stock Statement (Date Wise) ProductName OStock PurQty SaleQty\n"
            )
        )

    def test_live_bhure_scan_pdf(self):
        if not FIXTURE.exists():
            self.skipTest("fixture PDF not present")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertEqual(extra.get("extraction_method"), "zl_opening_bal_scan_vision")
        self.assertNotEqual(extra.get("gemini_fallback"), "validation_failed")
        self.assertNotIn(
            "stock_identity_failure",
            (extra.get("extraction_quality") or {}).get("reasons") or [],
        )
        stockist = str(result.get("stockist_name") or "")
        self.assertRegex(stockist, re.compile(r"Bhure", re.I))
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 6)
        self.assertLessEqual(len(items), 12)
        by_name = {
            str(i.get("product_name") or "").upper(): i
            for i in items
            if isinstance(i, dict)
        }
        # Match by substring — Vision may normalize punctuation.
        bonni = next(
            (i for k, i in by_name.items() if "BONNISAN" in k),
            None,
        )
        self.assertIsNotNone(bonni)
        self.assertEqual(bonni["opening_qty"], 80.0)
        self.assertEqual(bonni["receipts_qty"], 0.0)
        self.assertEqual(bonni["closing_qty"], 75.0)
        self.assertEqual(bonni["sales_qty"], 0.0)
        platenza = next(
            (i for k, i in by_name.items() if "PLATENZA" in k and "TABLET" in k),
            None,
        )
        self.assertIsNotNone(platenza)
        self.assertEqual(platenza["opening_qty"], 155.0)
        self.assertEqual(platenza["closing_qty"], 55.0)
        self.assertEqual(result.get("period_from"), "2026-08-01")
        self.assertEqual(result.get("period_to"), "2026-08-31")
        self.assertGreater(_to_float_safe(result.get("totals", {}).get("closing_value")), 0)


def _to_float_safe(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


if __name__ == "__main__":
    unittest.main()
