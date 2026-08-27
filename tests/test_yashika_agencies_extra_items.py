"""YASHIKA AGENCIES: GST footer pipe-table must not become an extra product."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _extract_line_items_for_validation,
    drop_yashika_agencies_extra_items,
    enforce_schema,
    ocr_suggests_yashika_agencies,
    recover_missing_items_from_ocr,
)

YASHIKA_OCR = """
YASHIKA AGENCIES YASHIKA DISTRIBUTORS
TAX INVOICE-CREDIT Bill No : 31364
RACK MFR HSN PRODUCT NAME PACK BATCH EXDT QTY FREE PD% MRP SRATE GST% VALUE
C ZYDU 30049079 ASP ATORVA 75 CAPS 10CAP (1 26S2GCA095 07/27 50.00 0.00 55.32 34.22 5 1711.00
C ZYDU 30049099 ATORVA E 10 TAB 10'S (1) SB00193A 02/28 30.00 0.00 416.42 235.41 5 7062.30
C142 ZYDU 30049099 VOCID 20 TAB 10'S (1) EMV260937B 03/28 50.00 0.00 175.31 106.85 5 5342.50
C175 ZYDU 30049099 BEMDAC EZ 10 (1) ANC06AAB 02/28 30.00 0.00 373.03 239.16 5 7174.80
T.ITEMS 12 TOTAL 189977.90
 |  |  |  |  | SUB TOTAL | 189977.9 | 0.00 | 0.00 | 0.00 |  |  | 0.00 |  | T.ITEMS |  | 12 |  |  | GST | 9498.90 |  | TRANS |  |  |  | GST | 9498.90
LIFE SCAN PRODUCTS NO EXPIRY AND RETURNS
@RELIABLE SOFTWARE,GOA WWW.RELYSOFT.IN Page No 1/1
"""

YASH_AGENCIES_OCR = """
YASH AGENCIES TAX INVOICE
TaxInvNo : YE03417
PRODUCT NAME PACK HSNCODE BATCH EXP QTY FREE OLDMRP M.R.P RATE DIS% AMOUNT GST%
"""

YASHIKA_ITEMS = [
    {
        "product_description": "ASP ATORVA 75 CAPS",
        "lot_batch_number": "26S2GCA095",
        "quantity": "50.00",
        "unit_price": "34.22",
        "total_amount": "1711.00",
    },
    {
        "product_description": "ATORVA E 10 TAB",
        "lot_batch_number": "SB00193A",
        "quantity": "30.00",
        "unit_price": "235.41",
        "total_amount": "7062.30",
    },
    {
        "product_description": "VOCID 20 TAB",
        "lot_batch_number": "EMV260937B",
        "quantity": "50.00",
        "unit_price": "106.85",
        "total_amount": "5342.50",
    },
    {
        "product_description": "BEMDAC EZ",
        "lot_batch_number": "ANC06AAB",
        "quantity": "30.00",
        "unit_price": "239.16",
        "total_amount": "7174.80",
    },
]


class TestYashikaAgenciesExtraItems(unittest.TestCase):
    def test_detects_yashika_not_yash_agencies(self):
        self.assertTrue(ocr_suggests_yashika_agencies(YASHIKA_OCR))
        self.assertTrue(
            ocr_suggests_yashika_agencies(YASHIKA_OCR, "YASHIKA AGENCIES"))
        self.assertFalse(ocr_suggests_yashika_agencies(YASH_AGENCIES_OCR))
        self.assertFalse(ocr_suggests_yashika_agencies("JACKSON MEDICALS"))

    def test_recovery_does_not_add_sub_total(self):
        out = recover_missing_items_from_ocr(
            [dict(x) for x in YASHIKA_ITEMS], YASHIKA_OCR)
        names = [i["product_description"].upper() for i in out]
        self.assertEqual(len(out), 4)
        self.assertFalse(any("SUB TOTAL" in n or n == "SUBTOTAL" for n in names))
        self.assertFalse(any("LIFE SCAN" in n for n in names))
        self.assertEqual(names.count("BEMDAC EZ"), 1)

    def test_drops_footer_sub_total_row(self):
        items = [dict(x) for x in YASHIKA_ITEMS] + [{
            "product_description": "SUB TOTAL",
            "quantity": "12",
            "unit_price": "0.00",
            "total_amount": "189977.90",
            "lot_batch_number": "TRANS",
            "recovered_from_ocr": True,
        }]
        out = drop_yashika_agencies_extra_items(
            items, YASHIKA_OCR, "YASHIKA AGENCIES")
        names = [i["product_description"].upper() for i in out]
        self.assertEqual(len(out), 4)
        self.assertNotIn("SUB TOTAL", names)
        self.assertIn("BEMDAC EZ", names)

    def test_does_not_drop_other_formats(self):
        items = [{
            "product_description": "SUB TOTAL",
            "quantity": "12",
            "unit_price": "0.00",
            "total_amount": "189977.90",
        }]
        out = drop_yashika_agencies_extra_items(items, YASH_AGENCIES_OCR)
        self.assertEqual(out[0]["product_description"], "SUB TOTAL")

    def test_enforce_schema_keeps_twelve_style_products_drops_footer(self):
        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "YASHIKA AGENCIES",
                    "customer": "YASHIKA DISTRIBUTORS",
                    "invoice_no": "31364",
                    "total": "199477.00",
                },
                "line_items": {
                    "items": [dict(x) for x in YASHIKA_ITEMS] + [{
                        "product_description": "SUB TOTAL",
                        "quantity": "12",
                        "unit_price": "0.00",
                        "total_amount": "189977.90",
                        "lot_batch_number": "TRANS",
                        "recovered_from_ocr": True,
                    }],
                    "count": 5,
                },
                "ocr_text": YASHIKA_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        names = [i["product_description"].upper() for i in out]
        self.assertEqual(len(out), 4)
        self.assertFalse(any("SUB TOTAL" in n for n in names))
        self.assertEqual(names.count("ASP ATORVA 75 CAPS"), 1)
        self.assertEqual(names.count("BEMDAC EZ"), 1)


if __name__ == "__main__":
    unittest.main()
