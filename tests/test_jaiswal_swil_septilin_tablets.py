"""Sales & Stock Receipt/Pur — keep SEPTILIN SYRUP and SEPTILIN TABLETS as two rows."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _apply_swil_receipt_value_fields,
    _dedupe_swil_receipt_overlap_items,
    _extract_header_driven_stock_photo,
    _peek_suggests_swil_receipt_value,
    empty_result,
)


JAISWAL_PEEK = (
    "JAISWAL AGENCIES (MEDICAL)\n"
    "Page No. 1 Sales & Stock Statement (From 01/08/2026 Upto 27/08/2026)\n"
    "HIMALAYA ZENDRA\n"
    "PRODUCT NAME PACKING Op Qty Opening Bal Value Receipt Qty Receipt/Pur Value "
    "Total Qty Issue Qty Issue/Sales Value Closing Qty Closing Bala Value Near Expiry\n"
)

JAISWAL_OCR_NOISE = (
    "JAISWAL AGENG)!'S (MEDICAL)\n"
    "Page No, | billes & Stock Statemeni(inamd | 04/2026 Upto 27/08/2026) Aug 27,2020\n"
    "HIMALAYA ZENDRA\n"
    "RODUCT NAMI PACKING Op Opening\n"
)

SSA_PEEK = (
    "STOCK & SALES ANALYSIS 01-08-2026 - 31-08-2026\n"
    "ITEM DESCRIPTION OPENING RECEIPT ISSUE CLOSING\n"
)


class TestSwilReceiptSeptilinRows(unittest.TestCase):
    def test_peek_detects_sales_stock_not_ssa_analysis(self):
        self.assertTrue(_peek_suggests_swil_receipt_value(JAISWAL_PEEK))
        self.assertTrue(_peek_suggests_swil_receipt_value(JAISWAL_OCR_NOISE))
        self.assertFalse(_peek_suggests_swil_receipt_value(SSA_PEEK))

    def test_apply_keeps_septilin_syrup_and_tablets(self):
        result = empty_result("jaiswal.jpg", "jpg")
        parsed = {
            "report_title": "Sales & Stock Statement",
            "stockist_name": "JAISWAL AGENCIES (MEDICAL)",
            "company_name": "HIMALAYA ZENDRA",
            "line_items": [
                {
                    "product_name": "SEPTILIN SYRUP 200M",
                    "packing": "200ML",
                    "opening_qty": 56,
                    "opening_value": 6825.84,
                    "receipts_qty": 0,
                    "receipts_value": 0.0,
                    "sales_qty": 11,
                    "sales_value": 1340.79,
                    "closing_qty": 45,
                    "closing_value": 5485.05,
                    "extra": {"total_stock": 56},
                },
                {
                    "product_name": "SEPTILIN TABLETS",
                    "packing": "60TAB",
                    "opening_qty": 88,
                    "opening_value": 11798.16,
                    "receipts_qty": 0,
                    "receipts_value": 0.0,
                    "sales_qty": 7,
                    "sales_value": 898.89,
                    "closing_qty": 81,
                    "closing_value": 10899.27,
                    "extra": {"total_stock": 88},
                },
                {
                    "product_name": "TALEKT TABLETS",
                    "packing": "60'S",
                    "opening_qty": 0,
                    "opening_value": 0.0,
                    "receipts_qty": 50,
                    "receipts_value": 4092.01,
                    "sales_qty": 0,
                    "sales_value": 0.0,
                    "closing_qty": 50,
                    "closing_value": 4092.01,
                    "extra": {"total_stock": 50},
                },
            ],
            "totals": {"sales_value": 22397.31, "closing_value": 131314.86},
        }
        applied = _apply_swil_receipt_value_fields(result, parsed)
        names = [str(i.get("product_name") or "").upper() for i in applied["line_items"]]
        self.assertTrue(any("SEPTILIN SYRUP" in n for n in names))
        self.assertTrue(any("SEPTILIN TABLET" in n for n in names))
        syrup = next(
            i
            for i in applied["line_items"]
            if "SYRUP" in str(i.get("product_name") or "").upper()
        )
        tabs = next(
            i
            for i in applied["line_items"]
            if "TABLET" in str(i.get("product_name") or "").upper()
            and "SEPTILIN" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(syrup["opening_qty"], 56.0)
        self.assertEqual(syrup["closing_qty"], 45.0)
        self.assertEqual(tabs["opening_qty"], 88.0)
        self.assertEqual(tabs["closing_qty"], 81.0)
        self.assertEqual(
            applied["totals"]["extra"]["extraction_method"],
            "swil_receipt_pur_value_vision",
        )

    def test_dedupe_does_not_collapse_septilin_pair(self):
        items = [
            {
                "product_name": "SEPTILIN SYRUP 200M",
                "packing": "200ML",
                "opening_qty": 56.0,
                "receipts_qty": 0.0,
                "sales_qty": 11.0,
                "closing_qty": 45.0,
                "closing_value": 5485.05,
                "extra": {"receipts_value": 0.0},
            },
            {
                "product_name": "SEPTILIN TABLETS",
                "packing": "60TAB",
                "opening_qty": 88.0,
                "receipts_qty": 0.0,
                "sales_qty": 7.0,
                "closing_qty": 81.0,
                "closing_value": 10899.27,
                "extra": {"receipts_value": 0.0},
            },
        ]
        kept = _dedupe_swil_receipt_overlap_items(items)
        self.assertEqual(len(kept), 2)

    def test_header_driven_defers_sales_stock_to_swil(self):
        from unittest import mock

        swil = {
            "line_items": [
                {
                    "product_name": "SEPTILIN SYRUP",
                    "packing": "200ML",
                    "opening_qty": 56.0,
                    "closing_qty": 45.0,
                },
                {
                    "product_name": "SEPTILIN TABLETS",
                    "packing": "60TAB",
                    "opening_qty": 88.0,
                    "closing_qty": 81.0,
                },
            ],
            "totals": {
                "extra": {"extraction_method": "swil_receipt_pur_value_vision"}
            },
        }
        with mock.patch(
            "services.sales_statement_extractor._ocr_image_to_text",
            return_value=JAISWAL_OCR_NOISE,
        ), mock.patch(
            "services.sales_statement_extractor._extract_swil_receipt_value_vision",
            return_value=swil,
        ):
            out = _extract_header_driven_stock_photo(b"img", "jaiswal.jpg", ".jpg")
        self.assertIsNotNone(out)
        names = [str(i.get("product_name") or "").upper() for i in out["line_items"]]
        self.assertTrue(any("SEPTILIN SYRUP" in n for n in names))
        self.assertTrue(any("SEPTILIN TABLET" in n for n in names))
        self.assertEqual(
            out["totals"]["extra"]["extraction_method"],
            "swil_receipt_pur_value_vision",
        )


if __name__ == "__main__":
    unittest.main()
