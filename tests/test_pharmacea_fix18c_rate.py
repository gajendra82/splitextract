"""Pharmacea Link FIX18c: do not divide correct Unit Price by qty."""
import unittest

from app import (
    enforce_schema,
    _extract_line_items_for_validation,
    fix_pharmacea_link_product_names_from_ocr,
)


GOODS_OCR_2832 = (
    "Pharmacea Link\n"
    "Document No. : 2832/26-27\n"
    "SINo | Item Description |HSN Code |Quantity |Unit |Unit Price(Rs) "
    "|Discount(Rs) | Taxable Amount(Rs) Tax Rate Total\n"
    "1 | Pantodac 20 Tab |30049039 |180.0 |NOS | 118.42 | 639.47 | 20676.13 |5.00 + 0.00| 21709.93\n"
    "2 | Zycel 100 Cap |30049069 |100.0 |NOS | 127.65 | 382.95 | 12382.05 |5.00 + 0.00| 13001.15\n"
)


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

    def test_discounted_unit_price_not_replaced_with_taxable_over_qty(self):
        """Vision Unit Price 118.42 must not become Taxable/qty=114.87 when Discount(Rs) missing."""
        payload = _pharmacea_payload([
            {
                "product_description": "Pantodac Tab",
                "hsn_code": "30049039",
                "quantity": "180.0",
                "unit_price": "118.42",
                "total_amount": "21709.93",
                "additional_fields": {
                    "gross_amount": "20676.13",
                    "discount_percentage": "0.0",
                },
            },
            {
                "product_description": "Zycel Cap",
                "hsn_code": "30049069",
                "quantity": "100.0",
                "unit_price": "127.65",
                "total_amount": "13001.15",
                "additional_fields": {
                    "gross_amount": "12382.05",
                    "discount_percentage": "0.0",
                },
            },
        ])
        payload["data"]["ocr_text"] = GOODS_OCR_2832
        out = _extract_line_items_for_validation(enforce_schema(payload))
        by_name = {it["product_description"]: it for it in out}
        self.assertIn("Pantodac 20 Tab", by_name)
        self.assertIn("Zycel 100 Cap", by_name)
        self.assertEqual(by_name["Pantodac 20 Tab"]["unit_price"], "118.42")
        self.assertEqual(by_name["Zycel 100 Cap"]["unit_price"], "127.65")
        self.assertAlmostEqual(float(by_name["Pantodac 20 Tab"]["quantity"]), 180.0)
        self.assertAlmostEqual(float(by_name["Zycel 100 Cap"]["quantity"]), 100.0)

    def test_restores_unit_price_from_nos_column_when_net_rate_extracted(self):
        items = [
            {
                "product_description": "Zycel Cap",
                "hsn_code": "30049069",
                "quantity": "100.0",
                "unit_price": "123.82",
                "total_amount": "13001.15",
                "additional_fields": {
                    "gross_amount": "12382.05",
                    "discount_percentage": "0.0",
                },
            }
        ]
        out = fix_pharmacea_link_product_names_from_ocr(items, GOODS_OCR_2832)
        self.assertEqual(out[0]["product_description"], "Zycel 100 Cap")
        self.assertEqual(out[0]["unit_price"], "127.65")

    def test_does_not_swap_letter_tokens_from_garbled_ocr(self):
        items = [{
            "product_description": "Dexona Inj",
            "quantity": "100.0",
            "unit_price": "7.98",
            "total_amount": "837.9",
        }]
        ocr = (
            "Pharmacea Link\n"
            "Dexana Inj |300490 |100.0 |NOS | 7.98\n"
        )
        out = fix_pharmacea_link_product_names_from_ocr(items, ocr)
        self.assertEqual(out[0]["product_description"], "Dexona Inj")
        self.assertEqual(out[0]["unit_price"], "7.98")

    def test_ferinject_unit_price_not_divided_as_taxable(self):
        items = [{
            "product_description": "Ferinject 500mg Inj 10ml",
            "hsn_code": "30045090",
            "quantity": "10.0",
            "unit_price": "1500.0",
            "total_amount": "15000.0",
            "additional_fields": {},
        }]
        out = _extract_line_items_for_validation(
            enforce_schema(_pharmacea_payload(items))
        )
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(float(out[0]["unit_price"]), 1500.0, places=2)


if __name__ == "__main__":
    unittest.main()
