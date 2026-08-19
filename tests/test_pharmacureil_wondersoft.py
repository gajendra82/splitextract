"""PHARMACUREIL / Sf-Wondersoft: product names, taxable base, WsDocNo."""
import unittest

from app import (
    extract_pharmacureil_wondersoft_invoice_no,
    extract_pharmacureil_wondersoft_line_items_from_ocr,
    fix_pharmacureil_wondersoft_line_items_from_ocr,
    ocr_suggests_pharmacureil_wondersoft,
    try_extract_invoice_from_text,
)

PYMUPDF_OCR = """
PHARMACUREIL PRIVATE LIMITED
SHANTHI SOCIAL SERVICES(PHARMACY DIV)
26-27GD18891
WsDocNo:
BILLDATE 29/06/2026
NETAMT
SG/C
G
GST
MRP
OLD
MRP
RATE
QTY
EXP
BATCH
PACK
DESCRIPTION
MFR
HSN
1762.29
83.92
05%
260.39
.00
178.55
10
01/28
EMV260500C
10's
*DAPAGLYN SM 100/500mg TAB
ZYDU
30049099
219.31
10.44
05%
63.63
.00
44.44
5
08/27
IA01342A
10's
*HYOCIMAX S TAB*
ZYDU
30049099
Sf-Wondersoft(Ph:044-42073411)
ITEMS : 6 QTY : 79 BASE : 8039.79
"""

PDFPLUMBER_OCR = """
PHARMACUREIL PRIVATE LIMITED
GST No: 33AANCP2938G1ZA FSSAI:12413003001748 TAX INVOICE
SHANTHI SOCIAL SERVICES(PHARMACY DIV)
WsDocNo: 26-27GD18891 GSTIN: 33AACTS6602A1Z2
HSN MFR DESCRIPTION PACK BATCH EXP QTY E RATE MRP MRP GST G NETAMT
30049099ZYDUS* SDHAAPNATGHLIY*N SM 100/500mg TAB*10's EMV260500C 01/28 10 178.55 .00 260.3905% 83.92 1762.29
E.&O.E Sf-Wondersoft(Ph:044-42073411)
"""

YEN_OCR = """
YEN-PHARMA (A Unit of Yenepoya Pharmaceuticals & Surgicals)
INV.NO: 1753
TAX INVOICE VALUE RATE Hsn NO
"""


