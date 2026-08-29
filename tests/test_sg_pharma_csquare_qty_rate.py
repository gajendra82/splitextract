"""SG PHARMA / C-Square: restore qty/rate when Gemini invents 10x QTY."""
import unittest

from app import (
    _ocr_suggests_sg_pharma_csquare,
    _parse_sg_pharma_csquare_qty_rate_rows,
    fix_sg_pharma_csquare_qty_rate_from_ocr,
)


SG_OCR = """
SG PHARMA DISTRIBUTORS PVT LTD Bill To : WELLO RETAIL PRIVATE LIMITED
TAX INV NO : 2600073002053
DATE : 26-06-2026 16:51
| 30043919 DECA DURABOLIN 50MG INJ 1X1 GB0124A 12-29 10 0.00 508.13 290.95 0.00 2909.50 5.0 ZYDU
| 30049079 ATORVA 40MG TAB 1X10 IB00787A 03-29 180 0.00 207.50 114.80 0.00 20664.00 5.0 ZYDU
| 30049079 ATORVA 40MG TAB 1X10 IB01063A 04-29 570 0.00 207.50 114.80 0.00 65436.00 5.0 ZYDU
| 30049079 CLOPITORVA 40MG TAB 1X10 CPR26001 12-27 10 0.00 390.23 240.73 0.00 2407.30 5.0 ZYDU
| 30049099 MELGAIN LOTION 5ML 1X5ML AFC1040 02-28 12 0.00 1018.83 548.15 0.00 6877.80 5.0 ZYDU
| 30049079 CLOPITORVA 10MG CAP (15) 1X15 IB00189A 12-27 870 0.00 426.46 195.75 0.00 170302.50 5.0 ZYDU
| 30049079 ATORVA E TAB 1X10 SB00193A 02-28 10 0.00 416.42 201.26 0.00 2012.60 5.0 ZYDU
Tot Items:7 Tot Qty:1662
Software by C-Square Info Solution Pvt Ltd.
"""

# Garbled OCR variants (MRP decimal must not become QTY; 7A7 rate; truncated amount)
SG_OCR_GARBLED = """
SG PHARMA DISTRIBUTORS PVT LTD
| 30043919 DECA DURABOLIN SOMG INJ 1X1 GBO1244 1229 10 508.13 290.95 0.00 2909.50 5.0 ZYDU
30943913 DEXONA 8MG INJ 2ML 1X2ML_NBOO162A 08-27 140 0.00 10.87 7A7 0.00 1003.80 5.0 ZYDU|
| 30049079 ATORVA 5MG TAB (15) 1X15 IBOO785A 03-29 250 0.00 160.62 86.50 0.00 2625.00 5.0 ZYDU
Tot Items:3 Tot Qty:400
Software by C-Square Info Solution Pvt Ltd.
"""


