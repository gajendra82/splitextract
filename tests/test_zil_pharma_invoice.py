"""Zil Pharma Enterprises: vendor/GSTIN, S/ invoice#, tax, false-row filter."""
import unittest

from app import (
    _parse_zil_pharma_ocr_rows,
    _zil_pharma_extract_tax_and_total,
    _zil_pharma_normalize_invoice_no,
    fix_zil_pharma_line_items_from_ocr,
    fix_zil_pharma_summary_from_ocr,
    ocr_suggests_zil_pharma,
)

# Representative OCR (from JULY 2026 $/2682 + $/3159 + S/2808 pipelines).
OCR_2682 = """
TAX INVOICE ORIGINAL FOR RECIPIENT
Zil Pharma Enterprises Pvt. Ltd. | HOLY FAMILY HOSPITAL Invoice No: $/2682
BASEMENT, MERCHANT CHAMBER, 41, NEW MARINELINES
GSTIN : 27AABCZ9702A1ZH / PAN : AABCZ9702A GSTIN : 27AAATT0530C1Z1 [ REGISTER ]
Web : www.zilpharma.in E-mail : zilpharma31@gmail.com
SHIPPED TO HOLY FAMILY HOSPITAL GSTIN : 27AAATT0530C1Z1 PAN: AAATT0530C
HSN/SAC LOC MFGR PACK. PRODUCT DESCRIPTION QTY FREE BATCH NO. MFG. EXP. DISC% GST% MRP RATE AMOUNT
30049039 67 ZYDU 1X15TA PANTODAC - 40MG TAB (NEW) 60 1800409C 03/26 02/29 0.00 5.0 236.16 150.00 9000.00
30049069 194 INDO 1X15TA FEBREX PLUS TAB 100 26070008 01/26 12/28 0.00 5.0 111.00 84.57 8457.00
30044090 197 INDO 100ML CITAL LIQUID 100ML SUGAR FREE 15 26400034 02/26 01/29 0.00 5.0 131.00 99.81 1497.15
MRP AMT : 29002.40 | IRN NO : 1c74d78789e9a0a0f95b2408d691502509ec5013623490a23dc9004742653b1c
STANDARD CHARTERED BANK AMOUNT SCHAMT TDAMT CD AMT TAXABLE CGST CGSTAMT SGST SGSTAMT TOTAL AMT
AIC NO: 22506210770 20301.00 199.63 0.00 0.00 20101.37 2.5% 502.55 2.5% 502.55 21106.47
Cases : 0 20301.00 199.63 0.00 0.00 20101.37 502.55 502.55 21106.47
For ZIL PHARMA ENTERPRISES PVT LTD Total Amt : 21106.47
T. Prod: 5 T.QTY: 190 TOPAY % 21106.00
"""

OCR_3159 = """
Zil Pharma Enterprises Pvt, Ltd, Invoice No : $/3159
HOLY FAMILY HOSPITAL Inv Date: 18/07/2026
GSTIN : 27AABCZ9702A1ZH / PAN : AABCZ9702A GSTIN : 27AAATT0530C1Z1 [ REGISTER ]
SHIPPED TO HOLY FAMILY HOSPITAL GSTIN : 27AAATT0530C1Z1
HSN/SAC LOC MFGR PACK. PRODUCT DESCRIPTION QTY FREE BATCH NO. MFG. EXP. DISC% GST% MRP RATE AMOUNT
30049099 ELAN 1X15CA RINIFOL CAP (15 CAP) 30 REC6C017 03/26 05/27 0.00 5.0 91.88 70.00 2100.00
30049039 67 ZYDU 1X15TA PANTODAC - 40MG TAB (NEW) 300 1B00409C 03/26 02/29 0.00 5.0 236.16 150.00 45000.00
MRP AMT : 73604.40
47100.00 0.00 0.00 0.00 47100.00 2.5% 1177.50 2.5% 1177.50 49455.00
Cases : 0 47100.00 0.00 0.00 0.00 47100.00 1177.50 1177.50 49455.00
For ZIL PHARMA ENTERPRISES PVT LTD Total Amt : 49455.00
T. Prod: 2 T.QTY: 330 TOPAY % 49455.00
"""

