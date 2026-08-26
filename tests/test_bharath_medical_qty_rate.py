"""Bharath Medical stockist table: Qty before Pack, Rate after HSN."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _parse_bharath_medical_qty_rate_rows,
    fix_bharath_medical_qty_rate_from_ocr,
    ocr_suggests_bharath_medical,
)


BHARATH_OCR = r"""
Bharath Medical and General Agencies TAX INVOICE
Bill No. : 417065
72 Oty Pack lem Description Batch Ex,Dt. New MRP HSN Rate Dis% Sch% GrossVaiue
7 2 2010S” # BEMDAC TAB euv260s45 0128 299.47 30049099 16.09.00
5 150 15's # ATORVA 20mg. IADI346B 08-28 201.25 30049099 321.58 7320
Epics 10 10's 4 ATORVA 80mg (BOOSI4B 02-28 433.49 30049009 261.80 2618.00
ooh 50 10's # CLOPITORVA 20mg CAP 1B00722A 02-28 392.54 30045099 241.38
--- Page 2 ---
Rack MFR. Qty. Pg. Item Description Batch MRP HSN Rate Disc Sch% Value GST GSTAmt
EMEA 20 10'S w BEMDAC TAB EMVZ60%5 OF28 299.47 30099099 186.69 5.00 000 3733.80 5.00 17.36
150 15's #ATORVA20mg 1A013468 08-28 201.25 30049099 121.55 18232,50 5.00 866.04
10 15's #ATORVA Smeg 1BOOTBSA 93-29 160.62 30049079 89.72 5.00 897.20 5.00 42.62
10 10's | HATORVA 80mg IBOOSI4B 02-28 433.49 30069099 261.80 5.00 2618.00 8.00 124.36
10 10's | #ATORVAE Tab SBOO193A 02-28 416.42 30049079 232.62 2326.20 5.00 710.50
Reus SO 15's # CLOPITORVA 10mg CAP 18007424 02-28 426.46 30049079 238.26 090 1913.00 565.86
50 10's A CLOPITORVA 20mg CAP 18007224 02-28 392.84 30049099 241.38 12069.00 5.00 573.28
betes 30 10's | # CLOPITORVA 40mg CAP cpR26002 03-28 390.23 30039099 217.97 8539.10 5.00 310.60
20 10's # DAPABISO 10/2.5mg ANCOIAFA 03-27 186.56 30049099 120.82 2416.40 5.00 114.78
V3mi WSEMAGLYN (N) PEN 8260003 03-31 1,250.00 90189099 754.30 5.09 384.30 5.00 35.82
mee 13m! # SEMAGLYN IN] PEN B260003 03-31 1,250.00 90189099 754.30 5.00 0.00 754.30
"""

FREE_OCR = """
Bharath Medical and General Agencies
# BEMDAC TAB extra
20 10'S # BEMDAC TAB EMV260545 01-28 299.47 30049099 0.00 0.00 0.00
"""


class TestBharathMedicalQtyRate(unittest.TestCase):
    def test_detects_bharath(self):
        self.assertTrue(ocr_suggests_bharath_medical(BHARATH_OCR))
        self.assertFalse(ocr_suggests_bharath_medical("PAYAL PHARMA TAX INVOICE"))

    def test_parses_qty_rate_preferring_rate_after_hsn(self):
        rows = _parse_bharath_medical_qty_rate_rows(BHARATH_OCR)
        by = {_parse_key(r["product_description"]): r for r in rows}

        def q(name):
            return by[name]["quantity"], by[name]["unit_price"]

        self.assertEqual(q("BEMDACTAB")[0], 20)
        self.assertAlmostEqual(q("BEMDACTAB")[1], 186.69, places=2)
        self.assertEqual(q("ATORVA20MG")[0], 150)
        self.assertAlmostEqual(q("ATORVA20MG")[1], 121.55, places=2)
        self.assertEqual(q("ATORVA5MG")[0], 10)
        self.assertAlmostEqual(q("ATORVA5MG")[1], 89.72, places=2)
        self.assertEqual(q("ATORVA80MG")[0], 10)
        self.assertAlmostEqual(q("ATORVA80MG")[1], 261.80, places=2)
        self.assertEqual(q("ATORVAETAB")[0], 10)
        self.assertAlmostEqual(q("ATORVAETAB")[1], 232.62, places=2)
        self.assertEqual(q("CLOPITORVA10MGCAP")[0], 50)
        self.assertAlmostEqual(q("CLOPITORVA10MGCAP")[1], 238.26, places=2)
        self.assertEqual(q("CLOPITORVA20MGCAP")[0], 50)
        self.assertAlmostEqual(q("CLOPITORVA20MGCAP")[1], 241.38, places=2)
        self.assertEqual(q("CLOPITORVA40MGCAP")[0], 30)
        self.assertAlmostEqual(q("CLOPITORVA40MGCAP")[1], 217.97, places=2)
        self.assertEqual(q("DAPABISO1025MG")[0], 20)
        self.assertAlmostEqual(q("DAPABISO1025MG")[1], 120.82, places=2)
        self.assertEqual(q("SEMAGLYNINJPEN")[0], 1)
        self.assertAlmostEqual(q("SEMAGLYNINJPEN")[1], 754.30, places=2)

    def test_free_row_has_zero_rate(self):
        rows = _parse_bharath_medical_qty_rate_rows(
            "Bharath Medical\n20 10'S # BEMDAC TAB EMV1 01-28 299.47 30049099 0.00 0.00 0.00\n"
        )
        self.assertTrue(rows)
        self.assertTrue(rows[0]["is_free"])
        self.assertEqual(rows[0]["unit_price"], 0.0)
        self.assertEqual(rows[0]["quantity"], 20)

    def test_fix_updates_wrong_qty_rate_only(self):
        items = [
            {"product_description": "BEMDAC TAB", "quantity": "2", "unit_price": "1901.00"},
            {"product_description": "ATORVA 20mg.", "quantity": "150", "unit_price": "48.80"},
            {"product_description": "ATORVA 5mg", "quantity": "5", "unit_price": "1646.80"},
            {"product_description": "ATORVA 80mg", "quantity": "24", "unit_price": "261.80"},
            {"product_description": "ATORVAE Tab", "quantity": "5", "unit_price": "44113.20"},
            {"product_description": "CLOPITORVA 10mg CAP", "quantity": "5", "unit_price": "1.00"},
            {"product_description": "CLOPITORVA 20mg CAP", "quantity": "3", "unit_price": "1.00"},
            {"product_description": "CLOPITORVA 40mg CAP", "quantity": "3", "unit_price": "1.00"},
            {"product_description": "DAPABISO 10/2.5mg", "quantity": "20", "unit_price": "11492.80"},
            {"product_description": "SEMAGLYN IN] PEN", "quantity": "1", "unit_price": "1858.00"},
        ]
        out = fix_bharath_medical_qty_rate_from_ocr(items, BHARATH_OCR)
        by = {i["product_description"]: i for i in out}
        self.assertEqual(by["BEMDAC TAB"]["quantity"], "20")
        self.assertEqual(by["BEMDAC TAB"]["unit_price"], "186.69")
        self.assertEqual(by["ATORVA 20mg."]["quantity"], "150")
        self.assertEqual(by["ATORVA 20mg."]["unit_price"], "121.55")
        self.assertEqual(by["ATORVA 5mg"]["quantity"], "10")
        self.assertEqual(by["ATORVA 5mg"]["unit_price"], "89.72")
        self.assertEqual(by["ATORVA 80mg"]["quantity"], "10")
        self.assertEqual(by["ATORVA 80mg"]["unit_price"], "261.80")
        self.assertEqual(by["ATORVAE Tab"]["quantity"], "10")
        self.assertEqual(by["ATORVAE Tab"]["unit_price"], "232.62")
        self.assertEqual(by["CLOPITORVA 10mg CAP"]["quantity"], "50")
        self.assertEqual(by["CLOPITORVA 10mg CAP"]["unit_price"], "238.26")
        self.assertEqual(by["CLOPITORVA 20mg CAP"]["quantity"], "50")
        self.assertEqual(by["CLOPITORVA 20mg CAP"]["unit_price"], "241.38")
        self.assertEqual(by["CLOPITORVA 40mg CAP"]["quantity"], "30")
        self.assertEqual(by["CLOPITORVA 40mg CAP"]["unit_price"], "217.97")
        self.assertEqual(by["DAPABISO 10/2.5mg"]["quantity"], "20")
        self.assertEqual(by["DAPABISO 10/2.5mg"]["unit_price"], "120.82")
        self.assertEqual(by["SEMAGLYN IN] PEN"]["quantity"], "1")
        self.assertEqual(by["SEMAGLYN IN] PEN"]["unit_price"], "754.30")

    def test_unrelated_format_unchanged(self):
        items = [{"product_description": "FOO TAB", "quantity": "9", "unit_price": "1.11"}]
        out = fix_bharath_medical_qty_rate_from_ocr(items, "SUPREME LIFE SCIENCES")
        self.assertEqual(out[0]["quantity"], "9")
        self.assertEqual(out[0]["unit_price"], "1.11")


def _parse_key(name: str) -> str:
    from app import _bharath_medical_name_key
    return _bharath_medical_name_key(name)


if __name__ == "__main__":
    unittest.main()
