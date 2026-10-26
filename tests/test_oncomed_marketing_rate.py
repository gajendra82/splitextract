"""ONCOMED MARKETING: Qty/Rate vs SGST Value / Amount column correction."""
import unittest

from app import (
    extract_oncomed_marketing_rates_from_ocr,
    fix_oncomed_marketing_rate_from_ocr,
    ocr_suggests_oncomed_marketing,
)


ONCOMED_OCR = """
ONCOMED MARKETING
TAX INVOICE OM002659
1 DAPAGLYN L 10/5 MG TAB 10'S 20 EMV260730AZYDUS 07/27 192.84 120.00 0.00 2.50 60.00 2.50 60.00 2400.00
"""

OCR_AMOUNT_AS_RATE = """
ONCOMED MARKETING
1 30049094 DERIPHYLLIN RETARD 150 MG. 11/25 1X30 T 4 IA01714A GERMA 10/28 41.85 23.61 0.00 2.50 2.36 2.50 2.36 94.44
2 30049072 DEFIN RETARD TAB 4/26 1*15 20 IB00533B ZYDUS 3/28 63.98 48.26 0.00 2.50 24.13 2.50 24.13 965.20
"""

OCR_SGST_AS_RATE = """
ONCOMED MARKETING
1 21069099 DECAM-V TAB. 1X10 T 30 PN-1708 3D LI 5/27 712.00 400.00 0.00 2.50 300.00 2.50 300.00 12000.00
2 30049099 THROMBOPHOB OINTMENT 4/26 20GM 20 IB00929A ZYDUS 3/29 236.80 133.32 0.00 2.50 66.66 2.50 66.66 2666.40
"""


class TestOncomedMarketingRate(unittest.TestCase):
    def test_detector(self):
        self.assertTrue(ocr_suggests_oncomed_marketing(ONCOMED_OCR, ""))
        self.assertTrue(
            ocr_suggests_oncomed_marketing("", "OncoMed MARKETING"))
        self.assertFalse(ocr_suggests_oncomed_marketing("OTHER VENDOR", ""))

    def test_parse_prefers_amount_over_noisy_rate(self):
        noisy = ONCOMED_OCR.replace("120.00", "120.09")
        rows = extract_oncomed_marketing_rates_from_ocr(noisy)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["unit_price"], 120.0)
        self.assertEqual(rows[0]["total_amount"], 2400.0)
        self.assertEqual(rows[0]["quantity"], 20.0)

    def test_fix_sgst_value_mapped_as_rate(self):
        items = [{
            "product_description": "DAPAGLYN 5MG TAB*H",
            "quantity": "20",
            "unit_price": "60.00",
            "total_amount": "1200.00",
            "lot_batch_number": "EMV260730AZYDUS",
            "additional_fields": {},
        }]
        out = fix_oncomed_marketing_rate_from_ocr(
            items,
            ONCOMED_OCR,
            ONCOMED_OCR,
            "OncoMed MARKETING",
            {"total": "2520.00", "tax": "120.00", "vendor": "OncoMed MARKETING"},
        )
        self.assertEqual(out[0]["unit_price"], "120.00")
        self.assertEqual(out[0]["total_amount"], "2400.00")

    def test_fallback_without_table_ocr(self):
        items = [{
            "product_description": "DAPAGLYN 5MG TAB*H",
            "quantity": "20",
            "unit_price": "60.00",
            "total_amount": "1200.00",
            "lot_batch_number": "",
            "additional_fields": {},
        }]
        out = fix_oncomed_marketing_rate_from_ocr(
            items,
            "ONCOMED MARKETING OM002659",
            "",
            "OncoMed MARKETING",
            {"total": "2520.00", "tax": "120.00"},
        )
        self.assertEqual(out[0]["unit_price"], "120.00")
        self.assertEqual(out[0]["total_amount"], "2400.00")

    def test_fix_amount_mapped_as_rate_and_qty_one(self):
        items = [
            {
                "product_description": "DERIPHYLLIN RETARD 150 MG.",
                "quantity": "1",
                "unit_price": "94.44",
                "total_amount": "94.44",
                "lot_batch_number": "IA01714A",
                "additional_fields": {},
            },
            {
                "product_description": "DEFIN RETARD TAB",
                "quantity": "1",
                "unit_price": "965.20",
                "total_amount": "965.20",
                "lot_batch_number": "IB00533B",
                "additional_fields": {},
            },
        ]
        out = fix_oncomed_marketing_rate_from_ocr(
            items,
            OCR_AMOUNT_AS_RATE,
            OCR_AMOUNT_AS_RATE,
            "ONCOMED MARKETING",
            {"total": "1113.62", "tax": "53.03"},
        )
        self.assertEqual(out[0]["quantity"], "4")
        self.assertEqual(out[0]["unit_price"], "23.61")
        self.assertEqual(out[1]["quantity"], "20")
        self.assertEqual(out[1]["unit_price"], "48.26")

    def test_fix_sgst_mapped_as_rate_with_wrong_qty(self):
        items = [
            {
                "product_description": "DECAM-V TAB.",
                "quantity": "40",
                "unit_price": "300.00",
                "total_amount": "12000.00",
                "lot_batch_number": "PN-1708",
                "additional_fields": {},
            },
            {
                "product_description": "THROMBOPHOB OINT",
                "quantity": "40",
                "unit_price": "66.66",
                "total_amount": "2666.40",
                "lot_batch_number": "IB00929A",
                "additional_fields": {},
            },
        ]
        out = fix_oncomed_marketing_rate_from_ocr(
            items,
            OCR_SGST_AS_RATE,
            OCR_SGST_AS_RATE,
            "Oncomed Marketing",
            {"total": "15400.00", "tax": "733.32"},
        )
        self.assertEqual(out[0]["quantity"], "30")
        self.assertEqual(out[0]["unit_price"], "400.00")
        self.assertEqual(out[1]["quantity"], "20")
        self.assertEqual(out[1]["unit_price"], "133.32")

    def test_parse_ignores_pack_as_qty(self):
        rows = extract_oncomed_marketing_rates_from_ocr(OCR_AMOUNT_AS_RATE)
        by_batch = {r["lot_batch_number"]: r for r in rows}
        self.assertEqual(by_batch["IB00533B"]["quantity"], 20.0)
        self.assertEqual(by_batch["IB00533B"]["unit_price"], 48.26)


if __name__ == "__main__":
    unittest.main()
