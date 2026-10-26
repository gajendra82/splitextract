"""LIFE LINE CARE: unswap integer RATE values that Pattern 1 put in Quantity."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _extract_line_items_for_validation,
    _parse_life_line_care_qty_rate_rows,
    enforce_schema,
    fix_life_line_care_swapped_qty_rate_from_ocr,
    fix_swapped_quantity_unit_price,
    ocr_suggests_life_line_care,
)


LIFE_LINE_OCR = """
LIFE LINE CARE TAX INVOICE Party Name
YASHODA PHARMACARE PVT LTD
Invoice No. W26-27/01318
Invoice Date 16/06/2026
SNO. HSN PRODUCT DESCRIPTION PACK BATCH Exp QTY FREE RATE MRP DISCOUNT CGST SGST AMOUNT
CODE (%) AMT. (%) AMT. (%) AMT.
1 30049099 KABILYTE 500 UNIT 82WB689602 01-28 60.00 0.00 151.43 388.00 0.00 0.00 2.50 227.15 2.50 227.15 9540.10
2 30049099 ADEGRON-50 TAB TABLET MFT-250861 07-27 60.00 0.00 225.00 384.38 0.00 0.00 2.50 337.50 2.50 337.50 14175.00
3 21069099 COLNIVEST TAB TABLET 2515EBA932 08-27 60.00 0.00 340.00 612.00 4.00 816.00 2.50 489.60 2.50 489.60 20563.20
4 21069099 MEDNOCAL TAB TABLET T260307 07-27 200.00 0.00 131.20 260.00 4.00 1,049.60 2.50 629.76 2.50 629.76 26449.92
5 30049039 LAXOPEG POWDER 17GM UNIT W26C15 02-28 100.00 10.00 37.34 49.00 5.00 186.70 2.50 88.68 2.50 88.68 3724.66
6 30049094 FLUTICONE FT NASAL UNIT AFC1052 03-28 15.00 0.00 219.81 554.81 0.00 0.00 2.50 82.43 2.50 82.43 3462.01
7 30049099 COMPLAMINA TAB 150MG TABLET SB00070A 12-28 20.00 0.00 34.99 52.18 0.00 0.00 2.50 17.50 2.50 17.50 734.80
TOTAL ITEM:- 7
TOTAL QTY:- 515.00
TOTAL AMOUNT
78650.00
"""

LIFE_LINE_01341_OCR = """
LIFE LINE CARE TAX INVOICE Party Name
YASHODA PHARMACARE PVT LTD
Invoice No. W26-27/01341
Invoice Date 17/06/2026
SNO. HSN PRODUCT DESCRIPTION PACK BATCH Exp QTY FREE RATE MRP DISCOUNT CGST SGST AMOUNT
CODE (%) AMT. (%) AMT. (%) AMT.
1 30049099 VELPANAT 400 MG+100MG BOTTEL 1950223 10-28 3.00 3.00 8000.00 16406.25 0.00 0.00 2.50 600.00 2.50 600.00 25200.00
2 30021500 TENECTASE 20 MG UNIT B112606N08 03-28 6.00 0.00 22438.00 41498.00 0.00 0.00 2.50 3365.70 2.50 3365.70 141359.40
3 30049099 NEPHROSTERIL 250ML BOTTLE 16UM5595 11-28 30.00 0.00 400.00 713.00 0.00 0.00 2.50 300.00 2.50 300.00 12600.00
4 30021500 DARBEREL 40MCG PFS UNIT DPSIB25016 11-28 30.00 0.00 660.00 2798.00 0.00 0.00 2.50 495.00 2.50 495.00 20790.00
5 19019010 POLYRICH DF (NEW) UNIT P26HDF18EB 10-27 15.00 3.00 288.93 490.00 0.00 0.00 2.50 108.35 2.50 108.35 4550.65
6 30042099 PUROCIN OIN UNIT F3507 04-28 25.00 5.00 243.00 346.00 0.00 0.00 2.50 151.88 2.50 151.88 6378.76
7 30049099 LINID-12X10 TAB TABLET IB00541A 02-28 24.00 0.00 241.91 381.04 0.00 0.00 2.50 145.15 2.50 145.15 6096.14
TOTAL ITEM:- 7
TOTAL QTY:- 133.00
TOTAL AMOUNT
216975.00
"""

DANG_OCR = """
DANG MEDICALS
BILL No:- 26-27/1412 GST INVOICE BILL DATE:- 09/06/2026
SNO. Qty+Free Pack ItemName BATCH EXP. HSN O.MRP MRP Rate Disc CGST SGST AMOUNT
1 10.00+0.00 1UNIT FORGLYN PLUS INH. IB00881A 03-28 300490 0.00 1,028.02 783.25 0.00 2.50 2.50 8224.12
TOTAL AMOUNT 37,305.00
"""


class TestLifeLineCareQtyRateSwap(unittest.TestCase):
    def test_detects_life_line_not_other_formats(self):
        self.assertTrue(ocr_suggests_life_line_care(LIFE_LINE_OCR))
        self.assertFalse(ocr_suggests_life_line_care(DANG_OCR))
        self.assertFalse(ocr_suggests_life_line_care("PRAKASH MEDICAL STORES\nMFAC"))

    def test_parses_qty_and_rate_columns(self):
        rows = _parse_life_line_care_qty_rate_rows(LIFE_LINE_OCR)
        self.assertEqual(len(rows), 7)
        adeg = next(r for r in rows if "ADEGRON" in r["product_description"].upper())
        self.assertEqual(adeg["quantity"], 60.0)
        self.assertAlmostEqual(adeg["unit_price"], 225.0, places=2)
        coln = next(r for r in rows if "COLNIVEST" in r["product_description"].upper())
        self.assertEqual(coln["quantity"], 60.0)
        self.assertAlmostEqual(coln["unit_price"], 340.0, places=2)

    def test_unswaps_only_inverted_integer_rate_rows(self):
        items = [
            {
                "product_description": "KABILYTE 500 UNIT",
                "quantity": "60.00",
                "unit_price": "151.43",
                "total_amount": "9540.10",
                "lot_batch_number": "82WB689602",
            },
            {
                "product_description": "ADEGRON-50 TAB TABLET",
                "quantity": "225",
                "unit_price": "60.00",
                "total_amount": "14175.00",
                "lot_batch_number": "MFT-250861",
            },
            {
                "product_description": "COLNIVEST TAB TABLET",
                "quantity": "340",
                "unit_price": "60.00",
                "total_amount": "20563.20",
                "lot_batch_number": "2515EBA932",
            },
            {
                "product_description": "MEDNOCAL TAB TABLET",
                "quantity": "200.00",
                "unit_price": "131.20",
                "total_amount": "26449.92",
                "lot_batch_number": "T260307",
            },
        ]
        fixed = fix_life_line_care_swapped_qty_rate_from_ocr(items, LIFE_LINE_OCR)
        kabi = next(i for i in fixed if "KABILYTE" in i["product_description"].upper())
        adeg = next(i for i in fixed if "ADEGRON" in i["product_description"].upper())
        coln = next(i for i in fixed if "COLNIVEST" in i["product_description"].upper())
        medn = next(i for i in fixed if "MEDNOCAL" in i["product_description"].upper())
        self.assertEqual(kabi["quantity"], "60.00")
        self.assertEqual(kabi["unit_price"], "151.43")
        self.assertEqual(kabi["total_amount"], "9540.10")
        self.assertEqual(adeg["quantity"], "60.00")
        self.assertEqual(adeg["unit_price"], "225")
        self.assertEqual(adeg["total_amount"], "14175.00")
        self.assertEqual(coln["quantity"], "60.00")
        self.assertEqual(coln["unit_price"], "340")
        self.assertEqual(coln["total_amount"], "20563.20")
        self.assertEqual(medn["quantity"], "200.00")
        self.assertEqual(medn["unit_price"], "131.20")

    def test_shared_pattern1_swaps_then_enforce_unswaps(self):
        item = {
            "product_description": "ADEGRON-50 TAB TABLET",
            "quantity": "60.00",
            "unit_price": "225.00",
            "total_amount": "14175.00",
            "lot_batch_number": "MFT-250861",
        }
        swapped = fix_swapped_quantity_unit_price(dict(item))
        self.assertEqual(str(swapped["quantity"]), "225")
        self.assertEqual(swapped["unit_price"], "60.00")

        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "LIFE LINE CARE",
                    "customer": "YASHODA PHARMACARE PVT LTD",
                    "invoice_no": "W26-27/01318",
                    "total": "78650.00",
                },
                "line_items": {
                    "items": [
                        dict(item),
                        {
                            "product_description": "KABILYTE 500 UNIT",
                            "quantity": "60.00",
                            "unit_price": "151.43",
                            "total_amount": "9540.10",
                            "lot_batch_number": "82WB689602",
                        },
                    ],
                    "count": 2,
                },
                "ocr_text": LIFE_LINE_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        adeg = next(i for i in out if "ADEGRON" in i["product_description"].upper())
        kabi = next(i for i in out if "KABILYTE" in i["product_description"].upper())
        self.assertEqual(float(adeg["quantity"]), 60.0)
        self.assertAlmostEqual(float(adeg["unit_price"]), 225.0, places=2)
        self.assertEqual(adeg["total_amount"], "14175.00")
        self.assertEqual(float(kabi["quantity"]), 60.0)
        self.assertAlmostEqual(float(kabi["unit_price"]), 151.43, places=2)
        self.assertEqual(kabi["total_amount"], "9540.10")

    def test_parses_bottel_pack_and_high_integer_rates(self):
        rows = _parse_life_line_care_qty_rate_rows(LIFE_LINE_01341_OCR)
        self.assertEqual(len(rows), 7)
        velp = next(r for r in rows if "VELPANAT" in r["product_description"].upper())
        tene = next(r for r in rows if "TENECTASE" in r["product_description"].upper())
        self.assertEqual(velp["quantity"], 3.0)
        self.assertAlmostEqual(velp["unit_price"], 8000.0, places=2)
        self.assertEqual(tene["quantity"], 6.0)
        self.assertAlmostEqual(tene["unit_price"], 22438.0, places=2)

    def test_unswaps_velpanat_and_tenectase_gst_drift(self):
        items = [
            {
                "product_description": "VELPANAT 400 MG+100MG",
                "quantity": "8000",
                "unit_price": "3.00",
                "total_amount": "25200.00",
                "lot_batch_number": "1950223",
            },
            {
                "product_description": "TENECTASE 20 MG",
                "quantity": "22438",
                "unit_price": "6.30",
                "total_amount": "141359.40",
                "lot_batch_number": "B112606N08",
            },
            {
                "product_description": "POLYRICH DF (NEW)",
                "quantity": "15.00",
                "unit_price": "288.93",
                "total_amount": "4550.65",
                "lot_batch_number": "P26HDF18EB",
            },
        ]
        fixed = fix_life_line_care_swapped_qty_rate_from_ocr(
            items, LIFE_LINE_01341_OCR)
        velp = next(i for i in fixed if "VELPANAT" in i["product_description"].upper())
        tene = next(i for i in fixed if "TENECTASE" in i["product_description"].upper())
        poly = next(i for i in fixed if "POLYRICH" in i["product_description"].upper())
        self.assertEqual(float(velp["quantity"]), 3.0)
        self.assertEqual(str(velp["unit_price"]), "8000")
        self.assertEqual(velp["total_amount"], "25200.00")
        self.assertEqual(float(tene["quantity"]), 6.0)
        self.assertEqual(str(tene["unit_price"]), "22438")
        self.assertEqual(tene["total_amount"], "141359.40")
        self.assertEqual(poly["quantity"], "15.00")
        self.assertEqual(poly["unit_price"], "288.93")
        self.assertEqual(poly["total_amount"], "4550.65")

    def test_parses_bag_pack_smofkabiven_qty_rate(self):
        ocr = """
