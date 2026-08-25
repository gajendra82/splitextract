"""SmartPharma360 (ALARIC): Qty/Free/Rate columns vs shared MRP/qty-misread."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _parse_smartpharma360_qty_rate_serialized,
    fix_mrp_as_unit_price,
    fix_smartpharma360_qty_rate_from_ocr,
    ocr_suggests_smartpharma360_qty_rate,
)


SP360_OCR = """
ALARIC ENTERPRISES TAX INVOICE
Inv. No. : INDEL2627008376
SNO HSN MFG Product Name Pack Batch No Expiry Qty Free Rate MRP Amount Disc% Net
Powered by Smartpharma360 || www.smartpharma360.in
"""

DANG_OCR = """
DANG MEDICALS
BILL No:- 26-27/1412 GST INVOICE
SNO. Qty+Free Pack ItemName BATCH EXP. HSN Rate AMOUNT
"""

TABLE_OCR = """
SP360\tBIOVORIN 50MG INJECTION\t\t\t2650.00
SP360\tDABAZ 200MG INJECTION\t\t235.56\t
SP360\tDABAZ 200MG INJECTION\t\t235.56\t1177.80
SP360\tDABAZ 500MG INJECTION\t\t370.17\t2591.19
SP360\tDABAZ 500MG INJECTION\t\t370.17\t1110.51
SP360\tDOCETERE 20MG INJECTION\t\t\t2200.00
SP360\tNANOXEL 100MG INJECTION\t\t4140.00\t20700.00
SP360\tNANOXEL 30MG INJECTION\t\t1500.00\t7500.00
SP360\tPACLIALL 100MG INJECTION\t\t1600.00\t19200.00
SP360\tREDITUX\t\t\t5250.00
SP360\tREDITUX\t\t5250.00\t15750.00
SP360\tREDITUX\t\t\t13300.00
SP360\tDOCEAQUALIP 20MG INJECTION\t\t2100.00\t12600.00
SP360\tTRASTUREL 440MG INJECTION\t\t40919.90\t40919.90
SP360\tONCOFLUOR 500MG INJECTION\t40\t200.40\t816.00
SP360\tELEFTHA 440MG INJECTION\t\t6800.00\t13600.00
"""


class TestSmartpharma360QtyRate(unittest.TestCase):
    def test_detects_alaric_not_dang(self):
        self.assertTrue(ocr_suggests_smartpharma360_qty_rate(SP360_OCR, "ALARIC ENTERPRISES"))
        self.assertFalse(ocr_suggests_smartpharma360_qty_rate(DANG_OCR, "DANG MEDICALS"))

    def test_parses_serialized_column_rows(self):
        rows = _parse_smartpharma360_qty_rate_serialized(TABLE_OCR)
        nanoxel = next(r for r in rows if "NANOXEL 100" in r["product_description"].upper())
        self.assertAlmostEqual(nanoxel["unit_price"], 4140.0, places=2)
        self.assertAlmostEqual(nanoxel["total_amount"], 20700.0, places=2)

    def test_skips_shared_mrp_qty_misread(self):
        item = {
            "product_description": "NANOXEL 30MG INJECTION",
            "quantity": "5",
            "unit_price": "1500.00",
            "total_amount": "4524.00",
        }
        out = fix_mrp_as_unit_price(item, vendor="ALARIC ENTERPRISES", ocr_text=SP360_OCR)
        self.assertEqual(out["quantity"], "5")
        self.assertEqual(out["unit_price"], "1500.00")

    def test_fixes_amount_as_rate_and_leaves_correct_rows(self):
        items = [
            {"product_description": "BIOVORIN 50MG INJECTION",
             "quantity": "50", "unit_price": "265.00", "total_amount": "4671.20"},
            {"product_description": "DABAZ 200MG INJECTION",
             "quantity": "1", "unit_price": "235.56", "total_amount": "235.56"},
            {"product_description": "DABAZ 200MG INJECTION",
             "quantity": "5", "unit_price": "1177.80", "total_amount": "1177.80"},
            {"product_description": "DOCETERE 20MG INJECTION",
             "quantity": "4", "unit_price": "2200.00", "total_amount": "2200.00"},
            {"product_description": "NANOXEL 100MG INJECTION",
             "quantity": "5", "unit_price": "20700.00", "total_amount": "4120865.00"},
            {"product_description": "NANOXEL 30MG INJECTION",
             "quantity": "5", "unit_price": "904.80", "total_amount": "4524.00"},
            {"product_description": "PACLIALL 100MG INJECTION",
             "quantity": "12", "unit_price": "1600.00", "total_amount": "19200.00"},
            {"product_description": "REDITUX 500MG INJECTION",
             "quantity": "1", "unit_price": "5250.00", "total_amount": "5250.00"},
            {"product_description": "REDITUX 500MG INJECTION",
             "quantity": "3", "unit_price": "5250.00", "total_amount": "15750.00"},
            {"product_description": "IV SET BEXTER (FOC)",
             "quantity": "5", "unit_price": "0.10", "total_amount": "0.10"},
            {"product_description": "DOCEAQUALIP 20MG INJECTION",
             "quantity": "6", "unit_price": "2100.00", "total_amount": "72600.00"},
            {"product_description": "TRASTUREL 440MG INJECTION",
             "quantity": "5", "unit_price": "4099.90", "total_amount": "40919.90"},
            {"product_description": "ONCOFLUOR 500MG INJECTION",
             "quantity": "40", "unit_price": "20.40", "total_amount": "816.00"},
            {"product_description": "ELEFTHA 440MG INJECTION",
             "quantity": "2", "unit_price": "6800.00", "total_amount": "13600.00"},
        ]
        out = fix_smartpharma360_qty_rate_from_ocr(
            items, SP360_OCR, TABLE_OCR, "ALARIC ENTERPRISES")
        by_name = {}
        for it in out:
            by_name.setdefault(it["product_description"], []).append(it)

        bio = by_name["BIOVORIN 50MG INJECTION"][0]
        self.assertEqual(bio["quantity"], "50")
        self.assertAlmostEqual(float(bio["unit_price"]), 53.0, places=2)

        dabaz = by_name["DABAZ 200MG INJECTION"]
        self.assertEqual(dabaz[0]["quantity"], "1")
        self.assertAlmostEqual(float(dabaz[0]["unit_price"]), 235.56, places=2)
        self.assertEqual(dabaz[1]["quantity"], "5")
        self.assertAlmostEqual(float(dabaz[1]["unit_price"]), 235.56, places=2)

        doc = by_name["DOCETERE 20MG INJECTION"][0]
        self.assertEqual(doc["quantity"], "4")
        self.assertAlmostEqual(float(doc["unit_price"]), 550.0, places=2)

        n100 = by_name["NANOXEL 100MG INJECTION"][0]
        self.assertEqual(n100["quantity"], "5")
        self.assertAlmostEqual(float(n100["unit_price"]), 4140.0, places=2)

        n30 = by_name["NANOXEL 30MG INJECTION"][0]
        self.assertEqual(n30["quantity"], "5")
        self.assertAlmostEqual(float(n30["unit_price"]), 1500.0, places=2)

        pac = by_name["PACLIALL 100MG INJECTION"][0]
        self.assertEqual(pac["quantity"], "12")
        self.assertAlmostEqual(float(pac["unit_price"]), 1600.0, places=2)

        r500 = by_name["REDITUX 500MG INJECTION"]
        self.assertEqual(r500[0]["quantity"], "1")
        self.assertAlmostEqual(float(r500[0]["unit_price"]), 5250.0, places=2)
        self.assertEqual(r500[1]["quantity"], "3")
        self.assertAlmostEqual(float(r500[1]["unit_price"]), 5250.0, places=2)

        foc = by_name["IV SET BEXTER (FOC)"][0]
        self.assertEqual(foc["quantity"], "5")
        self.assertAlmostEqual(float(foc["unit_price"]), 0.02, places=2)

        d20 = by_name["DOCEAQUALIP 20MG INJECTION"][0]
        self.assertEqual(d20["quantity"], "6")
        self.assertAlmostEqual(float(d20["unit_price"]), 2100.0, places=2)

        tra = by_name["TRASTUREL 440MG INJECTION"][0]
        self.assertEqual(tra["quantity"], "1")
        self.assertAlmostEqual(float(tra["unit_price"]), 40919.90, places=2)

        onc = by_name["ONCOFLUOR 500MG INJECTION"][0]
        self.assertEqual(onc["quantity"], "40")
        self.assertAlmostEqual(float(onc["unit_price"]), 20.40, places=2)

        ele = by_name["ELEFTHA 440MG INJECTION"][0]
        self.assertEqual(ele["quantity"], "2")
        self.assertAlmostEqual(float(ele["unit_price"]), 6800.0, places=2)

    def test_does_not_run_on_other_formats(self):
        items = [{
            "product_description": "FORGLYN PLUS INH",
            "quantity": "10",
            "unit_price": "783.25",
            "total_amount": "8224.12",
        }]
        out = fix_smartpharma360_qty_rate_from_ocr(
            items, DANG_OCR, TABLE_OCR, "DANG MEDICALS")
        self.assertEqual(out[0]["unit_price"], "783.25")
        self.assertEqual(out[0]["quantity"], "10")


if __name__ == "__main__":
    unittest.main()
