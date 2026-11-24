"""STOCK REPORT: bare To period, batch-type I not packing, shopping address."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import _extract_report_period_from_content
from services.stock_header_resolver import _peel_pack_from_qty_texts, _stash_peeled_pack
from services.stock_native_resolver import (
    _LETTERHEAD_ADDRESS_RE,
    _apply_pdf_words_letterhead_meta,
    _looks_like_division_banner,
)


class StockReportBarePeriodPackITests(unittest.TestCase):
    def test_bare_to_period_under_stock_report(self):
        text = (
            "SHIVAM DRUG ENTERPRISES\n"
            "SHANTI SHOPING COMPLEX\n"
            "STOCK REPORT\n"
            "01/09/26 To :30/09/26\n"
            "Particular Op.Qnt. Pur.Qnt. Sl.Qnt. Sl.Value Cl.Qnt. Cl.Value\n"
        )
        pf, pt, source = _extract_report_period_from_content(text)
        self.assertEqual(pf, "2026-09-01")
        self.assertEqual(pt, "2026-09-30")
        self.assertEqual(source, "report_stock_report_bare_to")

    def test_bare_to_ignored_without_stock_report_title(self):
        text = (
            "Some Agency\n"
            "01/09/26 To :30/09/26\n"
            "Particular Op.Qnt. Sl.Qnt.\n"
        )
        pf, pt, source = _extract_report_period_from_content(text)
        self.assertIsNone(pf)
        self.assertIsNone(pt)
        self.assertIsNone(source)

    def test_batch_type_i_not_peeled_as_pack(self):
        packs, rest = _peel_pack_from_qty_texts(["304.00", "I"])
        self.assertEqual(packs, [])
        self.assertEqual(rest, ["304.00"])
        fields: dict = {}
        _stash_peeled_pack(fields, ["I"])
        self.assertIsNone(fields.get("pack"))

    def test_real_pack_still_peeled(self):
        packs, rest = _peel_pack_from_qty_texts(["30GM", "47.00"])
        self.assertEqual(packs, ["30GM"])
        self.assertEqual(rest, ["47.00"])

    def test_shopping_complex_is_address_not_company(self):
        self.assertTrue(_LETTERHEAD_ADDRESS_RE.search("SHANTI SHOPING COMPLEX"))
        self.assertFalse(_looks_like_division_banner("SHANTI SHOPING COMPLEX"))
        result = {
            "stockist_name": "SHIVAM DRUG ENTERPRISES",
            "company_name": None,
            "stockist_address": None,
            "totals": {"extra": {}},
        }
        peek = (
            "SHIVAM DRUG ENTERPRISES\n"
            "SHANTI SHOPING COMPLEX\n"
            "M.P.D ROAD\n"
            "BHAGALPUR-812002\n"
            "BHAGALPUR\n"
            "STOCK REPORT\n"
            "Particular Op.Qnt.\n"
        )
        self.assertTrue(_apply_pdf_words_letterhead_meta(result, peek))
        self.assertNotEqual(
            str(result.get("company_name") or "").upper(), "STOCK REPORT"
        )
        self.assertIsNone(result.get("company_name"))
        addr = str(result.get("stockist_address") or "")
        self.assertIn("SHANTI SHOPING COMPLEX", addr)
        self.assertIn("M.P.D ROAD", addr)


if __name__ == "__main__":
    unittest.main()