LIFE LINE CARE TAX INVOICE
SNO. HSN PRODUCT DESCRIPTION PACK BATCH Exp QTY FREE RATE
4 30049099 SMOFKABIVEN PERI 1448 ML BAG 10UL5468 10-27 4.00 0.00 2750.00 4783.00 0.00 0.00 2.50 275.00 2.50 275.00 11550.00
13 30049099 SMOFKABIVEN 986ML BAG 10WA7316 12-27 20.00 0.00 3100.00 4985.50 0.00 0.00 2.50 1550.00 2.50 1550.00 65100.00
TOTAL ITEM:- 13
"""
        self.assertTrue(ocr_suggests_life_line_care(ocr))
        rows = _parse_life_line_care_qty_rate_rows(ocr)
        peri = next(r for r in rows if "PERI" in r["product_description"].upper())
        smof = next(r for r in rows if "986" in r["product_description"].upper())
        self.assertEqual(peri["quantity"], 4.0)
        self.assertAlmostEqual(peri["unit_price"], 2750.0, places=2)
        self.assertEqual(smof["quantity"], 20.0)
        self.assertAlmostEqual(smof["unit_price"], 3100.0, places=2)

        items = [
            {
                "product_description": "SMOFKABIVEN PERI 1448 ML BAG",
                "quantity": "2750",
                "unit_price": "4.00",
                "total_amount": "11550.00",
                "lot_batch_number": "10UL5468",
            },
            {
                "product_description": "SMOFKABIVEN 986ML BAG",
                "quantity": "3100",
                "unit_price": "20.00",
                "total_amount": "65100.00",
                "lot_batch_number": "10WA7316",
            },
            {
                "product_description": "SOLUPERZ TAB TABLET",
                "quantity": "20.00",
                "unit_price": "475",
                "total_amount": "9975.00",
                "lot_batch_number": "MT261513",
            },
        ]
        fixed = fix_life_line_care_swapped_qty_rate_from_ocr(items, ocr)
        peri_i = next(i for i in fixed if "PERI" in i["product_description"].upper())
        smof_i = next(i for i in fixed if "986" in i["product_description"].upper())
        solu = next(i for i in fixed if "SOLUPERZ" in i["product_description"].upper())
        self.assertEqual(float(peri_i["quantity"]), 4.0)
        self.assertEqual(str(peri_i["unit_price"]), "2750")
        self.assertEqual(peri_i["total_amount"], "11550.00")
        self.assertEqual(float(smof_i["quantity"]), 20.0)
        self.assertEqual(str(smof_i["unit_price"]), "3100")
        self.assertEqual(smof_i["total_amount"], "65100.00")
        self.assertEqual(solu["quantity"], "20.00")
        self.assertEqual(solu["unit_price"], "475")

    def test_restores_qty_rate_when_cgst_columns_used(self):
        ocr = """
