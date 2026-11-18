"""Sainath Agencies (Pharma) Pvt. Ltd. — stockist-scoped extraction regression."""
import unittest

from app import (
    _parse_sainath_agencies_ocr_rows,
    _sainath_agencies_extract_financials,
    _sainath_agencies_reconciliation,
    enforce_schema,
    fix_sainath_agencies_line_items_from_ocr,
    fix_sainath_agencies_summary_from_ocr,
    ocr_suggests_riddhishree_healthcare,
    ocr_suggests_sainath_agencies,
    ocr_suggests_zil_pharma,
)

# Deterministic OCR fixture modeled on verified S/7195 PDF ground truth.
# Values below are fixture evidence for unit tests — not production constants.
# Real S/7195 footer shape (PyMuPDF): column band + Total Amt / Round Off / TO PAY.
OCR_S7195 = """
TAX INVOICE
SAINATH AGENCIES (PHARMA) PVT. LTD.
PAN : AASCS7992L GST : 27AASCS7992L1Z4
SIR H N RELIANCE FOUNDATION HOSPITAL
GST : 27AANTS2921H1Z8  PAN : AANTS2921H
Inv No : S/7195
Inv Dt : 31/08/2026 (MON) R-6
Due Dt : 07/09/2026
HSN MFG PACK PRODUCT DESCRIPTION QTY FREE BATCH NO EXP. TD% CD% GST% M.R.P. RATE AMOUNT
30049039 ZYDU 1X15TA PANTODAC 40MG TAB 20 0 AB12345 02/28 0.00 0.00 5.0 236.16 150.00 3000.00
30049069 INDO 1X15TA FEBREX PLUS TAB 15 0 BC23456 12/28 0.00 0.00 5.0 111.00 200.00 3000.00
30044090 INDO 100ML CITAL LIQUID 100ML 10 0 CD34567 01/29 0.00 0.00 5.0 131.00 250.00 2500.00
30049099 ELAN 1X15CA RINIFOL CAP 15CAP 12 0 DE45678 05/27 0.00 0.00 5.0 91.88 180.50 2166.00
30049039 ZYDU 1X10TA ATORVA 10MG TAB 20 2 EF56789 08/28 0.00 0.00 5.0 95.00 85.25 1705.00
30042019 CIPL 1X10TA AZITHRAL 500 TAB 10 0 FG67890 03/28 0.00 0.00 5.0 120.00 95.00 950.00
30049011 SUNP 1X10TA SHELCAL 500 TAB 10 0 GH78901 11/27 0.00 0.00 5.0 110.00 55.00 550.00
30049012 ALKE 1X10TA PAN 40 TAB 10 0 HI89012 06/28 0.00 0.00 5.0 90.00 20.53 205.33
AMOUNT
SCH AMT DISC AMT
TAXABLE
GCESS
CGST CGST AMT SGST SGST AMT
TOTAL AMT
   14076.33
       0.00
     563.06   13513.27
       0.00  2.5%
     337.82  2.5%
     337.82    14188.91
Total Amt
:    14188.91
AD/LS Amt
:        0.00
Round Off
:         .09
TO PAY
`   14189.00
Total Prod :  8   Total QTY :  107
For SAINATH AGENCIES (PHARMA) PVT. LTD.
AUTHORISED SIGNATORY
"""

# Live-broken Gemini summary shape confirmed against 7195(1).pdf before the fix.
BROKEN_LIVE_SUMMARY = {
    "vendor": "SAINATH AGENCIES (PHARMA) PVT. LTD.",
    "vendor_gstin": "27AASCS7992L1Z4",
    "customer": "SIR H N RELIANCE FOUNDATION HOSPITAL",
    "customer_gstin": "27AANTS2921H1Z8",
    "invoice_no": "S/7195",
    "invoice_date": "2026-08-31",
    "tax": "563.06",
    "total": "14076.33",
}

