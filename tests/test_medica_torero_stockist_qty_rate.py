"""Medica/Torero stockist invoices: Rate and Quantity from table-band OCR."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _parse_medica_torero_qty_rate_rows,
    fix_medica_torero_stockist_qty_rate_from_ocr,
    ocr_suggests_medica_torero_stockist,
)


TABLE_OCR = """
[HSN MFG PACK PRODUCT DESCRIPTION QTY FREE BATCH EXP DIS% GST% M.R.P. RATE AMOUNT
30019099 ZYD 2X14°S |ATEN25MG TABS ——t#t+* | 930) 1800777A  |03/28|; 6.0) 5.0} 69.52) 47.19! 43886.70
30049099 ZYD | 2X14°S ATEN 50MG TABS 540 1B01062A 04/29} 6.0) 5.0) 77.44) 52.58) 28393.20
30089094’ ZYD | 14X2ML | DERIPHYLLIN AMPS 5 | NB00211A  /|03/30| 6.0) 5.0) 157.08; 105.96 529.80
30049091 ZYD | 5X2.5ML. COMBIMIST LD RESPULES 632, lEAGOO30. =—«-«12/27/| «6.0; 5.0| 95.60] 42.57) 26904.24
‘ sameons ZYD | SX2MLL| DERINIDE 0.S5MG RESPULES 60 FK50308 10/27| 6.0| 5.0] 114.55) 44.05} 2643.00
Medica A Tovtvo Product
ORIGINAL FOR RECIPIENT
Total Prod : 5 Total QTY : 2167
"""

FREE_OCR = """
HSN MFG PACK PRODUCT DESCRIPTION QTY FREE BATCH EXP DIS% GST% M.R.P. RATE AMOUNT
30049099 ZYD 2X14'S ATEN 25MG TABS 930 IB00777A 03/28 6.0 5.0 69.52 47.19 43886.70
30049099 ZYD 2X14'S ATEN 50MG TABS 10 IB01062A 04/29 6.0 5.0 77.44 0.00 0.00
Medica A Torero Product
"""

PAGE_OCR = """
MARUTHI DISTRIBUTOR
TAX INVOICE Invoice No : D353321
Party Code : 25036
[HSN [wrG[PACK [PRODUCT DESCRIPHON ary rRee Rave AMOUNT
SMAN : RITESH KUMAR
ORIGINAL FOR RECIPIENT
Subject To HYDERABAD Jurisdiction Medica A Tovtvo Product
"""


class TestMedicaToreroStockistQtyRate(unittest.TestCase):
    def test_detects_medica_torero_not_payal(self):
        self.assertTrue(ocr_suggests_medica_torero_stockist(PAGE_OCR, "MARUTHI DISTRIBUTOR"))
        self.assertFalse(ocr_suggests_medica_torero_stockist(
            PAGE_OCR.replace("Medica A Tovtvo Product", "")
            + "\nPAYAL PHARMA\nPACK. MFGR\nORIGINAL FOR RECIPIENT\nSMAN",
            "PAYAL PHARMA",
        ))

    def test_parses_qty_rate_from_table_ocr(self):
        rows = _parse_medica_torero_qty_rate_rows(TABLE_OCR)
        by_name = {
            r["product_description"].upper().replace(" ", ""): r for r in rows
        }
        self.assertGreaterEqual(len(rows), 5)
        aten25 = next(r for r in rows if "ATEN" in r["product_description"].upper()
                      and "25" in r["product_description"])
        aten50 = next(r for r in rows if "ATEN" in r["product_description"].upper()
                      and "50" in r["product_description"])
        deri = next(r for r in rows if "DERIPHYLLIN" in r["product_description"].upper())
        combi = next(r for r in rows if "COMBIMIST" in r["product_description"].upper())
        derin = next(r for r in rows if "DERINIDE" in r["product_description"].upper())
        self.assertEqual(aten25["quantity"], 930)
        self.assertAlmostEqual(aten25["unit_price"], 47.19, places=2)
        self.assertEqual(aten50["quantity"], 540)
        self.assertAlmostEqual(aten50["unit_price"], 52.58, places=2)
        self.assertEqual(deri["quantity"], 5)
        self.assertAlmostEqual(deri["unit_price"], 105.96, places=2)
        self.assertEqual(combi["quantity"], 632)
        self.assertAlmostEqual(combi["unit_price"], 42.57, places=2)
        self.assertEqual(derin["quantity"], 60)
        self.assertAlmostEqual(derin["unit_price"], 44.05, places=2)
        self.assertFalse(any(r.get("is_free") for r in rows))

    def test_free_row_does_not_inherit_paid_rate(self):
        rows = _parse_medica_torero_qty_rate_rows(FREE_OCR)
        free = next(r for r in rows if r.get("is_free"))
        paid = next(r for r in rows if not r.get("is_free"))
        self.assertEqual(paid["quantity"], 930)
        self.assertAlmostEqual(paid["unit_price"], 47.19, places=2)
        self.assertEqual(free["quantity"], 10)
        self.assertEqual(free["unit_price"], 0.0)

    def test_fix_updates_wrong_rate_and_adds_missing(self):
        items = [
            {
                "product_description": "DERIPHYLLIN AMPS",
                "quantity": "5",
                "unit_price": "529.80",
                "total_amount": "2649.00",
                "additional_fields": {"free_quantity": "0"},
            },
            {
                "product_description": "COMBIMIST LD RESPULES",
                "quantity": "632",
                "unit_price": "26.43",
                "total_amount": "16695.36",
                "additional_fields": {"free_quantity": "0"},
            },
            {
                "product_description": "DERINIDE 0.6MG RESPULES",
                "quantity": "60",
                "unit_price": "94.61",
                "total_amount": "5676.60",
                "additional_fields": {"free_quantity": "0"},
            },
        ]
        out = fix_medica_torero_stockist_qty_rate_from_ocr(
            items, PAGE_OCR, TABLE_OCR, "MARUTHI DISTRIBUTOR")
        by = {i["product_description"].upper(): i for i in out}
        self.assertEqual(by["DERIPHYLLIN AMPS"]["quantity"], "5")
        self.assertEqual(by["DERIPHYLLIN AMPS"]["unit_price"], "105.96")
        self.assertEqual(by["COMBIMIST LD RESPULES"]["quantity"], "632")
        self.assertEqual(by["COMBIMIST LD RESPULES"]["unit_price"], "42.57")
        self.assertEqual(by["DERINIDE 0.6MG RESPULES"]["quantity"], "60")
        self.assertEqual(by["DERINIDE 0.6MG RESPULES"]["unit_price"], "44.05")
        aten25 = next(i for i in out if "ATEN" in i["product_description"].upper()
                      and "25" in i["product_description"])
        aten50 = next(i for i in out if "ATEN" in i["product_description"].upper()
                      and "50" in i["product_description"])
        self.assertEqual(aten25["quantity"], "930")
        self.assertEqual(aten25["unit_price"], "47.19")
        self.assertEqual(aten50["quantity"], "540")
        self.assertEqual(aten50["unit_price"], "52.58")

    def test_does_not_change_unrelated_format(self):
        items = [{
            "product_description": "SOME TAB",
            "quantity": "10",
            "unit_price": "12.00",
        }]
        out = fix_medica_torero_stockist_qty_rate_from_ocr(
            items, "SUPREME LIFE SCIENCES MARG ERP", "", "SUPREME LIFE SCIENCES")
        self.assertEqual(out[0]["unit_price"], "12.00")
        self.assertEqual(out[0]["quantity"], "10")


if __name__ == "__main__":
    unittest.main()