class TestPharmacureilWondersoft(unittest.TestCase):
    def test_detector_only_matches_pharmacureil(self):
        self.assertTrue(ocr_suggests_pharmacureil_wondersoft(PYMUPDF_OCR))
        self.assertTrue(ocr_suggests_pharmacureil_wondersoft(PDFPLUMBER_OCR))
        self.assertFalse(ocr_suggests_pharmacureil_wondersoft(YEN_OCR))

    def test_wsdocno_invoice_number_not_truncated(self):
        self.assertEqual(
            extract_pharmacureil_wondersoft_invoice_no(PYMUPDF_OCR),
            "26-27GD18891",
        )
        self.assertEqual(
            extract_pharmacureil_wondersoft_invoice_no(PDFPLUMBER_OCR),
            "26-27GD18891",
        )
        self.assertEqual(
            try_extract_invoice_from_text(PYMUPDF_OCR),
            "26-27GD18891",
        )
        self.assertEqual(
            try_extract_invoice_from_text(PDFPLUMBER_OCR),
            "26-27GD18891",
        )

    def test_enforce_schema_replaces_dl_number_with_wsdocno(self):
        from app import enforce_schema
        raw = {
            "data": {
                "invoice_summary": {
                    "vendor": "PHARMACUREIL PRIVATE LIMITED",
                    "invoice_no": "CBE/6924/20",
                    "invoice_date": "2026-06-29",
                    "total": "8442.00",
                    "tax": "401.99",
                },
                "line_items": {"items": []},
                "ocr_text": PYMUPDF_OCR,
            }
        }
        out = enforce_schema(raw)
        self.assertEqual(
            out["data"]["invoice_summary"]["invoice_no"],
            "26-27GD18891",
        )
        self.assertEqual(
            out["data"]["invoice_summary"]["calculated_total"],
            "8442.00",
        )
        self.assertEqual(
            out["data"]["invoice_summary"]["total"],
            "8442.00",
        )

    def test_wsdocno_not_dl_number_when_label_follows_value(self):
        ocr = """
        PHARMACUREIL PRIVATE LIMITED
        26-27GD18891
        WsDocNo:
        CBE/6924/20,CBE/4444/20B
        DLNO1
        Sf-Wondersoft(Ph:044-42073411)
        NETAMT
        """
        self.assertEqual(
            extract_pharmacureil_wondersoft_invoice_no(ocr),
            "26-27GD18891",
        )
        self.assertEqual(try_extract_invoice_from_text(ocr), "26-27GD18891")

    def test_fix_replaces_gemini_column_swap_rows(self):
        items = [{
            "product_description": "*HYOCIMAX S TAB*",
            "quantity": "25",
            "unit_price": "44.44",
            "total_amount": "1111.00",
            "lot_batch_number": "IA01342A",
        }]
        out = fix_pharmacureil_wondersoft_line_items_from_ocr(
            items, PYMUPDF_OCR, "PHARMACUREIL PRIVATE LIMITED")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["product_description"], "DAPAGLYN SM 100/500mg TAB")
        self.assertEqual(out[0]["quantity"], "10")
        self.assertEqual(out[1]["product_description"], "HYOCIMAX S TAB")
        self.assertEqual(out[1]["quantity"], "5")
        self.assertEqual(out[1]["unit_price"], "44.44")

    def test_yen_invoice_no_unchanged(self):
        self.assertEqual(try_extract_invoice_from_text(YEN_OCR), "1753")

    def test_parses_description_and_taxable_base(self):
        rows = extract_pharmacureil_wondersoft_line_items_from_ocr(PYMUPDF_OCR)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["product_description"], "DAPAGLYN SM 100/500mg TAB")
        self.assertEqual(rows[0]["quantity"], "10")
        self.assertEqual(rows[0]["unit_price"], "178.55")
        self.assertEqual(rows[0]["total_amount"], "1762.29")
        self.assertEqual(rows[0]["tax_amount"], "5")
        self.assertEqual(rows[0]["additional_fields"]["net_amount"], "1762.29")
        self.assertEqual(rows[0]["additional_fields"]["taxable_amount"], "1678.37")
        self.assertEqual(rows[1]["product_description"], "HYOCIMAX S TAB")
        self.assertEqual(rows[1]["total_amount"], "219.31")

    def test_fix_overlays_garbled_name_and_netamt(self):
        items = [{
            "product_description": "S* SDHAAPNATGHLIY*N SM 100/500mg TAB",
            "quantity": "10",
            "unit_price": "178.55",
            "total_amount": "1762.29",
            "lot_batch_number": "EMV260500C",
        }]
        out = fix_pharmacureil_wondersoft_line_items_from_ocr(
            items, PYMUPDF_OCR, "PHARMACUREIL PRIVATE LIMITED")
        self.assertEqual(out[0]["product_description"], "DAPAGLYN SM 100/500mg TAB")
        self.assertEqual(out[0]["total_amount"], "1762.29")
        self.assertEqual(out[0]["quantity"], "10")
        self.assertEqual(out[0]["unit_price"], "178.55")

    def test_fix_skips_other_vendors(self):
        items = [{
            "product_description": "S* GARBLED",
            "quantity": "10",
            "unit_price": "178.55",
            "total_amount": "1762.29",
        }]
        out = fix_pharmacureil_wondersoft_line_items_from_ocr(
            items, YEN_OCR, "YEN-PHARMA")
        self.assertEqual(out[0]["product_description"], "S* GARBLED")
        self.assertEqual(out[0]["total_amount"], "1762.29")


if __name__ == "__main__":
    unittest.main()