OCR_S7195_IGST = """
SAINATH AGENCIES (PHARMA) PVT. LTD.
GSTIN : 27AASCS7992L1Z4 PAN : AASCS7992L
Bill To : OUT OF STATE HOSPITAL
GSTIN : 29AABCT1332L1ZB
Invoice No : S/7201 Invoice Date : 05/09/2026
HSN MFGR PACK PRODUCT DESCRIPTION QTY FREE BATCH EXP TD% CD% GST% MRP RATE AMOUNT
30049039 ZYDU 1X15TA PANTODAC 40MG TAB 10 0 ZZ11111 02/28 0.00 0.00 5.0 236.16 100.00 1000.00
Gross Amount : 1000.00
Discount Amount : 0.00
Taxable Amount : 1000.00
IGST 5% 50.00
Invoice Total : 1050.00
Round Off : 0.00
Net Payable : 1050.00
Total Products : 1  Total Qty : 10
"""

OCR_SPLIT_HEADER = """
SAI NATH AGENCIES
(PHARMA) PVT. LTD.
GSTIN: 27AASCS7992L1Z4
PAN: AASCS7992L
Bill To: SIR H N RELIANCE FOUNDATION HOSPITAL
GSTIN: 27AANTS2921H1Z8
Invoice No: S/7195 Date: 31/08/2026
HSN BATCH MRP RATE AMOUNT
Taxable 13513.27 CGST 337.82 SGST 337.82 Invoice Total 14188.91
"""


