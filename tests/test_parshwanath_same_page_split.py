"""PARSHWANATH CRITICAL CARE same-page dual PUNECCB invoice split."""
import unittest

from app import (
    collect_parshwanath_same_page_invoice_nos,
    extract_parshwanath_puneccb_invoice_nos,
    ocr_suggests_parshwanath_critical_care,
    split_parshwanath_ocr_by_puneccb_invoices,
    _normalize_parshwanath_puneccb_token,
)


PARSH_STACKED_OCR = """
PARSHWANATH CRITICAL CARE
SHOP NO.14 GR.FL.PURVA COMPLEX
HAPPI D CAP 10 IB01173A
HYOCIMAX 5 TAB 90
Customer Copy Page No. 1/

PARSHWANATH CRITICAL CARE GST TAX INVOICE
SHOP NO.14 GR FL.PURVA COMPLEX PUNECCB50302"
K.E.M. HOSPITALS
LINID INFUSION 300 ML 60
Subject to PUNE jurisdisction
"""


class TestParshwanathSamePageSplit(unittest.TestCase):
    def test_detects_vendor(self):
        self.assertTrue(ocr_suggests_parshwanath_critical_care(PARSH_STACKED_OCR))
        self.assertFalse(ocr_suggests_parshwanath_critical_care("Pharmacea Link"))

    def test_normalizes_ocr_tokens(self):
        self.assertEqual(
            _normalize_parshwanath_puneccb_token("PUNE€CB503O1'"),
            "PUNECCB50301",
        )
        self.assertEqual(
            _normalize_parshwanath_puneccb_token('PUNECCB50302"'),
            "PUNECCB50302",
        )

    def test_merges_vision_and_ocr_invoice_nos(self):
        # OCR only has the second invoice; Vision returns the first.
        nos = collect_parshwanath_same_page_invoice_nos(
            PARSH_STACKED_OCR, "PUNECCB50179"
        )
        self.assertEqual(nos, ["PUNECCB50179", "PUNECCB50302"])

    def test_extract_from_ocr_only(self):
        self.assertEqual(
            extract_parshwanath_puneccb_invoice_nos(PARSH_STACKED_OCR),
            ["PUNECCB50302"],
        )

    def test_split_keeps_both_sections_and_injects_missing_first_inv(self):
        nos = ["PUNECCB50179", "PUNECCB50302"]
        sections = split_parshwanath_ocr_by_puneccb_invoices(
            PARSH_STACKED_OCR, nos
        )
        self.assertEqual(set(sections.keys()), set(nos))
        self.assertIn("PUNECCB50179", sections["PUNECCB50179"])
        self.assertIn("HAPPI D CAP", sections["PUNECCB50179"])
        self.assertNotIn("LINID INFUSION", sections["PUNECCB50179"])
        self.assertIn("PUNECCB50302", sections["PUNECCB50302"])
        self.assertIn("LINID INFUSION", sections["PUNECCB50302"])
        self.assertNotIn("HAPPI D CAP", sections["PUNECCB50302"])

    def test_non_parshwanath_returns_empty(self):
        self.assertEqual(
            collect_parshwanath_same_page_invoice_nos(
                "SOME OTHER VENDOR\nINV 123", "PUNECCB50179"
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