OCR_2808 = """
Zil Pharma Enterprises Pvt. Ltd. | SAIFEE HOSPITAL Invoice No : $/2808
GSTIN : 27AABCZ9702A1ZH / PAN : AABCZ9702A GSTIN : 27AABTS7800D1Z0 [ REGISTER ]
SHIPPED TO SAIFEE HOSPITAL GSTIN : 27AABTS7800D1Z0
POST BOX NO. 3771, 15/17,, MAHARSHI KARVE RD
HSN/SAC LOC MFGR PACK. PRODUCT DESCRIPTION QTY FREE BATCH NO. MFG. EXP. DISC% GST% MRP RATE AMOUNT
30049039 67 ZYDU 1X15TA PANTODAC - 40MG TAB (NEW) 120 1B00409C 03/26 02/29 0.00 5.0 236.16 137.00 16440.00
16440.00 0.00 0.00 0.00 16440.00 2.5% 411.00 2.5% 411.00 17262.00
For ZIL PHARMA ENTERPRISES PVT LTD Total Amt : 17262.00
T. Prod: 1 T.QTY: 120 TOPAY 17262.00
"""


class TestZilPharmaInvoice(unittest.TestCase):
    def test_detects_zil_pharma(self):
        self.assertTrue(ocr_suggests_zil_pharma(OCR_2682))
        self.assertTrue(ocr_suggests_zil_pharma(OCR_3159))
        self.assertFalse(ocr_suggests_zil_pharma("OTHER VENDOR TAX INVOICE"))
        self.assertFalse(
            ocr_suggests_zil_pharma("RIDDHISHREE HEALTHCARE Inv No : RSB2627/091")
        )

    def test_normalize_invoice_no_dollar_to_s(self):
        self.assertEqual(_zil_pharma_normalize_invoice_no("$/2682"), "S/2682")
        self.assertEqual(_zil_pharma_normalize_invoice_no("S/3159"), "S/3159")
        self.assertEqual(_zil_pharma_normalize_invoice_no("$/3302 Z"), "S/3302")

    def test_summary_vendor_gstin_invoice_tax(self):
        summary = {
            "vendor": "None",
            "vendor_gstin": "",
            "customer": "HOLY FAMILY HOSPITAL",
            "customer_gstin": "27AABCZ9702A1ZH",  # wrongly assigned vendor GSTIN
            "invoice_no": "$/2682",
            "tax": None,
            "total": "21106.47",
        }
        out = fix_zil_pharma_summary_from_ocr(summary, OCR_2682)
        self.assertEqual(out["vendor"], "Zil Pharma Enterprises Pvt. Ltd.")
        self.assertEqual(out["vendor_gstin"], "27AABCZ9702A1ZH")
        self.assertEqual(out["customer_gstin"], "27AAATT0530C1Z1")
        self.assertEqual(out["invoice_no"], "S/2682")
        self.assertEqual(out["tax"], "1005.10")
        self.assertEqual(out["total"], "21106.47")

    def test_summary_saifee_customer_gstin(self):
        summary = {
            "vendor": "None",
            "vendor_gstin": "",
            "customer": "SAIFEE HOSPITAL",
            "customer_gstin": "27AABCZ9702A1ZH",
            "invoice_no": "$/2808",
            "tax": None,
            "total": None,
        }
        out = fix_zil_pharma_summary_from_ocr(summary, OCR_2808)
        self.assertEqual(out["customer_gstin"], "27AABTS7800D1Z0")
        self.assertEqual(out["invoice_no"], "S/2808")
        self.assertEqual(out["tax"], "822.00")
        self.assertEqual(out["total"], "17262.00")

    def test_drops_saif_box_false_row(self):
        items = [
            {
                "product_description": "PANTODAC 40MG TAB (NEW)",
                "quantity": "120",
                "unit_price": "137.00",
                "total_amount": "16440.00",
                "hsn_code": "30049039",
                "lot_batch_number": "1B00409C",
            },
            {
                "product_description": "SAIF BOX",
                "hsn_code": "",
                "quantity": "14",
                "unit_price": "777.00",
                "total_amount": "11026.00",
                "lot_batch_number": "",
                "recovered_from_ocr": True,
            },
            {
                "product_description": "SHIPPSDEP BOX",
                "hsn_code": "",
                "quantity": None,
                "unit_price": None,
                "total_amount": None,
                "lot_batch_number": "",
                "recovered_from_ocr": True,
            },
        ]
        out = fix_zil_pharma_line_items_from_ocr(items, OCR_2808)
        descs = [i["product_description"] for i in out]
        self.assertEqual(len(out), 1)
        self.assertIn("PANTODAC", descs[0])
        self.assertNotIn("SAIF BOX", descs)
        self.assertNotIn("SHIPPSDEP BOX", descs)

    def test_fixes_mrp_as_rate_and_wrong_amount(self):
        # Gemini used MRP 236.16 as rate and invented 70848 amount
        items = [
            {
                "product_description": "RINIFOL CAP (15 CAP)",
                "quantity": "30",
                "unit_price": "91.88",
                "total_amount": "2756.40",
                "hsn_code": "30049099",
                "lot_batch_number": "REC6C017",
                "additional_fields": {"mrp": "91.88"},
            },
            {
                "product_description": "PANTODAC - 40MG TAB (NEW)",
                "quantity": "300",
                "unit_price": "236.16",
                "total_amount": "70848.00",
                "hsn_code": "30049039",
                "lot_batch_number": "1B00409C",
                "additional_fields": {"mrp": "236.16"},
            },
        ]
        out = fix_zil_pharma_line_items_from_ocr(items, OCR_3159)
        by_batch = {i["lot_batch_number"]: i for i in out}
        pant = by_batch["1B00409C"]
        self.assertEqual(pant["unit_price"], "150.00")
        self.assertEqual(pant["total_amount"], "45000.00")
        rin = by_batch["REC6C017"]
        self.assertEqual(rin["unit_price"], "70.00")
        self.assertEqual(rin["total_amount"], "2100.00")

    def test_parse_ocr_rows(self):
        rows = _parse_zil_pharma_ocr_rows(OCR_2682)
        self.assertGreaterEqual(len(rows), 3)
        batches = {r["lot_batch_number"] for r in rows}
        self.assertIn("1800409C", batches)
        pant = next(r for r in rows if r["lot_batch_number"] == "1800409C")
        self.assertEqual(pant["unit_price"], 150.0)
        self.assertEqual(pant["total_amount"], 9000.0)

    def test_tax_total_extract(self):
        tax, total = _zil_pharma_extract_tax_and_total(OCR_3159)
        self.assertEqual(tax, "2355.00")
        self.assertEqual(total, "49455.00")

    def test_taxable_residual_when_ocr_drops_one_amount(self):
        # PANTODAC fixed from OCR; RINIFOL amount missing in OCR → residual 2100
        ocr = OCR_3159  # taxable 47100 = 2100 + 45000
        items = [
            {
                "product_description": "RINIFOL CAP (15 CAP)",
                "quantity": "30",
                "unit_price": "91.88",
                "total_amount": "2756.40",
                "hsn_code": "30049099",
                "lot_batch_number": "REC6C017",
                "additional_fields": {"mrp": "91.88"},
            },
            {
                "product_description": "PANTODAC - 40MG TAB (NEW)",
                "quantity": "300",
                "unit_price": "150.00",
                "total_amount": "45000.00",
                "hsn_code": "30049039",
                "lot_batch_number": "1B00409C",
                "additional_fields": {"mrp": "236.16"},
            },
        ]
        out = fix_zil_pharma_line_items_from_ocr(items, ocr)
        rin = next(i for i in out if "RINIFOL" in i["product_description"])
        self.assertEqual(rin["total_amount"], "2100.00")
        self.assertEqual(rin["unit_price"], "70.00")


if __name__ == "__main__":
    unittest.main()