LIFE LINE CARE TAX INVOICE
SNO. HSN PRODUCT DESCRIPTION PACK BATCH Exp QTY FREE RATE
1 30049099 NASOCLEAR 20ML UNIT DBC1031 02-29 300.00 0.00 55.60 72.98 0.00 0.00 2.50 417.00 2.50 417.00 17514.00
6 30049099 OCID-20MG CAP TABLET IB00105A 09-27 120.00 0.00 39.79 61.30 0.00 0.00 2.50 119.37 2.50 119.37 5013.54
7 30049099 NUPATCH 20MG PATCH UNIT CNC1011 01-28 100.00 0.00 132.40 195.05 0.00 0.00 2.50 331.00 2.50 331.00 13902.00
8 30049099 NUCOXIA RELAX TAB TABLET BRB1123 04-27 5.00 0.00 392.08 514.60 0.00 0.00 2.50 49.01 2.50 49.01 2058.42
TOTAL ITEM:- 10
"""
        items = [
            {
                "product_description": "NASOCLEAR 20ML",
                "quantity": "300",
                "unit_price": "1.39",
                "total_amount": "417.00",
                "lot_batch_number": "DBC1031",
            },
            {
                "product_description": "OCID-20MG CAP",
                "quantity": "3",
                "unit_price": "39.79",
                "total_amount": "119.37",
                "lot_batch_number": "IB00105A",
            },
            {
                "product_description": "NUPATCH 20MG PATCH",
                "quantity": "2.50",
                "unit_price": "132.40",
                "total_amount": "331.00",
                "lot_batch_number": "CNC1011",
            },
            {
                "product_description": "NUCOXIA RELAX TAB",
                "quantity": "5.00",
                "unit_price": "9.80",
                "total_amount": "49.01",
                "lot_batch_number": "BRB1123",
            },
        ]
        fixed = fix_life_line_care_swapped_qty_rate_from_ocr(items, ocr)
        naso = next(i for i in fixed if "NASOCLEAR" in i["product_description"].upper())
        ocid = next(i for i in fixed if "OCID" in i["product_description"].upper())
        nupa = next(i for i in fixed if "NUPATCH" in i["product_description"].upper())
        rela = next(i for i in fixed if "RELAX" in i["product_description"].upper())
        self.assertEqual(float(naso["quantity"]), 300.0)
        self.assertAlmostEqual(float(naso["unit_price"]), 55.60, places=2)
        self.assertEqual(naso["total_amount"], "417.00")
        self.assertEqual(float(ocid["quantity"]), 120.0)
        self.assertEqual(ocid["unit_price"], "39.79")
        self.assertEqual(ocid["total_amount"], "119.37")
        self.assertEqual(float(nupa["quantity"]), 100.0)
        self.assertEqual(nupa["unit_price"], "132.40")
        self.assertEqual(float(rela["quantity"]), 5.0)
        self.assertAlmostEqual(float(rela["unit_price"]), 392.08, places=2)

    def test_parses_box_pack_ranimac_qty_rate(self):
        ocr = """
