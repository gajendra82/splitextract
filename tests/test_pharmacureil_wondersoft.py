"""PHARMACUREIL / Sf-Wondersoft: product names, taxable base, WsDocNo."""
import unittest

from app import (
    extract_pharmacureil_wondersoft_invoice_no,
    extract_pharmacureil_wondersoft_invoice_total,
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
            "1981.60",
        )
        self.assertEqual(
            out["data"]["invoice_summary"]["total"],
            "8442.00",
        )

    def test_invoice_total_is_grand_total_not_category_amount(self):
        ocr = """
PHARMACUREIL PRIVATE LIMITED
WsDocNo: 26-27GD18249
NETAMT
Amount in Words :
Rupees Twenty Thousand Three Hundred And Seventy One Only
20371.00
Grand Total
1238.36
CD
20370.87
Amount
19400.83
Base
21609.22
AMOUNT :
970.04
GST :
19400.83
BASE :
ITEMS :
Sf-Wondersoft(Ph:044-42073411)
"""
        self.assertEqual(
            extract_pharmacureil_wondersoft_invoice_total(ocr),
            20371.00,
        )
        from app import enforce_schema
        raw = {
            "data": {
                "invoice_summary": {
                    "vendor": "PHARMACUREIL PRIVATE LIMITED",
                    "invoice_no": "26-27GD18249",
                    "invoice_date": "2026-06-25",
                    "total": "20370.87",
                    "tax": "970.04",
                },
                "line_items": {"items": [
                    {"product_description": "NEBULA D TABS", "quantity": "15",
                     "unit_price": "175.14", "total_amount": "2592.95",
                     "additional_fields": {"net_amount": "20370.88"}},
                ]},
                "ocr_text": ocr,
            }
        }
        out = enforce_schema(raw)
        self.assertEqual(out["data"]["invoice_summary"]["total"], "20371.00")
        self.assertEqual(
            out["data"]["invoice_summary"]["calculated_total"],
            "20371.00",
        )
        item = out["data"]["line_items"]["items"][0]
        self.assertEqual(item["product_description"], "NEBULA D TABS")
        self.assertEqual(item["quantity"], "15")
        self.assertEqual(item["unit_price"], "175.14")
        self.assertEqual(item["total_amount"], "2592.95")

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
        self.assertNotIn("tax_amount", rows[0])
        self.assertEqual(rows[0]["additional_fields"]["net_amount"], "1762.29")
        self.assertEqual(rows[0]["additional_fields"]["taxable_amount"], "1678.37")
        self.assertEqual(rows[1]["product_description"], "HYOCIMAX S TAB")
        self.assertEqual(rows[1]["total_amount"], "219.31")

    def test_parses_inhaler_and_syrup_pack_rows(self):
        ocr = PYMUPDF_OCR + """
6222.05
296.29
05%
1010.26
.00
630.40
10
12/27
IB00187A
120MD
*ODIHALE 100 INH*
ZYDU
30049099
183.56
8.74
05%
135.61
144.65
92.99
2
06/27
OCS25004
60ml
*ODIMONT LC SYP *
ZYDU
30049099
"""
        rows = extract_pharmacureil_wondersoft_line_items_from_ocr(ocr)
        names = [r["product_description"] for r in rows]
        self.assertIn("ODIHALE 100 INH", names)
        self.assertIn("ODIMONT LC SYP", names)
        by_name = {r["product_description"]: r for r in rows}
        self.assertEqual(by_name["ODIHALE 100 INH"]["unit_price"], "630.40")
        self.assertEqual(by_name["ODIHALE 100 INH"]["quantity"], "10")
        self.assertEqual(by_name["ODIHALE 100 INH"]["total_amount"], "6222.05")
        self.assertEqual(by_name["ODIMONT LC SYP"]["unit_price"], "92.99")
        self.assertEqual(by_name["ODIMONT LC SYP"]["quantity"], "2")
        self.assertEqual(by_name["ODIMONT LC SYP"]["total_amount"], "183.56")

    def test_parses_cream_gm_pack_row(self):
        ocr = PYMUPDF_OCR + """
603.58
92.07
18%
999.00
.00
544.16
1
08/27
AVB013
15gm
*FAIREYE CREAM*
ZYDU
33049910
"""
        rows = extract_pharmacureil_wondersoft_line_items_from_ocr(ocr)
        names = [r["product_description"] for r in rows]
        self.assertEqual(names.count("FAIREYE CREAM"), 1)
        cream = next(r for r in rows if r["product_description"] == "FAIREYE CREAM")
        self.assertEqual(cream["quantity"], "1")
        self.assertEqual(cream["unit_price"], "544.16")
        self.assertEqual(cream["total_amount"], "603.58")
        self.assertEqual(cream["lot_batch_number"], "AVB013")

    def test_parses_inj_ml_pack_with_apostrophe(self):
        ocr = PYMUPDF_OCR + """
2383.60
113.50
05%
287.00
.00
161.00
15
01/30
GB0125A
1'ml
*PROLUTON DEPOT 250mg INJ*
ZYDU
30043190
"""
        rows = extract_pharmacureil_wondersoft_line_items_from_ocr(ocr)
        names = [r["product_description"] for r in rows]
        self.assertEqual(names.count("PROLUTON DEPOT 250mg INJ"), 1)
        inj = next(r for r in rows if r["product_description"] == "PROLUTON DEPOT 250mg INJ")
        self.assertEqual(inj["quantity"], "15")
        self.assertEqual(inj["unit_price"], "161.00")
        self.assertEqual(inj["total_amount"], "2383.60")
        self.assertEqual(inj["lot_batch_number"], "GB0125A")

    def test_parses_respule_compound_ml_pack_rows(self):
        ocr = PYMUPDF_OCR + """
2980.74
141.94
05%
294.95
.00
151.00
20
06/27
EA60038
5*2ml
*GLYNIUM 25mg RESPULES*
ZYDU
30049099
1192.30
56.78
05%
294.95
.00
151.00
8
04/27
EK50199
5*2ml
*GLYNIUM 25mg RESPULES*
ZYDU
30049099
1192.30
56.78
05%
294.95
.00
151.00
8
05/27
EL50236
5*2ml
*GLYNIUM 25mg RESPULES*
ZYDU
30049099
"""
        rows = extract_pharmacureil_wondersoft_line_items_from_ocr(ocr)
        gly = [r for r in rows if r["product_description"] == "GLYNIUM 25mg RESPULES"]
        self.assertEqual(len(gly), 3)
        by_batch = {r["lot_batch_number"]: r for r in gly}
        self.assertEqual(by_batch["EA60038"]["quantity"], "20")
        self.assertEqual(by_batch["EA60038"]["unit_price"], "151.00")
        self.assertEqual(by_batch["EA60038"]["total_amount"], "2980.74")
        self.assertEqual(by_batch["EK50199"]["quantity"], "8")
        self.assertEqual(by_batch["EK50199"]["total_amount"], "1192.30")
        self.assertEqual(by_batch["EL50236"]["quantity"], "8")
        self.assertEqual(by_batch["EL50236"]["total_amount"], "1192.30")

    def test_parses_product_name_with_percent(self):
        ocr = PYMUPDF_OCR + """
408.91
19.47
05%
57.68
.00
41.43
10
01/28
AHC114
50gm
*XYLOCAINE 5% OINT*
ZYDU
30049099
"""
        rows = extract_pharmacureil_wondersoft_line_items_from_ocr(ocr)
        names = [r["product_description"] for r in rows]
        self.assertEqual(names.count("XYLOCAINE 5% OINT"), 1)
        item = next(r for r in rows if r["product_description"] == "XYLOCAINE 5% OINT")
        self.assertEqual(item["quantity"], "10")
        self.assertEqual(item["unit_price"], "41.43")
        self.assertEqual(item["total_amount"], "408.91")
        self.assertEqual(item["lot_batch_number"], "AHC114")

    def test_parses_decimal_compound_ml_pack_split_across_lines(self):
        ocr = PYMUPDF_OCR + """
1243.62
59.22
05%
131.25
.00
63.00
20
01/28
EB60051
5*2.5M
L
*COMBIMIST L RESP*
ZYDU
30049099
"""
        rows = extract_pharmacureil_wondersoft_line_items_from_ocr(ocr)
        names = [r["product_description"] for r in rows]
        self.assertEqual(names.count("COMBIMIST L RESP"), 1)
        item = next(r for r in rows if r["product_description"] == "COMBIMIST L RESP")
        self.assertEqual(item["quantity"], "20")
        self.assertEqual(item["unit_price"], "63.00")
        self.assertEqual(item["total_amount"], "1243.62")
        self.assertEqual(item["lot_batch_number"], "EB60051")

    def test_parses_same_batch_repeated_line(self):
        ocr = PYMUPDF_OCR + """
1022.53
48.69
05%
156.66
.00
103.60
10
11/27
APF12AEA
10's
*TORGET PLUS 10mg TAB*
ZYDU
30049099
1022.53
48.69
05%
156.66
.00
103.60
10
11/27
APF12AEA
10's
*TORGET PLUS 10mg TAB*
ZYDU
30049099
"""
        rows = extract_pharmacureil_wondersoft_line_items_from_ocr(ocr)
        plus = [r for r in rows if r["product_description"] == "TORGET PLUS 10mg TAB"]
        self.assertEqual(len(plus), 2)
        for r in plus:
            self.assertEqual(r["quantity"], "10")
            self.assertEqual(r["unit_price"], "103.60")
            self.assertEqual(r["total_amount"], "1022.53")
            self.assertEqual(r["lot_batch_number"], "APF12AEA")

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
