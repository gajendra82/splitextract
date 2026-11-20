"""OStock/PurQty/SaleQty Stock Statement (Date Wise) — Jalaram-style PDF."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_ostock_purqty_datewise_statement,
    _parse_ostock_purqty_datewise_statement,
    extract_sales_statement,
)

FIXTURE = Path(__file__).resolve().parents[1] / "test_pdfs" / "jalaram_ostock_purqty_datewise.pdf"


@unittest.skipUnless(FIXTURE.exists(), "fixture PDF missing")
class OstockPurqtyDatewiseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import fitz

        cls.doc = fitz.open(FIXTURE)
        cls.text = "\n".join(page.get_text("text") or "" for page in cls.doc)
        cls.parsed = _parse_ostock_purqty_datewise_statement(cls.doc, FIXTURE.name)

    def test_detector_matches_ostock_layout(self):
        self.assertTrue(_is_ostock_purqty_datewise_statement(self.text))
        # Must not steal classic PROMPT OpStk/ClStk datewise.
        self.assertFalse(
            _is_ostock_purqty_datewise_statement(
                "Stock Statement (Datewise)\nOpStk Pur Sales ClStk"
            )
        )

    def test_parser_stockist_period_and_counts(self):
        self.assertIsNotNone(self.parsed)
        self.assertEqual(self.parsed.get("stockist_name"), "JALARAM AGENCIES")
        self.assertEqual(self.parsed.get("period_from"), "2026-08-01")
        self.assertEqual(self.parsed.get("period_to"), "2026-08-31")
        items = self.parsed.get("line_items") or []
        self.assertEqual(len(items), 48)
        self.assertEqual(
            self.parsed["totals"]["extra"].get("extraction_method"),
            "ostock_purqty_datewise_layout",
        )
        self.assertAlmostEqual(
            sum(float(i["opening_qty"]) for i in items), 2506.0, places=2
        )
        self.assertAlmostEqual(
            sum(float(i["sales_qty"]) for i in items), 1682.0, places=2
        )
        self.assertAlmostEqual(
            sum(float(i["closing_qty"]) for i in items), 2473.0, places=2
        )
        self.assertAlmostEqual(
            sum(float(i["closing_value"]) for i in items), 316677.25, places=2
        )

    def test_product_names_not_prefixed_with_serials(self):
        items = self.parsed.get("line_items") or []
        names = [str(i.get("product_name") or "") for i in items]
        self.assertIn("ARJUNA TAB", names)
        self.assertTrue(any(n.startswith("LIV 52 DS TAB") for n in names))
        self.assertTrue(any(n.startswith("LIV 52 DS SYP") for n in names))
        self.assertFalse(any(n.startswith(("32 ", "33 ", "oi ")) for n in names))
        liv = next(i for i in items if str(i.get("product_name")).startswith("LIV 52 DS TAB"))
        self.assertEqual(liv.get("opening_qty"), 93.0)
        self.assertEqual(liv.get("sales_qty"), 32.0)
        self.assertEqual(liv.get("closing_qty"), 161.0)

    def test_extract_sales_statement_uses_named_parser(self):
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "ostock_purqty_datewise_layout")
        self.assertEqual(result.get("stockist_name"), "JALARAM AGENCIES")
        self.assertNotEqual(result.get("stockist_name"), "ProductName")
        self.assertEqual(len(result.get("line_items") or []), 48)
        self.assertNotEqual(extra.get("gemini_fallback"), True)


if __name__ == "__main__":
    unittest.main()