LIFE LINE CARE TAX INVOICE
SNO. HSN PRODUCT DESCRIPTION PACK BATCH Exp QTY FREE RATE
1 30049099 RANIMAC 2ML BOX MA25C57 02-27 4.00 0.00 140.00 282.19 0.00 0.00 2.50 14.00 2.50 14.00 588.00
2 30049099 ATORVA-40MG TAB TABLET IB00787A 03-29 10.00 0.00 120.96 207.50 0.00 0.00 2.50 30.24 2.50 30.24 1270.08
3 30049099 BILYPSA TAB TABLET IB000068A 06-28 20.00 0.00 381.82 501.13 5.00 381.82 2.50 181.36 2.50 181.36 7617.30
TOTAL ITEM:- 3
"""
        self.assertTrue(ocr_suggests_life_line_care(ocr))
        rows = _parse_life_line_care_qty_rate_rows(ocr)
        self.assertEqual(len(rows), 3)
        rani = next(r for r in rows if "RANIMAC" in r["product_description"].upper())
        self.assertEqual(rani["quantity"], 4.0)
        self.assertAlmostEqual(rani["unit_price"], 140.0, places=2)

        items = [
            {
                "product_description": "RANIMAC 2ML",
                "quantity": "140",
                "unit_price": "4.00",
                "total_amount": "588.00",
                "lot_batch_number": "MA25C57",
            },
            {
                "product_description": "ATORVA-40MG TAB",
                "quantity": "10.00",
                "unit_price": "120.96",
                "total_amount": "1270.08",
                "lot_batch_number": "IB00787A",
            },
            {
                "product_description": "BILYPSA TAB",
                "quantity": "20.00",
                "unit_price": "381.82",
                "total_amount": "7617.30",
                "lot_batch_number": "IB000068A",
            },
        ]
        fixed = fix_life_line_care_swapped_qty_rate_from_ocr(items, ocr)
        rani_i = next(i for i in fixed if "RANIMAC" in i["product_description"].upper())
        ator = next(i for i in fixed if "ATORVA" in i["product_description"].upper())
        bily = next(i for i in fixed if "BILYPSA" in i["product_description"].upper())
        self.assertEqual(float(rani_i["quantity"]), 4.0)
        self.assertEqual(str(rani_i["unit_price"]), "140")
        self.assertEqual(rani_i["total_amount"], "588.00")
        self.assertEqual(ator["quantity"], "10.00")
        self.assertEqual(ator["unit_price"], "120.96")
        self.assertEqual(bily["quantity"], "20.00")
        self.assertEqual(bily["unit_price"], "381.82")

    def test_parses_batch_with_slash_mg_cart(self):
        ocr = """
