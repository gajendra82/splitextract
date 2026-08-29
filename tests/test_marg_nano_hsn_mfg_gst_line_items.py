"""MARG ERP NANO GST stockist: Product Name, Rate, billed Quantity from OCR."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _parse_marg_nano_hsn_mfg_gst_rows,
    fix_marg_nano_hsn_mfg_gst_line_items_from_ocr,
    ocr_suggests_marg_nano_hsn_mfg_gst,
)


GST_NANO_OCR = """
GST INVOICE Recipient Copy GST INVOICE Supplier Copy
LAXMI SALES PHARMACEUTICALS To:- S D M PHARMACY
HSN MFG PRODUCT PACK BATCH EXP MRP QTY RATE Dis VALUE
%
300490 EVE M COBAREN CAP 10'S BHC2511038310/27 206.00 100 156.96 0.00 0.00 5 15696.00 100
30045020 EVE M COBAREN CD3 CAP 10'S BCT26070 8/27 281.00 500 214.10 0.00 0.00 5 107050.00 500
30049099 FEM ALDOFEM INJ 1'S L1222602AL1 1/28 158.38 50 120.67 0.00 0.00 5 6033.50 50
30049079 GLE STILOZ 50MG TAB 15S SZ1PP602 2/28 368.50 21 280.76 0.00 280.76 5 5615.20 20+1
30041090 LUP TAZAR 4.5GM VIALS G272622 1/28 300.15 300 85.00 0.00 0.00 5 25500.00 300
30045039 LUP OPTINEURON INJ 3ML A26028VP 6/27 14.30 1000 10.90 0.00 0.00 5 10900.00 1000
21069099 MANHAIRBLESS TAB 15'S K4AIZ010 8/27 324.00 26 246.86 0.00 0.00 5 6418.36 26
30049079 MANCILAHEART 10MG TAB 15'S Q35Y005 11/27 158.26 60 120.58 0.00 602.90 5 6631.90 55+5
30049079 MANCILAHEART T TAB 15'S W55Z001 12/27 220.55 109 168.04 0.00 1512.36 5 16804.00 100+9
30049099 MYR MYSEVA 400MG TAB 10'S BD252585A 10/28 245.00 90 197.18 0.00 0.00 5 17746.20 90
30049099 OVE IGUSEAS TAB 10'S IGS05 1/28 304.69 144 232.14 0.00 5571.36 5 27856.80 120+24
30049099 PHA MGD3 TAB 10'S MDTM26016 10/28 295.00 200 224.76 0.00 0.00 5 44952.00 200
30049099 RPG ALDACTONE 50MG TAB 15# 37A26004 2/29 70.41 350 53.65 0.00 0.00 5 18777.50 350
21069099 SAM CALSIVIT FORTE TAB 10'S T2603165 8/27 220.00 690 167.62 0.00 19276.30 5 96381.50 575+115
300490 ZYD PANTODAC I V VIAL GAQ0141 3/28 54.21 2000 15.00 0.00 0.00 5 30000.00 2000
30049099 INT ATZ 50MG TAB 15'S T-2604196 3/28 181.57 90 138.34 0.00 0.00 5 12450.60 90
30049099 INT GABAPIN NT 100MG TAB 15'S N2600954 2/28 218.30 110 166.32 0.00 1663.20 5 16632.00 100+10
30049081 INT VALPROL CR 750MG TAB 10'S N2600656 2/28 213.40 20 162.59 0.00 0.00 5 3251.80 20
30049069 INT HIFENAC P TAB 15S K2600944 3/28 117.50 200 89.52 0.00 1790.40 5 16113.60 180+20
30042039 MAC LEVOMAC IV 100ML MC285713 9/27 209.86 100 159.90 0.00 7995.00 5 7995.00 50+50
8° STORAGE =====>
30043110 ERI XGLAR INJ 5ML V25BEH4008 8/28 893.27 20 714.61 0.00 0.00 5 14292.20 20
30043110 NOV HUMAN ACTRAPID 40IU 10ML B-70971 5/28 181.39 40 145.11 0.00 0.00 5 5804.40 40
30049099 INT LOPEZ INJ 2ML# KLLO6006 2/28 17.29 1000 13.17 0.00 0.00 5 13170.00 1000
SUB TOTAL 526072.56
G.Total 519234.00
MARG ERP NANO
"""

TRILOK_OCR = """
TAX INVOICE
TRILOK ENTERPRISES To:- WELLNESS FOREVER CHEMIST AND LIFESTYLE STORE
HSN MFG PRODUCT PACK BATCH EXP MRP QTY RATE Dis VALUE
30049039 GER CINTODAC CAP 10'S IA01746A 9/27 405.33 10 257.35 3.00 0.00 5 2573.50
MARG ERP NANO
"""


class TestMargNanoHsnMfgGstLineItems(unittest.TestCase):
    def test_detects_gst_nano_not_trilok(self):
        self.assertTrue(ocr_suggests_marg_nano_hsn_mfg_gst(GST_NANO_OCR, "LAXMI SALES"))
        self.assertFalse(ocr_suggests_marg_nano_hsn_mfg_gst(TRILOK_OCR, "TRILOK ENTERPRISES"))

    def test_parses_all_product_name_qty_rate(self):
        rows = _parse_marg_nano_hsn_mfg_gst_rows(GST_NANO_OCR)
        by_batch = {r["batch"]: r for r in rows}
        self.assertGreaterEqual(len(rows), 23)

        cobaren = by_batch["BHC2511038310"]
        self.assertEqual(cobaren["product_description"], "M COBAREN CAP 10'S")
        self.assertEqual(cobaren["quantity"], 100)
        self.assertAlmostEqual(cobaren["unit_price"], 156.96, places=2)
        self.assertFalse(cobaren.get("is_free"))

        tazar = by_batch["G272622"]
        self.assertEqual(tazar["product_description"], "TAZAR 4.5GM VIALS")
        self.assertEqual(tazar["quantity"], 300)
        self.assertAlmostEqual(tazar["unit_price"], 85.00, places=2)

        hair = by_batch["K4AIZ010"]
        self.assertEqual(hair["product_description"], "HAIRBLESS TAB 15'S")
        self.assertEqual(hair["mfg"], "MAN")
        self.assertEqual(hair["quantity"], 26)
        self.assertAlmostEqual(hair["unit_price"], 246.86, places=2)

        panto = by_batch["GAQ0141"]
        self.assertEqual(panto["product_description"], "PANTODAC I V VIAL")
        self.assertEqual(panto["quantity"], 2000)
        self.assertAlmostEqual(panto["unit_price"], 15.00, places=2)

        lopez = by_batch["KLLO6006"]
        self.assertEqual(lopez["product_description"], "LOPEZ INJ 2ML#")
        self.assertEqual(lopez["quantity"], 1000)
        self.assertAlmostEqual(lopez["unit_price"], 13.17, places=2)

        self.assertFalse(any("STORAGE" in r["product_description"].upper() for r in rows))

    def test_scheme_free_uses_paid_qty_and_rate_column(self):
        rows = _parse_marg_nano_hsn_mfg_gst_rows(GST_NANO_OCR)
        by_batch = {r["batch"]: r for r in rows}

        stiloz = by_batch["SZ1PP602"]
        self.assertEqual(stiloz["product_description"], "STILOZ 50MG TAB 15S")
        self.assertEqual(stiloz["quantity"], 20)
        self.assertEqual(stiloz["free_quantity"], 1)
        self.assertAlmostEqual(stiloz["unit_price"], 280.76, places=2)

        iguseas = by_batch["IGS05"]
        self.assertEqual(iguseas["quantity"], 120)
        self.assertEqual(iguseas["free_quantity"], 24)
        self.assertAlmostEqual(iguseas["unit_price"], 232.14, places=2)

        calsivit = by_batch["T2603165"]
        self.assertEqual(calsivit["quantity"], 575)
        self.assertEqual(calsivit["free_quantity"], 115)
        self.assertAlmostEqual(calsivit["unit_price"], 167.62, places=2)

        levomac = by_batch["MC285713"]
        self.assertEqual(levomac["quantity"], 50)
        self.assertEqual(levomac["free_quantity"], 50)
        self.assertAlmostEqual(levomac["unit_price"], 159.90, places=2)

        cil = by_batch["Q35Y005"]
        self.assertEqual(cil["product_description"], "CILAHEART 10MG TAB 15'S")
        self.assertEqual(cil["quantity"], 55)
        self.assertEqual(cil["free_quantity"], 5)
        self.assertAlmostEqual(cil["unit_price"], 120.58, places=2)

    def test_fix_restores_wrong_gemini_name_qty_rate(self):
        items = [
            {
                "product_description": "COBAREN CAP 10'S",
                "quantity": "100",
                "unit_price": "156.96",
                "total_amount": "15696.00",
                "lot_batch_number": "BHC2511038310",
            },
            {
                "product_description": "TAZAR 4.5GM",
                "quantity": "300",
                "unit_price": "85.00",
                "total_amount": "25500.00",
                "lot_batch_number": "G272622",
            },
            {
                "product_description": "MANHAIRBLESS TAB 15'S",
                "quantity": "26",
                "unit_price": "246.86",
                "total_amount": "6418.36",
                "lot_batch_number": "K4AIZ010",
            },
            {
                "product_description": "MANCILAHEART 10MG TAB 15'S",
                "quantity": "60",
                "unit_price": "120.58",
                "total_amount": "6631.90",
                "lot_batch_number": "Q35Y005",
                "additional_fields": {"free_quantity": "0"},
            },
            {
                "product_description": "IGUSEAS TAB 10'S",
                "quantity": "144",
                "unit_price": "193.45",
                "total_amount": "27856.80",
                "lot_batch_number": "IGS05",
            },
            {
                "product_description": "CALSIVIT FORTE TAB 10'S",
                "quantity": "690",
                "unit_price": "139.68",
                "total_amount": "96381.50",
                "lot_batch_number": "T2603165",
            },
            {
                "product_description": "PANTODAC I V",
                "quantity": "2000",
                "unit_price": "15.00",
                "total_amount": "30000.00",
                "lot_batch_number": "GAQ0141",
            },
            {
                "product_description": "STILOZ 50MG TAB 15S",
                "quantity": "21",
                "unit_price": "280.76",
                "total_amount": "5615.20",
                "lot_batch_number": "SZ1PP602",
            },
            {
                "product_description": "LEVOMAC IV 100ML",
                "quantity": "50",
                "unit_price": "159.90",
                "total_amount": "7995.00",
                "lot_batch_number": "MC285713",
            },
        ]
        out = fix_marg_nano_hsn_mfg_gst_line_items_from_ocr(
            items, GST_NANO_OCR, "LAXMI SALES PHARMACEUTICALS")
        by = {i["lot_batch_number"]: i for i in out}

        self.assertEqual(by["BHC2511038310"]["product_description"], "M COBAREN CAP 10'S")
        self.assertEqual(by["BHC2511038310"]["quantity"], "100")
        self.assertEqual(by["BHC2511038310"]["unit_price"], "156.96")
        self.assertEqual(by["BHC2511038310"]["total_amount"], "15696.00")

        self.assertEqual(by["G272622"]["product_description"], "TAZAR 4.5GM VIALS")
        self.assertEqual(by["K4AIZ010"]["product_description"], "HAIRBLESS TAB 15'S")
        self.assertEqual(by["GAQ0141"]["product_description"], "PANTODAC I V VIAL")

        self.assertEqual(by["Q35Y005"]["product_description"], "CILAHEART 10MG TAB 15'S")
        self.assertEqual(by["Q35Y005"]["quantity"], "55")
        self.assertEqual(by["Q35Y005"]["unit_price"], "120.58")
        self.assertEqual(by["Q35Y005"]["additional_fields"]["free_quantity"], "5")
        self.assertEqual(by["Q35Y005"]["total_amount"], "6631.90")

        self.assertEqual(by["IGS05"]["quantity"], "120")
        self.assertEqual(by["IGS05"]["unit_price"], "232.14")
        self.assertEqual(by["IGS05"]["additional_fields"]["free_quantity"], "24")

        self.assertEqual(by["T2603165"]["quantity"], "575")
        self.assertEqual(by["T2603165"]["unit_price"], "167.62")

        self.assertEqual(by["SZ1PP602"]["quantity"], "20")
        self.assertEqual(by["SZ1PP602"]["unit_price"], "280.76")
        self.assertEqual(by["SZ1PP602"]["additional_fields"]["free_quantity"], "1")

        self.assertEqual(by["MC285713"]["quantity"], "50")
        self.assertEqual(by["MC285713"]["unit_price"], "159.90")

    def test_does_not_change_unrelated_format(self):
        items = [{
            "product_description": "SOME TAB",
            "quantity": "10",
            "unit_price": "12.00",
            "total_amount": "120.00",
        }]
        out = fix_marg_nano_hsn_mfg_gst_line_items_from_ocr(
            items, "SUPREME LIFE SCIENCES MARG ERP", "SUPREME LIFE SCIENCES")
        self.assertEqual(out[0]["product_description"], "SOME TAB")
        self.assertEqual(out[0]["quantity"], "10")
        self.assertEqual(out[0]["unit_price"], "12.00")

    def test_does_not_change_trilok(self):
        items = [{
            "product_description": "GER CINTODAC CAP 10'S",
            "quantity": "10",
            "unit_price": "257.35",
            "lot_batch_number": "IA01746A",
        }]
        out = fix_marg_nano_hsn_mfg_gst_line_items_from_ocr(items, TRILOK_OCR)
        self.assertEqual(out[0]["product_description"], "GER CINTODAC CAP 10'S")
        self.assertEqual(out[0]["quantity"], "10")
        self.assertEqual(out[0]["unit_price"], "257.35")


if __name__ == "__main__":
    unittest.main()
