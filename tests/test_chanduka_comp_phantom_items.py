"""CHANDUKA AGENCIES COMP-column phantom product dedupe."""
import unittest

from app import (
    _parse_chanduka_ocr_line_items,
    drop_chanduka_comp_phantom_items,
    ocr_suggests_chanduka_agencies,
)


CHANDUKA_OCR = """
CHANDUKA AGENCIES
GST INVOICE Inv Mod CREDIT
Bill No. : SB-4613 Dated 20/07/2026
S.N COMP HSNCODE PARTICULARS PACK BATCH EXP. QTY. FREE N_MRP O_MRP RATE AMOUNT GST% DIS%
1. ZYDUS AE 30043990 ORCIBEST TABLETS 15`S IB00805A 03/28 50 F 246.54 0.00 187.84 9392.00 5.00 0.00
2. ZYDUS CO 30049099 DUONEM ER 300 TAB 10`S FA00035A 12/27 30 1740.80 0.00 1169.80 35094.00 5.00 0.00
3. ZYDUS DI 30049099 LIPAGLYN 4 MG 10`S MB04477B 10/28 30 501.13 0.00 381.81 11454.30 5.00 0.00
4. ZYDUS GE 30043913 DEXONA INJ 2ML NB00194A 08/27 1000 10.87 0.00 8.05 8050.00 5.00 0.00
5. ZYDUS HE 30049099 ODIMONT FX TABLETS 15`S IB00578C 02/28 50 323.29 0.00 175.58 8779.00 5.00 0.00
6. ZYDUS HE 30042039 OTODAC DX EAR DROP 5ML CAB01AAB 02/28 100 107.01 0.00 72.66 7266.00 5.00 0.00
7. ZYDUS RE 30049033 ODIMONT LC 15`S IB1076A 04/28 200 418.69 0.00 261.02 52204.00 5.00 0.00
"""


class TestChandukaCompPhantomItems(unittest.TestCase):
    def test_detects_chanduka_format(self):
        self.assertTrue(ocr_suggests_chanduka_agencies(CHANDUKA_OCR))
        self.assertFalse(ocr_suggests_chanduka_agencies("SUPREME LIFE SCIENCES\nM.R.P SGST"))

    def test_parses_seven_ocr_rows(self):
        rows = _parse_chanduka_ocr_line_items(CHANDUKA_OCR)
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0]["product_description"], "ORCIBEST TABLETS")
        self.assertEqual(rows[0]["quantity"], 50)
        self.assertEqual(rows[0]["unit_price"], 187.84)
        self.assertEqual(rows[0]["total_amount"], 9392.00)

    def test_drops_zydus_phantom_duplicates(self):
        items = [
            {
                "product_description": "ORCIBEST TAB",
                "quantity": "50",
                "unit_price": "187.84",
                "total_amount": "9392.00",
                "lot_batch_number": "IB00805A",
            },
            {
                "product_description": "ZYDUS AEROFROCE",
                "quantity": "50",
                "unit_price": "187.84",
                "total_amount": "9392.00",
            },
            {
                "product_description": "1.DUONEM ER 300 TAB (A)",
                "quantity": "30",
                "unit_price": "1169.80",
                "total_amount": "35094.00",
                "lot_batch_number": "FA00035A",
            },
            {
                "product_description": "ZYDUS MUCOTAB TAB",
                "quantity": "30",
                "unit_price": "1169.80",
                "total_amount": "35094.00",
            },
            {
                "product_description": "LIPAGLYN 4 MG",
                "quantity": "30",
                "unit_price": "381.81",
                "total_amount": "11454.30",
            },
            {
                "product_description": 'ZYDUS "DEXONA INJ',
                "quantity": "30",
                "unit_price": "381.81",
                "total_amount": "11454.30",
            },
            {
                "product_description": "DEXONA INJ",
                "quantity": "1000",
                "unit_price": "8.05",
                "total_amount": "8050.00",
            },
            {
                "product_description": "ZYDUS ZESPITRA",
                "quantity": "1000",
                "unit_price": "8.05",
                "total_amount": "8050.00",
            },
            {
                "product_description": "ODIMONT FX TABLET",
                "quantity": "50",
                "unit_price": "175.58",
                "total_amount": "8779.00",
            },
            {
                "product_description": "ZYDUS ZESPITRA",
                "quantity": "50",
                "unit_price": "175.58",
                "total_amount": "8779.00",
            },
            {
                "product_description": "OTODAC DX EAR DROP",
                "quantity": "100",
                "unit_price": "72.66",
                "total_amount": "7266.00",
            },
            {
                "product_description": "ODIMONT LC KID",
                "quantity": "200",
                "unit_price": "261.02",
                "total_amount": "52204.00",
            },
            {
                "product_description": "ZYDUS AEROFROCE",
                "quantity": "200",
                "unit_price": "261.02",
                "total_amount": "52204.00",
            },
        ]
        out = drop_chanduka_comp_phantom_items(items, CHANDUKA_OCR)
        self.assertEqual(len(out), 7)
        names = [it["product_description"] for it in out]
        self.assertNotIn("ZYDUS AEROFROCE", names)
        self.assertNotIn("ZYDUS MUCOTAB TAB", names)
        self.assertNotIn("ZYDUS ZESPITRA", names)
        self.assertIn("ORCIBEST TAB", names)
        self.assertIn("OTODAC DX EAR DROP", names)

    def test_no_op_for_other_formats(self):
        items = [
            {
                "product_description": "ZYDUS AEROFROCE",
                "quantity": "50",
                "unit_price": "187.84",
                "total_amount": "9392.00",
            },
        ]
        out = drop_chanduka_comp_phantom_items(items, "SUPREME LIFE SCIENCES")
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
