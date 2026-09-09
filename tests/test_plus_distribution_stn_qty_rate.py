"""PLUS DISTRIBUTION Stock Transfer Note: Bill Qty / Rate / Net Amt restore."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    fix_plus_distribution_stn_line_items_from_ocr,
    ocr_suggests_plus_distribution_stn,
    _parse_plus_stn_qty_rate_rows,
    _plus_stn_rows_are_sane,
)


HEADER = """
Stock Transfer Note
PLUS DISTRIBUTION PRIVATE LIMITED-GURUGRAM
Email: invoice.plus@plusdistributions.in
Invoice No.: STN32/26-3725
Bill Qty Free Qty Rate MRP DIS% DIS Amt Amt Net Amt
"""

FOOTER = """
QTY: 2664
TOTAL Dis: 31677.67
TOTAL AMT: 364293.11
TCD: 0 OFF: 0.11 364293
NET AMT: 364293
"""

ROWS = """
30049079 1BO0993A Mar-2029 100 - i 44.34 80.14 8.00 354.72 4434.00 4079.28
30049099 1BO0820A Mar-2029 | 200 - 112.22 202.54 8.00 1795.52 22444.00! 20648.48
30049099 IBO1026A Mar-2028 | 30 = 33.47 4437 8.00| 80.33 100410 923.77
30049099 1BOO962A Mar-2028 120 - 93.85 147.46 8.00 900.96 | 1262.00 10361.04
30049099 SBO0247A Sep-2028 | 168 - 104.41 189 8.00 1403.27 1754088 16137.61
30049069 IBOO395A Feb-2028 270 - 205.82 349.28 8.00 4445.71 55571.40 5125.69
30049029 IB00887A Mar-2028 | 30 - 9412 149.74 8.00 225.89 2823.60 2597.71
30049039 1B00926A Feb-2028 | 80 | 1543 299.4 8.00 986.43 12330.40 11343.97
30049099 SBOOI64A Feb-2029 | 50 | 38.28 5753 8.00 152.92 191150 1758.58
300490 1BI046A May-2028 10 = 22717, 435.02 8.00 181.74 227170 2089.96
30049099 IBOO765B Mar-2028 | 40 - 8633-12591 8.00 276.26 3453.20 3176.94
30043913 BECII7 Mar-2029 30 - 5.04 693 800 1209 15114 139.05
30049039 IBO1082A Apr-2029 300 12766 28796 8.00 3063.72 3829650 35232.78
30049061 IBOITIOA Oct-2027 120 7 299.66 582.61 8.00 2876.74 35959.20 33082.46
30049079 IBO1063A_-—Apr-2029 100 - 14.79 2075 8.00 918.28 1147850 10560.22
30049079 1BO0975A Apr-2028 30 38614 605.69 8.00 830.73 1038411 9553.38
30049094 IBOO66OA Feb-2028 | 30 - 34617 873.68 8.00 830.81 1038510 9554.29
30049099 MAO6836A Nov-2027 10 355.59 589.3 8.00 284.47 3555.90 3271.43
30049079 IBO0189A Dec-2027 100 241.26 426.46 8.00 1930.10 24126.30 22196.20
30049099 IBOO602A Aug-2028 100 377.99 501.13 8.00 3023.92 37799.00 34775.08
30049099 1B00392A Jan-2028 150 153.35 260.23 8.00 1840.20 2300250 2162.30
300490 1801078A Apr-2028 36 - 225.99 383.51 8.00 65085 8135.64 7484.79
30049099 EMV260854B Mar-2028 50 - 163.66 260.39 8.00 654.64 8183.00 7528.36
30049099 BRC1007 Dec-2027 10 - 318.96 555.26 8.00 255.16 3189.56 2934.40
300490 25S2GTC290 Feb-2027 70 - 205.22 312.88 8.00 1149.21 14365.12 13215.91
300490 26S2GTAI29 Jun-2027 30 205.22 312.88 8.00 492.52 6156.48 5663.96
30049069 DBC1035 Feb-2029 350 418 72.98 8.00 1170.40 14630.00 13459.60
30049099 CB-022608 Jan-2028 50 - 222.52 295 8.00 890.08 1125.95 10235.87
"""

OCR = HEADER + """
1. ATORVA 10 MG TAB 15'S
2. ATORVA 20 MG TAB 15'S
CLOP G CREAM 30 GM
COMBIMIST L RESPICAPS 30'S
EPSOLIN 100 MG TAB 100'S
NUCOXIA 90 MG TAB 15'S
ORNI G TAB 10'S
PANTODAC DSR CAPS 15'S
PRIMOLUT N TAB 10'S
NAFTIFAST CREAM 30 GM
NUCOXIA XP 120 MG TAB 5'S
DEXONA TAB 30'S
PANTODAC 40 MG TAB 15'S
FORGLYN PLUS RESPICAPS 30'S
ATORVA 40 MG TAB 10'S
ZYTANEX 5 MG TAB 15'S
GLYNIUM 50 MCG RESPICAPS 30'S
ARZEP NASAL SPRAY
CLOPITORVA 10 MG CAPS 15'S
LIPAGLYN 4 MG TAB 10'S
NUCOXIA 120 MG TAB 10'S
LINID TAB 10'S
DAPAGLYN SM 100/1000 MG TAB 10'S
ACTIBILE 300 MG TAB 10'S
NUCOXIA PG TAB 10'S
NASOCLEAR DROP 20 ML
ERDOZYD CAPS 10'S
""" + ROWS + FOOTER


class TestPlusDistributionStnQtyRate(unittest.TestCase):
    def test_detects_plus_distribution_stn(self):
        self.assertTrue(ocr_suggests_plus_distribution_stn(OCR))
        self.assertTrue(ocr_suggests_plus_distribution_stn(
            HEADER + "NET AMT: 100", "PLUS DISTRIBUTION PVT LTD"))
        self.assertFalse(ocr_suggests_plus_distribution_stn(
            "JACKSON MEDICALS Inv.No. D7655 QTY Rate Amount",
            "JACKSON MEDICALS"))
        self.assertFalse(ocr_suggests_plus_distribution_stn(
            "PLUS DISTRIBUTION PRIVATE LIMITED TAX INVOICE Net Amount 100"))

    def test_parse_bill_qty_rate_net_not_disc_pct(self):
        rows = _parse_plus_stn_qty_rate_rows(OCR)
        by_net = {round(r["total_amount"], 2): r for r in rows}
        atorva10 = by_net[4079.28]
        self.assertEqual(atorva10["quantity"], 100.0)
        self.assertAlmostEqual(atorva10["unit_price"], 44.34, places=2)
        # DIS% 8.00 must not become qty
        self.assertNotEqual(atorva10["quantity"], 8.0)

        combimist = by_net[10361.04]
        self.assertEqual(combimist["quantity"], 120.0)
        self.assertAlmostEqual(combimist["unit_price"], 93.85, places=2)

        nasoclear = by_net[13459.60]
        self.assertEqual(nasoclear["quantity"], 350.0)
        self.assertAlmostEqual(nasoclear["unit_price"], 41.80, places=2)

        atorva40 = by_net[10560.22]
        self.assertEqual(atorva40["quantity"], 100.0)
        self.assertAlmostEqual(atorva40["unit_price"], 114.79, places=1)

    def test_rows_match_footer_qty_and_net(self):
        rows = _parse_plus_stn_qty_rate_rows(OCR)
        self.assertTrue(_plus_stn_rows_are_sane(rows, OCR))
        self.assertGreaterEqual(len(rows), 26)
        qty_sum = sum(r["quantity"] for r in rows)
        net_sum = sum(r["total_amount"] for r in rows)
        self.assertLess(abs(qty_sum - 2664) / 2664, 0.12)
        self.assertLess(abs(net_sum - 364293) / 364293, 0.12)

    def test_fix_replaces_exploded_zydus_rows(self):
        items = [
            {
                "product_description": "ZYDUS",
                "quantity": "7298",
                "unit_price": "492.52",
                "total_amount": "8.00",
            },
            {
                "product_description": "ZYDUS GY",
                "quantity": "1462000",
                "unit_price": "170.40",
                "total_amount": "1345960",
            },
            {
                "product_description": "JATORVA10 MG TAB IS'S",
                "quantity": "8.00",
                "unit_price": "354.72",
                "total_amount": "4079.28",
            },
        ]
        out = fix_plus_distribution_stn_line_items_from_ocr(
            items, OCR, vendor="PLUS DISTRIBUTION PRIVATE LIMITED-GURUGRAM"
        )
        self.assertGreaterEqual(len(out), 26)
        qty_sum = sum(float(x["quantity"]) for x in out)
        net_sum = sum(float(x["total_amount"]) for x in out)
        self.assertLess(abs(qty_sum - 2664) / 2664, 0.12)
        self.assertLess(abs(net_sum - 364293) / 364293, 0.12)
        self.assertTrue(any(
            "ATORVA" in str(x.get("product_description", "")).upper()
            and float(x["quantity"]) == 100
            and abs(float(x["total_amount"]) - 4079.28) < 0.05
            for x in out
        ))
        self.assertFalse(any(float(x["quantity"]) > 5000 for x in out))

    def test_fix_skips_other_vendors(self):
        items = [{
            "product_description": "BETD 0.5ML",
            "quantity": "20",
            "unit_price": "5.15",
            "total_amount": "102.90",
        }]
        out = fix_plus_distribution_stn_line_items_from_ocr(
            items, "JACKSON MEDICALS Inv.No. D7655", vendor="JACKSON MEDICALS"
        )
        self.assertEqual(out[0]["quantity"], "20")
        self.assertEqual(out[0]["unit_price"], "5.15")


if __name__ == "__main__":
    unittest.main()
