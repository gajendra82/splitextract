"""Pharmacea Link FIX18c: do not divide correct Unit Price by qty."""
import unittest

from app import enforce_schema, _extract_line_items_for_validation


def _pharmacea_payload(items):
    return {
        "data": {
            "invoice_summary": {
                "vendor": "Pharmaceä Link",
                "customer": "Ruby Hall Clinic",
                "invoice_no": "2779/26-27",
            },
            "line_items": {"items": items, "count": len(items)},
            "ocr_text": (
                "Pharmacea Link\n"
                "Document No. : 2779/26-27\n"
                "SINo Item Description HSN Code Quantity Unit Unit Price(Rs) "
                "Discount(Rs) Taxable Amount(Rs) Tax Rate Total\n"
            ),
        }
    }


class TestPharmaceaFix18cRate(unittest.TestCase):
    def test_keeps_correct_unit_price_when_total_is_taxable(self):
        """Vision put Taxable into total_amount; unit_price is already Unit Price."""
        items = [
            {
                "product_description": "Pantodac DSR Cap",
                "hsn_code": "30049039",
                "quantity": "14.0",
                "unit_price": "190.09",
                "total_amount": "2661.26",
                "additional_fields": {"discount_percentage": "0.0"},
            },
            {
                "product_description": "Matilda E R Tab",
                "hsn_code": "30045039",
                "quantity": "20.0",
                "unit_price": "251.24",
                "total_amount": "5024.8",
                "additional_fields": {"discount_percentage": "0.0"},
            },
            {
                "product_description": "Pinom 40 Tab 15s",
                "hsn_code": "30049099",
                "quantity": "7.0",
                "unit_price": "271.81",
                "total_amount": "1902.67",
                "additional_fields": {"discount_percentage": "0.0"},
            },
        ]
        out = _extract_line_items_for_validation(
            enforce_schema(_pharmacea_payload(items))
        )
        by_name = {it["product_description"]: it for it in out}
        self.assertEqual(by_name["Pantodac DSR Cap"]["unit_price"], "190.09")
        self.assertEqual(by_name["Matilda E R Tab"]["unit_price"], "251.24")
        self.assertEqual(by_name["Pinom 40 Tab 15s"]["unit_price"], "271.81")
        # Taxable×1.05 GST uplift
        self.assertEqual(by_name["Pantodac DSR Cap"]["total_amount"], "2794.32")
        self.assertEqual(by_name["Matilda E R Tab"]["total_amount"], "5276.04")
        self.assertEqual(by_name["Pinom 40 Tab 15s"]["total_amount"], "1997.80")

    def test_still_splits_taxable_mapped_as_unit_price(self):
        """Legacy FIX18c: unit_price holds Taxable (≥1000), total = qty×taxable."""
        items = [
            {
                "product_description": "Specialty Inj",
                "hsn_code": "300490",
                "quantity": "20",
                "unit_price": "3415.4",
                "total_amount": "68308",
                "additional_fields": {},
            }
        ]
        out = _extract_line_items_for_validation(
            enforce_schema(_pharmacea_payload(items))
        )
        self.assertEqual(len(out), 1)
        rate = float(out[0]["unit_price"])
        self.assertAlmostEqual(rate, 170.77, places=2)

    def test_aztreo_discount_row_keeps_rate_and_uplifts_total(self):
        items = [
            {
                "product_description": "Aztreo 2Gm Inj",
                "hsn_code": "300420",
                "quantity": "1.0",
                "unit_price": "230.0",
                "total_amount": "220.8",
                "additional_fields": {
                    "gross_amount": "220.8",
                    "discount_percentage": "9.2",
                },
            }
        ]
        out = _extract_line_items_for_validation(
            enforce_schema(_pharmacea_payload(items))
        )
        self.assertEqual(out[0]["unit_price"], "230.0")
        self.assertEqual(out[0]["total_amount"], "231.84")


if __name__ == "__main__":
    unittest.main()
