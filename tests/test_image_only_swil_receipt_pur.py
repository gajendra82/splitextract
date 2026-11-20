"""Image-only Swil Sales & Stock Receipt/Pur (monitor photo PDFs)."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _fix_swil_receipt_photo_party_fields,
    _repair_swil_receipt_total_as_sales_qty,
    _swil_receipt_pur_image_only_ok,
    empty_result,
    extract_sales_statement,
)


PHOTO_PDF = Path(
    r"C:\Users\anuja\Downloads\ZA_2026_August"
    r"\0000737611_2026_08_ZA_25_299_07092026181318.pdf"
)


def _sample_ok_result():
    result = empty_result("photo.pdf", "pdf")
    result["period_from"] = "2026-08-01"
    result["period_to"] = "2026-08-31"
    result["company_name"] = "HIMALAYA WELLNESS COMPANY"
    result["line_items"] = [
        {
            "product_name": "BONNISAN DROPS 30ML",
            "packing": "30ML",
            "opening_qty": 0.0,
            "receipts_qty": 50.0,
            "receipts_value": 3309.30,
            "sales_qty": 20.0,
            "sales_value": 1493.20,
            "closing_qty": 30.0,
            "closing_value": 2084.99,
            "extra": {"receipts_value": 3309.30},
        },
        {
            "product_name": "CLARINA ANTI ACNE CR",
            "packing": "30GMS",
            "opening_qty": 0.0,
            "receipts_qty": 50.0,
            "receipts_value": 6483.86,
            "sales_qty": 3.0,
            "sales_value": 438.84,
            "closing_qty": 47.0,
            "closing_value": 6399.71,
            "extra": {"receipts_value": 6483.86},
        },
        {
            "product_name": "CONFIDO TABLETS",
            "packing": "60'S",
            "opening_qty": 0.0,
            "receipts_qty": 200.0,
            "receipts_value": 31184.06,
            "sales_qty": 0.0,
            "sales_value": 0.0,
            "closing_qty": 200.0,
            "closing_value": 32743.20,
            "extra": {"receipts_value": 31184.06},
        },
    ]
    return result


class TestImageOnlySwilReceiptPur(unittest.TestCase):
    def test_gate_accepts_receipt_values_rejects_bad_year(self):
        ok = _sample_ok_result()
        self.assertTrue(_swil_receipt_pur_image_only_ok(ok))
        bad = _sample_ok_result()
        bad["period_from"] = "2001-08-16"
        self.assertFalse(_swil_receipt_pur_image_only_ok(bad))
        empty = empty_result("x.pdf", "pdf")
        empty["line_items"] = [{"product_name": "X", "closing_qty": 1}]
        self.assertFalse(_swil_receipt_pur_image_only_ok(empty))

    def test_address_not_kept_as_stockist_name(self):
        result = empty_result("x.pdf", "pdf")
        result["stockist_name"] = "DESHBANDHU PARA, ISLAMPUR, U. D."
        result["company_name"] = "HIMALAYA WELLNESS COMPANY"
        fixed = _fix_swil_receipt_photo_party_fields(result)
        self.assertIsNone(fixed.get("stockist_name"))
        self.assertIn("ISLAMPUR", str(fixed.get("stockist_address") or "").upper())

    def test_repairs_total_qty_copied_into_sales(self):
        items = [
            {
                "product_name": "HIORA GA GEL",
                "opening_qty": 0.0,
                "receipts_qty": 50.0,
                "sales_qty": 50.0,
                "sales_value": 0.0,
                "closing_qty": 50.0,
                "extra": {},
            },
            {
                "product_name": "PILEX FORTE",
                "opening_qty": 0.0,
                "receipts_qty": 100.0,
                "sales_qty": 100.0,
                "sales_value": 0.0,
                "closing_qty": 75.0,
                "extra": {},
            },
        ]
        fixed = _repair_swil_receipt_total_as_sales_qty(items)
        self.assertEqual(fixed[0]["sales_qty"], 0.0)
        self.assertEqual(fixed[1]["sales_qty"], 25.0)

    def test_live_photo_pdf_keeps_receipts(self):
        if not PHOTO_PDF.exists():
            self.skipTest("photo PDF fixture not present")
        result = extract_sales_statement(PHOTO_PDF.read_bytes(), PHOTO_PDF.name)
        method = ((result.get("totals") or {}).get("extra") or {}).get(
            "extraction_method"
        )
        self.assertEqual(method, "swil_receipt_pur_value_vision")
        self.assertNotEqual(method, "gemini_extraction_fallback")
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 20)
        self.assertEqual(result.get("period_from"), "2026-08-01")
        self.assertEqual(result.get("period_to"), "2026-08-31")
        by_name = {i["product_name"]: i for i in items}
        # First-page spot check from the printed statement.
        bonn = next(
            (i for i in items if "BONNISAN" in str(i.get("product_name") or "").upper()
             or "BONISAN" in str(i.get("product_name") or "").upper()),
            None,
        )
        self.assertIsNotNone(bonn)
        self.assertEqual(bonn["receipts_qty"], 50.0)
        self.assertEqual(bonn["sales_qty"], 20.0)
        self.assertEqual(bonn["closing_qty"], 30.0)
        self.assertGreater(
            sum(1 for i in items if (i.get("receipts_qty") or 0) > 0),
            10,
        )
        pages_parsed = ((result.get("totals") or {}).get("extra") or {}).get(
            "pages_parsed"
        )
        pages_total = ((result.get("totals") or {}).get("extra") or {}).get(
            "pages_total"
        )
        if pages_total:
            self.assertEqual(pages_parsed, pages_total)
        # Page 2 products (monitor photo continuation).
        self.assertTrue(
            any("HIORA" in str(i.get("product_name") or "").upper() for i in items)
            or any("HIMCOLIN" in str(i.get("product_name") or "").upper() for i in items)
        )
        # Must not leave every opening/receipt/sales at zero.
        self.assertFalse(
            all(
                (i.get("opening_qty") or 0) == 0
                and (i.get("receipts_qty") or 0) == 0
                and (i.get("sales_qty") or 0) == 0
                for i in items
            )
        )


if __name__ == "__main__":
    unittest.main()
