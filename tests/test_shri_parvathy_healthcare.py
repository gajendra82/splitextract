"""SHRI PARVATHY HEALTHCARE Tax Inv. No. / Mfac-Rack layout."""
import unittest

from app import (
    _is_shri_parvathy_dl_number,
    _normalize_shri_parvathy_product,
    _shri_parvathy_skip_pdfplumber,
    extract_shri_parvathy_invoice_date,
    extract_shri_parvathy_invoice_no,
    extract_shri_parvathy_line_items_from_ocr,
    fix_shri_parvathy_healthcare_fields,
    ocr_suggests_shri_parvathy_healthcare,
    recover_missing_items_from_ocr,
    try_extract_invoice_from_text,
)

PYMUPDF_OCR = """
1
SHRI PAR~ATH_Y~EALTHCARE
*2500173008672*
Phannaceut,cal D1stnbutors,
NO:4 &5,KRISHNASWAMY ROAD.BROOKE BOND COLONY,R.S.PL CASH
-
NS
COIMBATORE-641002,COIMBATORE-641002
Tax Inv. No.
Phone : 2542634,9364444772,93644447;
Invoice Date
DI No
:CBE/5599/20B,
CBE/5513/21B.
Due Date
in No
,
Order No
:2500173008672
:04/02/26
· 04/02/26
Pan No.:AEDFS6695G
IGSTIN : 33AEDFS6695G1ZP
Email.shriparvathyhealthcare@gmi
KONGUNAD HOSPITALS (P) LTD
Mfac/JRack I Item Description
zvo
EuNSU\\zvcoLcH1N TAB
10'$
1300490991
64
IA02009A
11-27
34.82;
23.82
0.0
1524.48
Prep By:
Tot Items: 1
TotQty :64
SaleValue
1524.48
Grand Total
1601.00
For : SHRI PARVATHY HEALTHCARE
"""

PDFPLUMBER_OCR = """
1 *2500173008672*
SHRI PAR~ATH_Y~EALTHCARE
Phannaceut,cal D1stnbutors, Code: 1392
COIMBATORE-641002,COIMBATORE-641002 Tax Inv. No. KONGUNADU HOSPITAL COMPLEX
DP iIh n No Nn oe o :: C 25 B4 E2 /6 53 54 9 ,, 99 /3 26 04 B4 , 44772,93644447; CBE/5513/21B. DIn uv eo i Dce a tD ea te ·:: 02 045 4/0 /0 00 2 21 / /27 263 6 0 08672
zvo EuNSU\\zvcoLcH1N TAB 10'$ 1300490991 64 IA02009A 11-27 34.82; 23.82 0.0 o.od 1524.4812.50 I 38.111 2.501 38.111 1soo.10
TotQty :64 SaleValue 1524.48
Grand Total 1601.00
For : SHRI PARVATHY HEALTHCARE
"""

BETA_OCR = """
BETA AGENCIES & PROJECTS PVT. LTD.
INV.NO: 433
DATE: 08/04/2026
"""


class TestShriParvathyHealthcare(unittest.TestCase):
    def test_detector_matches_vendor_ocr(self):
        self.assertTrue(ocr_suggests_shri_parvathy_healthcare(PYMUPDF_OCR))
        self.assertTrue(ocr_suggests_shri_parvathy_healthcare(PDFPLUMBER_OCR))
        self.assertFalse(ocr_suggests_shri_parvathy_healthcare(BETA_OCR))

    def test_invoice_no_from_barcode_not_dl_number(self):
        self.assertEqual(
            extract_shri_parvathy_invoice_no(PYMUPDF_OCR), "2500173008672")
        self.assertEqual(
            extract_shri_parvathy_invoice_no(PDFPLUMBER_OCR), "2500173008672")
        self.assertEqual(
            try_extract_invoice_from_text(PYMUPDF_OCR), "2500173008672")
        self.assertTrue(_is_shri_parvathy_dl_number("CBE/5513/21B"))
        self.assertFalse(_is_shri_parvathy_dl_number("2500173008672"))

    def test_invoice_date_is_day_first(self):
        self.assertEqual(
            extract_shri_parvathy_invoice_date(PYMUPDF_OCR), "2026-02-04")

    def test_pdfplumber_without_dates_is_skipped(self):
        self.assertTrue(_shri_parvathy_skip_pdfplumber(PDFPLUMBER_OCR))
        self.assertFalse(_shri_parvathy_skip_pdfplumber(PYMUPDF_OCR))
        self.assertFalse(_shri_parvathy_skip_pdfplumber(BETA_OCR))

    def test_product_and_qty_from_garbled_row(self):
        self.assertEqual(
            _normalize_shri_parvathy_product("zvo EuNSU\\zvcoLcH1N TAB"),
            "ZYCOLCHIN TAB",
        )
        rows = extract_shri_parvathy_line_items_from_ocr(PYMUPDF_OCR)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product_description"], "ZYCOLCHIN TAB")
        self.assertEqual(rows[0]["quantity"], "64")
        plumber_rows = extract_shri_parvathy_line_items_from_ocr(PDFPLUMBER_OCR)
        self.assertEqual(plumber_rows[0]["product_description"], "ZYCOLCHIN TAB")
        self.assertEqual(plumber_rows[0]["quantity"], "64")

    def test_does_not_recover_pack_as_qty(self):
        recovered = recover_missing_items_from_ocr([], PDFPLUMBER_OCR)
        self.assertEqual(recovered, [])

    def test_fix_overrides_dl_number_month_swap_pack_qty(self):
        summary = {
            "invoice_no": "CBE/5513/21B",
            "invoice_date": "2026-04-02",
            "total": "1601.00",
            "tax": "76.22",
        }
        items = [{
            "product_description": "ZUNSU",
            "quantity": "10",
            "unit_price": "23.82",
            "total_amount": "1524.48",
        }]
        out = fix_shri_parvathy_healthcare_fields(
            items, summary, PYMUPDF_OCR, "SHRI PARVATHY HEALTHCARE")
        self.assertEqual(summary["invoice_no"], "2500173008672")
        self.assertEqual(summary["invoice_date"], "2026-02-04")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["product_description"], "ZYCOLCHIN TAB")
        self.assertEqual(out[0]["quantity"], "64")
        self.assertEqual(out[0]["unit_price"], "23.82")
        self.assertEqual(out[0]["total_amount"], "1524.48")
        self.assertEqual(summary["total"], "1601.00")

    def test_other_formats_unchanged(self):
        self.assertEqual(try_extract_invoice_from_text(BETA_OCR), "433")


if __name__ == "__main__":
    unittest.main()