class TestSgPharmaCsquareQtyRate(unittest.TestCase):
    def test_detect_sg_pharma(self):
        self.assertTrue(_ocr_suggests_sg_pharma_csquare(SG_OCR))
        self.assertFalse(
            _ocr_suggests_sg_pharma_csquare(
                "OTHER VENDOR Tot Qty: 10 TAX INV NO : 1", vendor="OTHER"
            )
        )

    def test_parse_qty_not_from_mrp_decimal(self):
        rows = _parse_sg_pharma_csquare_qty_rate_rows(SG_OCR_GARBLED)
        by_name = {
            r["product_description"].upper(): r for r in rows
        }
        deca = next(r for r in rows if "DECA" in r["product_description"].upper())
        self.assertEqual(deca["quantity"], 10.0)
        self.assertAlmostEqual(deca["unit_price"], 290.95, places=2)
        dex = next(r for r in rows if "DEXONA" in r["product_description"].upper())
        self.assertEqual(dex["quantity"], 140.0)
        self.assertAlmostEqual(dex["unit_price"], 7.17, places=2)
        atorva5 = next(r for r in rows if "ATORVA 5MG" in r["product_description"].upper())
        self.assertEqual(atorva5["quantity"], 250.0)
        self.assertAlmostEqual(atorva5["total_amount"], 21625.0, places=2)

    def test_fix_inflated_qty_from_gemini(self):
        bad = [
            {
                "product_description": "DECA DURABOLIN SOMG INJ 1X1",
                "quantity": "100",
                "unit_price": "290.95",
                "total_amount": "29095.00",
                "lot_batch_number": "GBO124A",
            },
            {
                "product_description": "CLOPITORVA 40MG TAB 1X10",
                "quantity": "100",
                "unit_price": "240.73",
                "total_amount": "24073.00",
                "lot_batch_number": "CPR26001",
            },
            {
                "product_description": "CLOPITORVA 10MG CAP (15) 1X15",
                "quantity": "8700",
                "unit_price": "1957.50",
                "total_amount": "17030250.00",
                "lot_batch_number": "IBOO189A",
            },
            {
                "product_description": "MELGAIN LOTION SMi 1X5ML",
                "quantity": "6",
                "unit_price": "1146.30",
                "total_amount": "6877.80",
                "lot_batch_number": "AFC1040",
            },
            {
                "product_description": "ATORVA E TAB 1X10",
                "quantity": "108",
                "unit_price": "201.26",
                "total_amount": "20126.90",
                "lot_batch_number": "SBO0193A",
            },
        ]
        out = fix_sg_pharma_csquare_qty_rate_from_ocr(
            bad, SG_OCR, vendor="SG PHARMA DISTRIBUTORS PVT LTD"
        )
        self.assertEqual(out[0]["quantity"], "10")
        self.assertEqual(out[0]["total_amount"], "2909.50")
        self.assertEqual(out[1]["quantity"], "10")
        self.assertEqual(out[1]["total_amount"], "2407.30")
        self.assertEqual(out[2]["quantity"], "870")
        self.assertEqual(out[2]["unit_price"], "195.75")
        self.assertEqual(out[2]["total_amount"], "170302.50")
        self.assertEqual(out[3]["quantity"], "12")
        self.assertEqual(out[3]["unit_price"], "548.15")
        self.assertEqual(out[3]["total_amount"], "6577.80")
        self.assertEqual(out[4]["quantity"], "10")
        self.assertEqual(out[4]["total_amount"], "2012.60")

    def test_live_ocr_10x_qty_and_paise_amount(self):
        """Live Tesseract often embeds Gemini's 10× QTY and amount without decimals."""
        ocr = """
SG PHARMA DISTRIBUTORS PVT LTD
360-1919 DECA DURABOLIN SOMG INJ 1X1 GBO124A 12:29 «100 0.00 508.13 290.95 0.00 290950 5.0 ZYDU.
gg MELGAIN LOTION SMi 1X5ML_ AFC1040 02-28 «6 12—=«O«O 0.00 1018.83 548.18 0.00 687780 5.0 ZYDU
3Crnb ivi: CLOPITORVA 10MG CAP (15) 1X15 IBOO189A 12-27 8700 0 0.00 426.46 195.75 0.00 17030250 5.0 ZYDU
Tot Hems:18
iy C-Square lifo Solution Pvt Ltd.
"""
        self.assertTrue(_ocr_suggests_sg_pharma_csquare(ocr))
        bad = [
            {
                "product_description": "DECA DURABOLIN SOMG INJ 1X1",
                "quantity": "100",
                "unit_price": "290.95",
                "total_amount": "29095.00",
                "lot_batch_number": "GBO124A",
            },
            {
                "product_description": "MELGAIN LOTION SMi 1X5ML",
                "quantity": "6",
                "unit_price": "1146.30",
                "total_amount": "6877.80",
                "lot_batch_number": "AFC1040",
            },
            {
                "product_description": "CLOPITORVA 10MG CAP (15) 1X15",
                "quantity": "8700",
                "unit_price": "1957.50",
                "total_amount": "17030250.00",
                "lot_batch_number": "IBOO189A",
            },
        ]
        out = fix_sg_pharma_csquare_qty_rate_from_ocr(bad, ocr, vendor="")
        self.assertEqual(out[0]["quantity"], "10")
        self.assertEqual(out[0]["total_amount"], "2909.50")
        self.assertEqual(out[1]["quantity"], "12")
        self.assertAlmostEqual(float(out[1]["unit_price"]), 548.18, places=2)
        self.assertEqual(out[2]["quantity"], "870")
        self.assertEqual(out[2]["unit_price"], "195.75")

    def test_non_sg_unchanged(self):
        items = [
            {
                "product_description": "SOME PRODUCT",
                "quantity": "100",
                "unit_price": "10.00",
                "total_amount": "1000.00",
            }
        ]
        out = fix_sg_pharma_csquare_qty_rate_from_ocr(
            items, "OTHER VENDOR TAX INVOICE", vendor="OTHER"
        )
        self.assertEqual(out[0]["quantity"], "100")


if __name__ == "__main__":
    unittest.main()