class TestSainathAgenciesInvoice(unittest.TestCase):
    def test_detects_sainath_and_isolates_other_stockists(self):
        self.assertTrue(ocr_suggests_sainath_agencies(OCR_S7195))
        self.assertTrue(ocr_suggests_sainath_agencies(OCR_SPLIT_HEADER))
        self.assertFalse(ocr_suggests_sainath_agencies("OTHER VENDOR TAX INVOICE"))
        # Must not trigger on S/ alone
        self.assertFalse(
            ocr_suggests_sainath_agencies("Invoice No : S/7195 SOME OTHER STOCKIST")
        )
        zil = """
        Zil Pharma Enterprises Pvt. Ltd. Invoice No: S/2682
        GSTIN : 27AABCZ9702A1ZH
        HSN/SAC MFGR BATCH RATE AMOUNT
        """
        self.assertTrue(ocr_suggests_zil_pharma(zil))
        self.assertFalse(ocr_suggests_sainath_agencies(zil))
        rd = "RIDDHISHREE HEALTHCARE Inv No : RSB2627/091"
        self.assertTrue(ocr_suggests_riddhishree_healthcare(rd))
        self.assertFalse(ocr_suggests_sainath_agencies(rd))

    def test_supplier_customer_gstin_separation(self):
        summary = {
            "vendor": "None",
            "vendor_gstin": "",
            "customer": "SIR H N RELIANCE FOUNDATION HOSPITAL",
            "customer_gstin": "27AASCS7992L1Z4",  # wrongly vendor GSTIN
            "invoice_no": "S/7195",
            "tax": "563.06",  # wrongly discount
            "total": "14189.00",
        }
        out = fix_sainath_agencies_summary_from_ocr(summary, OCR_S7195)
        self.assertEqual(out["vendor"], "SAINATH AGENCIES (PHARMA) PVT. LTD.")
        self.assertEqual(out["vendor_gstin"], "27AASCS7992L1Z4")
        self.assertEqual(out["customer_gstin"], "27AANTS2921H1Z8")
        self.assertNotEqual(out["customer_gstin"], out["vendor_gstin"])

    def test_vendor_name_from_split_header(self):
        summary = {"vendor": "None", "vendor_gstin": "", "customer_gstin": ""}
        out = fix_sainath_agencies_summary_from_ocr(summary, OCR_SPLIT_HEADER)
        self.assertEqual(out["vendor"], "SAINATH AGENCIES (PHARMA) PVT. LTD.")
        self.assertEqual(out["vendor_gstin"], "27AASCS7992L1Z4")

    def test_invoice_no_date_due_date(self):
        summary = {
            "vendor": "",
            "invoice_no": "",
            "invoice_date": "",
            "customer_gstin": "",
        }
        out = fix_sainath_agencies_summary_from_ocr(summary, OCR_S7195)
        self.assertEqual(out["invoice_no"], "S/7195")
        self.assertEqual(out["invoice_date"], "2026-08-31")
        af = out.get("additional_fields") or {}
        self.assertEqual(af.get("due_date"), "2026-09-07")

    def test_discount_not_mapped_to_tax(self):
        summary = {
            "vendor": "None",
            "vendor_gstin": "",
            "customer_gstin": "27AANTS2921H1Z8",
            "invoice_no": "S/7195",
            "tax": "563.06",
            "total": None,
        }
        out = fix_sainath_agencies_summary_from_ocr(summary, OCR_S7195)
        self.assertEqual(out["tax"], "675.64")  # 337.82 + 337.82
        self.assertNotEqual(out["tax"], "563.06")
        af = out["additional_fields"]
        self.assertEqual(af["discount_amount"], "563.06")
        self.assertEqual(af["taxable_amount"], "13513.27")
        self.assertEqual(af["cgst_amount"], "337.82")
        self.assertEqual(af["sgst_amount"], "337.82")
        self.assertEqual(af["invoice_total"], "14188.91")
        self.assertEqual(af["round_off"], "0.09")
        self.assertEqual(af["payable_amount"], "14189.00")
        self.assertEqual(out["total"], "14188.91")

    def test_live_broken_json_financials_corrected(self):
        """Regression: live JSON had tax=DISC AMT and total=AMOUNT gross."""
        self.assertEqual(BROKEN_LIVE_SUMMARY["tax"], "563.06")
        self.assertEqual(BROKEN_LIVE_SUMMARY["total"], "14076.33")
        out = fix_sainath_agencies_summary_from_ocr(
            dict(BROKEN_LIVE_SUMMARY), OCR_S7195
        )
        self.assertEqual(out["tax"], "675.64")
        self.assertEqual(out["total"], "14188.91")
        self.assertNotEqual(out["tax"], out["additional_fields"]["discount_amount"])
        self.assertNotEqual(out["total"], out["additional_fields"]["gross_amount"])
        self.assertEqual(out["additional_fields"]["payable_amount"], "14189.00")
        # Line-item list must not be required for financial correction
        fin = _sainath_agencies_extract_financials(OCR_S7195)
        self.assertEqual(fin["discount_amount"], 563.06)
        self.assertEqual(fin["invoice_total"], 14188.91)
        self.assertEqual(fin["round_off"], 0.09)

    def test_igst_configuration(self):
        summary = {
            "vendor": "None",
            "vendor_gstin": "",
            "customer_gstin": "",
            "tax": None,
            "total": None,
        }
        out = fix_sainath_agencies_summary_from_ocr(summary, OCR_S7195_IGST)
        self.assertEqual(out["tax"], "50.00")
        af = out["additional_fields"]
        self.assertEqual(af["igst_amount"], "50.00")
        self.assertEqual(af["discount_amount"], "0.00")
        self.assertEqual(out["total"], "1050.00")

    def test_parse_all_table_columns(self):
        rows = _parse_sainath_agencies_ocr_rows(OCR_S7195)
        self.assertEqual(len(rows), 8)
        pant = next(r for r in rows if r["lot_batch_number"] == "AB12345")
        self.assertEqual(pant["hsn_code"], "30049039")
        self.assertEqual(pant["manufacturer"], "ZYDU")
        self.assertEqual(pant["pack"], "1X15TA")
        self.assertIn("PANTODAC", pant["product_description"])
        self.assertEqual(pant["quantity"], 20.0)
        self.assertEqual(pant["free_quantity"], 0.0)
        self.assertEqual(pant["expiry_date"], "02/28")
        self.assertEqual(pant["trade_discount_pct"], 0.0)
        self.assertEqual(pant["cash_discount_pct"], 0.0)
        self.assertEqual(pant["gst_pct"], 5.0)
        self.assertEqual(pant["mrp"], 236.16)
        self.assertEqual(pant["unit_price"], 150.0)
        self.assertEqual(pant["total_amount"], 3000.0)
        ator = next(r for r in rows if r["lot_batch_number"] == "EF56789")
        self.assertEqual(ator["free_quantity"], 2.0)

    def test_mrp_vs_rate_confusion(self):
        items = [
            {
                "product_description": "PANTODAC 40MG TAB",
                "quantity": "20",
                "unit_price": "236.16",  # MRP wrongly used as rate
                "total_amount": "4723.20",
                "hsn_code": "30049039",
                "lot_batch_number": "AB12345",
                "additional_fields": {"mrp": "236.16"},
            },
        ]
        out = fix_sainath_agencies_line_items_from_ocr(items, OCR_S7195)
        pant = out[0]
        self.assertEqual(pant["unit_price"], "150.00")
        self.assertEqual(pant["total_amount"], "3000.00")
        self.assertEqual(pant["additional_fields"]["mrp"], "236.16")

    def test_free_quantity_zero_vs_null_and_batch_expiry(self):
        items = [
            {
                "product_description": "ATORVA 10MG TAB",
                "quantity": "20",
                "unit_price": "85.25",
                "total_amount": "1705.00",
                "hsn_code": "30049039",
                "lot_batch_number": "EF56789",
                "additional_fields": {},
            },
        ]
        out = fix_sainath_agencies_line_items_from_ocr(items, OCR_S7195)
        ator = next(i for i in out if i.get("lot_batch_number") == "EF56789")
        af = ator["additional_fields"]
        self.assertEqual(af["free_quantity"], "2")
        self.assertEqual(af["expiry_date"], "08/28")
        self.assertEqual(ator["lot_batch_number"], "EF56789")
        # Zero free stays explicit "0", not null
        pant = next(i for i in out if i.get("lot_batch_number") == "AB12345")
        self.assertEqual(pant["additional_fields"]["free_quantity"], "0")

    def test_drops_footer_contaminated_rows(self):
        items = [
            {
                "product_description": "PANTODAC 40MG TAB",
                "quantity": "20",
                "unit_price": "150.00",
                "total_amount": "3000.00",
                "hsn_code": "30049039",
                "lot_batch_number": "AB12345",
            },
            {
                "product_description": "Bank A/c HDFC AUTHORISED SIGNATORY",
                "quantity": None,
                "unit_price": None,
                "total_amount": None,
                "hsn_code": "",
                "recovered_from_ocr": True,
            },
            {
                "product_description": "ROUND OFF",
                "quantity": "1",
                "unit_price": "0.09",
                "total_amount": "0.09",
                "hsn_code": "",
                "recovered_from_ocr": True,
            },
        ]
        out = fix_sainath_agencies_line_items_from_ocr(items, OCR_S7195)
        descs = [i["product_description"] for i in out]
        self.assertIn("PANTODAC", descs[0])
        self.assertTrue(all("BANK" not in d.upper() for d in descs))
        self.assertTrue(all("ROUND" not in d.upper() for d in descs))

    def test_rebuild_when_product_count_matches_ocr(self):
        # Only 2 broken Gemini rows; OCR has 8 matching Total Products
        items = [
            {
                "product_description": "PANTODAC 40MG TAB",
                "quantity": "20",
                "unit_price": "150.00",
                "total_amount": None,
                "hsn_code": "30049039",
                "lot_batch_number": "AB12345",
            },
            {
                "product_description": "FEBREX PLUS TAB",
                "quantity": "15",
                "unit_price": "200.00",
                "total_amount": "3000.00",
                "hsn_code": "30049069",
                "lot_batch_number": "BC23456",
            },
        ]
        out = fix_sainath_agencies_line_items_from_ocr(items, OCR_S7195)
        self.assertEqual(len(out), 8)
        qty_sum = sum(float(i["quantity"]) for i in out)
        self.assertEqual(qty_sum, 107.0)

    def test_financial_extract_and_reconciliation(self):
        fin = _sainath_agencies_extract_financials(OCR_S7195)
        self.assertEqual(fin["discount_amount"], 563.06)
        self.assertEqual(fin["taxable_amount"], 13513.27)
        self.assertEqual(fin["cgst_amount"], 337.82)
        self.assertEqual(fin["sgst_amount"], 337.82)
        self.assertEqual(fin["tax"], 675.64)
        self.assertEqual(fin["invoice_total"], 14188.91)
        self.assertEqual(fin["round_off"], 0.09)
        self.assertEqual(fin["payable_amount"], 14189.00)
        self.assertEqual(fin["printed_product_count"], 8)
        self.assertEqual(fin["printed_total_qty"], 107)

        rows = _parse_sainath_agencies_ocr_rows(OCR_S7195)
        items = [
            {
                "product_description": r["product_description"],
                "quantity": str(int(r["quantity"])),
                "total_amount": f"{r['total_amount']:.2f}",
            }
            for r in rows
        ]
        summary = {"additional_fields": {}}
        rec = _sainath_agencies_reconciliation(summary, items, fin)
        self.assertAlmostEqual(rec["line_sum"], 14076.33, places=2)
        self.assertEqual(rec["qty_sum"], 107.0)

    def test_recovery_failure_preserves_best_supported(self):
        # OCR without usable product rows — keep Gemini items
        sparse = """
        SAINATH AGENCIES (PHARMA) PVT. LTD.
        GSTIN : 27AASCS7992L1Z4
        Invoice No : S/7195
        Taxable Amount : 13513.27 CGST 337.82 SGST 337.82 Invoice Total : 14188.91
        """
        items = [
            {
                "product_description": "UNKNOWN PRODUCT",
                "quantity": "5",
                "unit_price": "10.00",
                "total_amount": "50.00",
                "hsn_code": "30049039",
                "lot_batch_number": "XX99999",
            }
        ]
        out = fix_sainath_agencies_line_items_from_ocr(items, sparse)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["product_description"], "UNKNOWN PRODUCT")

    def test_api_schema_compatibility(self):
        raw = {
            "invoice_summary": {
                "vendor": "None",
                "vendor_gstin": "27AANTS2921H1Z8",
                "customer": "SIR H N RELIANCE FOUNDATION HOSPITAL",
                "customer_gstin": "27AASCS7992L1Z4",
                "invoice_no": "S/7195",
                "invoice_date": "31/08/2026",
                "tax": "563.06",
                "total": "14189.00",
            },
            "line_items": {
                "items": [
                    {
                        "product_description": "PANTODAC 40MG TAB",
                        "quantity": "20",
                        "unit_price": "236.16",
                        "total_amount": "4723.20",
                        "hsn_code": "30049039",
                        "lot_batch_number": "AB12345",
                        "additional_fields": {"mrp": "236.16"},
                    }
                ]
            },
            "ocr_text": OCR_S7195,
        }
        formatted = enforce_schema(raw)
        data = formatted["data"]
        summary = data["invoice_summary"]
        self.assertEqual(summary["vendor"], "SAINATH AGENCIES (PHARMA) PVT. LTD.")
        self.assertEqual(summary["vendor_gstin"], "27AASCS7992L1Z4")
        self.assertEqual(summary["customer_gstin"], "27AANTS2921H1Z8")
        self.assertEqual(summary["invoice_no"], "S/7195")
        self.assertEqual(summary["tax"], "675.64")
        self.assertEqual(summary["total"], "14188.91")
        af = summary.get("additional_fields") or {}
        self.assertEqual(af.get("discount_amount"), "563.06")
        items = data["line_items"]["items"]
        self.assertGreaterEqual(len(items), 1)
        # Schema keys preserved
        self.assertIn("status", formatted)
        self.assertIn("message", formatted)
        self.assertIn("line_items", data)


if __name__ == "__main__":
    unittest.main()
