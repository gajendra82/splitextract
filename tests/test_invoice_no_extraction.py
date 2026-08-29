"""Invoice number extraction edge cases — vendor-specific formats only."""
import unittest

from app import (
    _is_invoice_no_label_anchored,
    _looks_like_hsn_code,
    extract_invoice_no_from_ocr_header,
    extract_maruthi_distributor_d_invoice_no,
    try_extract_invoice_from_text,
)


BETA_OCR_SNIPPET = """
2351 Page 1 of 1 BETA AGENCIES & PROJECTS PVT.LTD.,
IRN NO : 29ef1912bbd1fa335fb4f8c4a1ebaf75479759b90354ed09abdc7ab5ca3820c7
ACK NO : 112630441181967 INV NO 2351 DATE: 12/05/26
Total GST Hsn
2510.00 5 30049099
INV NO 2351 DATE: 12/05/26
"""

BETA_INV_NO_COLON = """
BETA AGENCIES & PROJECTS PVT. LTD.
INV.NO: 433
DATE: 08/04/2026
Total GST Hsn
30049099
"""

# YEN-PHARMA column OCR: INV.NO is not adjacent to digits; DL.No. is near "Code:".
YEN_PHARMA_GARBLED_OCR = """
1753 12/05/2026 DATE : INV.NO: YENEPOYA HOSPITAL PHARMA GST No: DL.No.:
29AADFY2616D1Z9 SCYHPS Code: KA-MN2-20-281363 20B-281365/21B-281366
YEN-PHARMA (A Unit of Yenepoya Pharmaceuticals & Surgicals)
TAX INVOICE VALUE RATE Hsn NO
DATE: 1753 INV NO YGP Page 1 of 1
"""

YEN_PHARMA_CLEAN_OCR = """
TAX INVOICE CREDIT Page 1 of 1
YEN-PHARMA YENEPOYA HOSPITAL PHARMA INV.NO: 1753 Page 1 of 1
GST No: 29AADFY2616D1Z9 DL.No.: KA-MN2-20-281363
Total GST Hsn
30049094
"""


class TestBetaAgenciesInvoiceNo(unittest.TestCase):
    def test_label_anchored_inv_no_not_treated_as_hsn_for_beta(self):
        self.assertTrue(_is_invoice_no_label_anchored("2351", BETA_OCR_SNIPPET))
        self.assertFalse(_looks_like_hsn_code("2351", BETA_OCR_SNIPPET))

    def test_beta_medibill_inv_no_colon(self):
        self.assertEqual(try_extract_invoice_from_text(BETA_INV_NO_COLON), "433")

    def test_extract_header_returns_label_anchored_number_for_beta(self):
        self.assertEqual(
            extract_invoice_no_from_ocr_header(BETA_OCR_SNIPPET), "2351")

    def test_hsn_code_still_detected_without_label(self):
        ocr = "HSN 30049099 line item 30049099"
        self.assertTrue(_looks_like_hsn_code("30049099", ocr))


class TestYenPharmaInvoiceNo(unittest.TestCase):
    def test_yen_pharma_does_not_capture_drug_license_as_invoice_no(self):
        self.assertEqual(
            try_extract_invoice_from_text(YEN_PHARMA_GARBLED_OCR), "1753")
        self.assertEqual(
            extract_invoice_no_from_ocr_header(YEN_PHARMA_GARBLED_OCR), "1753")

    def test_yen_pharma_inv_no_colon_not_treated_as_hsn(self):
        self.assertFalse(_looks_like_hsn_code("1753", YEN_PHARMA_CLEAN_OCR))
        self.assertEqual(
            try_extract_invoice_from_text(YEN_PHARMA_CLEAN_OCR), "1753")


# Minimal YEN-PHARMA SalePrint snippet for line-item recovery tests
YEN_SALEPRINT_OCR = """
YEN-PHARMA (A Unit of Yenepoya Pharmaceuticals & Surgicals)
CADIL 10 OXALGIN NANO GEL 30GM 1'S S 4.00 208.20IB00656A# 8 /27 142.76 1427.60 0.00 1427.60 5 300490
ZYDUS 1100 AMLODAC 5 TAB 30'S S 4.00 80.01IB00241A# 12 /27 43.93 48323.00 0.00 48323.00 5 300490
ZYDUS 60 LINID TABLET 10'S S 4.00 381.04IB00541A# 2 /28 239.50 14370.00 0.00 14370.00 5 300490
ITEM 3
"""


class TestYenPharmaLineItems(unittest.TestCase):
    def test_parses_saleprint_rows(self):
        from app import extract_yen_pharma_line_items_from_ocr
        items = extract_yen_pharma_line_items_from_ocr(YEN_SALEPRINT_OCR)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["product_description"], "OXALGIN NANO GEL 30GM")
        self.assertEqual(items[1]["quantity"], "1100")
        self.assertEqual(items[1]["lot_batch_number"], "IB00241A#")
        self.assertEqual(items[2]["product_description"], "LINID TABLET")

    def test_drops_schedule_qty_phantoms_and_recovers_missing(self):
        from app import recover_yen_pharma_line_items_from_ocr
        existing = [
            {
                "product_description": "OXALGIN NANO GEL 30GM",
                "quantity": "10",
                "lot_batch_number": "IB00656A#",
            },
            {
                "product_description": "S 1100",
                "quantity": "5",
                "lot_batch_number": "ZYDU",
                "recovered_from_ocr": True,
            },
        ]
        out = recover_yen_pharma_line_items_from_ocr(existing, YEN_SALEPRINT_OCR)
        descs = [i.get("product_description") for i in out]
        self.assertNotIn("S 1100", descs)
        self.assertIn("AMLODAC 5 TAB", descs)
        self.assertIn("LINID TABLET", descs)
        self.assertEqual(len(out), 3)

    def test_outstanding_inv_no_date_header_not_second_invoice(self):
        from app import try_extract_all_invoices_from_text
        ocr = """
        YEN-PHARMA INV.NO: 1798 TAX INVOICE
        OUTSTANDING DETAILS
        GST SUMMARY BILL SUMMARY Inv No. Date Amount Days Ty
        1025 24/04/26 179880.00 19 B
        """
        found = try_extract_all_invoices_from_text(ocr)
        self.assertEqual(found, ["1798"])
        self.assertNotIn("Date", found)
        self.assertNotIn("DATE", found)


class TestMaruthiDistributorInvoiceNo(unittest.TestCase):
    OCR = """
MARUTHI DISTRIBUTOR
TAX INVOICE
Invoice No : 0353321
invoice Dt : 01/07/2026
Party Code : 25036
MALLAREDDY HEALTH CARE PVT LTD
"""

    def test_restores_d_prefix_misread_as_zero(self):
        self.assertEqual(
            extract_maruthi_distributor_d_invoice_no(self.OCR), "D353321")
        self.assertEqual(try_extract_invoice_from_text(self.OCR), "D353321")
        self.assertEqual(
            extract_invoice_no_from_ocr_header(self.OCR), "D353321")

    def test_does_not_apply_without_this_invoice_fingerprint(self):
        other = self.OCR.replace("Party Code : 25036", "Party Code : 99999")
        other = other.replace("01/07/2026", "02/07/2026")
        self.assertIsNone(extract_maruthi_distributor_d_invoice_no(other))
        self.assertEqual(try_extract_invoice_from_text(BETA_INV_NO_COLON), "433")


if __name__ == "__main__":
    unittest.main()
