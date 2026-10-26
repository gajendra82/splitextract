"""PRAKASH MEDICAL STORES packing-slip OCR must not add extra products."""
import unittest

from app import (
    ocr_suggests_prakash_medical_stores,
    recover_missing_items_from_ocr,
)


PRAKASH_OCR = """
PRAKASH MEDICAL STORES *26000730013333* PRAKASH MEDICAL STORES
CREDIT COPY
Tax Inv.No. :26000730013333
Invoice Date :11-06-26
GSTIN :22AAJFP7759A1ZA
MFAC Quantity CGST SGST SMan:01 DIRECT CREDIT
O MRld P / Mkt Item Description Pack CH oS dN e Billed Free/ Batch DE ax tp e M.R.P T Pra rid ce e DS ic sh c D %isc Ta Vx aa lub ele Rate Amt Rate Amt Net Value Item Decription Qty Exp
By Repl Dt.
0 ZYM SEMAGLYN PEN0 3ML 90189099 2 B260003 09-30 1250.00 857.00 0.00 0.01714 2.50 42.85 2.50 42.85 1799.70 SEMAGLYN PEN 2 09/30
0 ZYM SEMAGLYN INJECTION0 3ML 30049099 2 BB00028C 07-27 8749.00 5999.00 0.00 0.011998 2.50 299.95 2.50 299.95 12597.90 SEMAGLYN INJECTION 2 07/27
Prep:KAILASHTot Items:2 SchDiscGiven : Tax% Taxable SGST% AMT CGST% AMT OTHERS Sub Total : 13712.00
"""


VISION_ITEMS = [
    {
        "product_description": "SEMAGLYN PEN0",
        "quantity": "2",
        "unit_price": "857.00",
        "total_amount": "1714.00",
        "hsn_code": "90189099",
        "lot_batch_number": "B260003",
        "additional_fields": {
            "mrp": "1250.00",
            "mfg": "ZYM",
            "expiry_date": "2030-09-30",
            "free_quantity": "0",
        },
    },
    {
        "product_description": "SEMAGLYN INJECTION0",
        "quantity": "2",
        "unit_price": "5999.00",
        "total_amount": "11998.00",
        "hsn_code": "30049099",
        "lot_batch_number": "BB00028C",
        "additional_fields": {
            "mrp": "8749.00",
            "mfg": "ZYM",
            "expiry_date": "2027-07-31",
            "free_quantity": "0",
        },
    },
]


class TestPrakashMedicalStoresLineItems(unittest.TestCase):
    def test_detects_prakash_not_other_formats(self):
        self.assertTrue(ocr_suggests_prakash_medical_stores(PRAKASH_OCR))
        self.assertFalse(
            ocr_suggests_prakash_medical_stores(
                "CHANDUKA AGENCIES\nCOMP PARTICULARS N_MRP"
            )
        )
        self.assertFalse(
            ocr_suggests_prakash_medical_stores(
                "SUPREME LIFE SCIENCES\nM.R.P SGST"
            )
        )

    def test_does_not_recover_packing_slip_or_net_value_duplicate(self):
        out = recover_missing_items_from_ocr(
            [dict(item) for item in VISION_ITEMS], PRAKASH_OCR)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["product_description"], "SEMAGLYN PEN0")
        self.assertEqual(out[0]["unit_price"], "857.00")
        self.assertEqual(out[0]["total_amount"], "1714.00")
        self.assertEqual(out[0]["lot_batch_number"], "B260003")
        self.assertEqual(out[1]["product_description"], "SEMAGLYN INJECTION0")
        self.assertEqual(out[1]["unit_price"], "5999.00")
        self.assertEqual(out[1]["total_amount"], "11998.00")
        self.assertEqual(out[1]["lot_batch_number"], "BB00028C")
        self.assertFalse(any(item.get("recovered_from_ocr") for item in out))
        self.assertFalse(
            any("ZYM SEMAGLYN INJ" in str(item.get("product_description", "")).upper()
                for item in out)
        )


if __name__ == "__main__":
    unittest.main()
