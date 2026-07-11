"""Invoice number extraction edge cases — BETA AGENCIES only."""
import unittest

from app import (
    _is_invoice_no_label_anchored,
    _looks_like_hsn_code,
    extract_invoice_no_from_ocr_header,
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


if __name__ == "__main__":
    unittest.main()