LIFE LINE CARE TAX INVOICE
SNO. HSN PRODUCT DESCRIPTION PACK BATCH Exp QTY FREE RATE
5 21069099 MG CART TAB TABLET MCT/A2605790 04-28 60.00 0.00 174.00 340.00 4.76 496.94 2.50 248.58 2.50 248.58 10440.22
4 21069099 MEDNOVIT TAB TABLET 2515EB445 08-27 120.00 0.00 225.00 445.00 4.00 1,080.00 2.50 648.00 2.50 648.00 27216.00
TOTAL ITEM:- 11
"""
        rows = _parse_life_line_care_qty_rate_rows(ocr)
        mg = next(r for r in rows if "MG CART" in r["product_description"].upper())
        self.assertEqual(mg["quantity"], 60.0)
        self.assertAlmostEqual(mg["unit_price"], 174.0, places=2)
        self.assertEqual(mg["lot_batch_number"].upper(), "MCT/A2605790")

        items = [
            {
                "product_description": "MG CART TAB",
                "quantity": "174",
                "unit_price": "60.00",
                "total_amount": "10440.22",
                "lot_batch_number": "MCT/A2605790",
            },
            {
                "product_description": "MEDNOVIT TAB",
                "quantity": "120.00",
                "unit_price": "225",
                "total_amount": "27216.00",
                "lot_batch_number": "2515EB445",
            },
        ]
        fixed = fix_life_line_care_swapped_qty_rate_from_ocr(items, ocr)
        mg_i = next(i for i in fixed if "MG CART" in i["product_description"].upper())
        med = next(i for i in fixed if "MEDNOVIT" in i["product_description"].upper())
        self.assertEqual(float(mg_i["quantity"]), 60.0)
        self.assertEqual(str(mg_i["unit_price"]), "174")
        self.assertEqual(mg_i["total_amount"], "10440.22")
        self.assertEqual(med["quantity"], "120.00")
        self.assertEqual(med["unit_price"], "225")


if __name__ == "__main__":
    unittest.main()
