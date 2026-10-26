"""CHANDUKA AGENCIES COMP-column phantom product dedupe."""
import unittest

from app import (
    _parse_chanduka_ocr_line_items,
    drop_chanduka_comp_phantom_items,
    ocr_suggests_chanduka_agencies,
    recover_chanduka_line_items_from_ocr,
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


CHANDUKA_SB4904_OCR = """
CHANDUKA AGENCIES
GST INVOICE Inv Mod CREDIT
Bill No. : SB-4904 Dated 27/07/2026
S.N COMP HSNCODE PARTICULARS PACK BATCH EXP. QTY. FREE N_MRP O_MRP RATE AMOUNT GST% DIS%
1. BROOKS 30049099 DIMITAZ INJ 1 VIAL BLZF06A 10/27 20 F 242.52 0.00 55.00 1100.00 5.00 0.00
2. BROOKS 30049099 KORTIRON FCM 500 INJ 10ML L-2512054B 11/27 4 3234.38 3,450.00 500.00 2000.00 5.00 0.00
3. CIPLA *C 30049099 ANTIFLU CAP 10S 5BA2877 10/29 35 906.79 0.00 690.89 24181.15 5.00 4.00
4. CIPLA RE 30049099 ANTIFLU CAP 10S 5BA2877 10/29 5 906.79 0.00 690.89 3454.45 5.00 4.00
5. GUFIC CR 30042019 MEROFIC PLUS 1GM VIAL AK260126A 04/28 180 1024.33 0.00 97.00 17460.00 5.00 0.00
6. RADIANTS 21069099 RADIFRAC PLUS TAB 10`S RPT-032608 02/28 30 263.39 0.00 175.59 5267.70 5.00 0.00
7. SAMARTH 30049099 ASKLEROL INJ 3% 2ML IPLDA1504 07/28 7 164.98 179.59 125.70 879.90 5.00 4.00
8. SAMARTH 30049069 DISTINON TAB 10`S TPDBBT614 01/28 15 134.40 0.00 102.40 1536.00 5.00 4.00
9. UNITED B 30042063 CLARIMIN 500 4`S CNTA6C1 12/27 40 166.88 0.00 95.00 3800.00 5.00 0.00
10. UNITED B 30042095 CLINDATECH 300 8`S CLCK5B14 11/27 2 239.00 0.00 182.10 364.20 5.00 0.00
11. UNITED B 30043990 D PRESSIN NASAL SPRAY 5ML DEND6A1 03/28 1 1808.00 0.00 1377.52 1377.52 5.00 4.00
12. ZYDUS TO 30049079 AGGRAMED INJ 100ML HA260073A 02/28 5 10239.86 0.00 846.23 4231.15 5.00 0.00
"""


CHANDUKA_SB4427_OCR = """
CHANDUKA AGENCIES
GST INVOICE Inv Mod CREDIT
Bill No. : SB-4427 Dated 14/07/2026
S.N COMP HSNCODE PARTICULARS PACK BATCH EXP. QTY. FREE N_MRP O_MRP RATE AMOUNT GST% DIS%
1. BROOKS 30041090 BREXONA 8MG VIAL L-2606028B 05/28 230 F 10.87 0.00 8.28 1904.40 5.00 4.00
2. CIPLA RE 30049099 ANTIFLU CAP 10S 5BA2877 10/29 10 906.79 0.00 690.89 6908.90 5.00 4.00
3. GUFIC CR 30042019 MEROFIC PLUS 1GM VIAL AK260126A 04/28 170 1024.33 0.00 97.00 16490.00 5.00 0.00
4. JUGGAT P 30049084 BOTROCLOT LOTION 10ML BTS26409 03/28 10 306.00 198.00 233.14 2331.40 5.00 4.00
5. NICHOLAS 30049083 BACTRIM DS** 10`S PML0177 11/28 30 24.56 26.20 18.71 561.30 5.00 4.00
6. SAMARTH 30049099 ASKLEROL INJ 3% 2ML IPLDA1504 07/28 5 164.98 179.59 125.70 628.50 5.00 4.00
7. SAMARTH 30049069 DISTINON TAB 10`S TPDBBT614 01/28 30 134.40 0.00 102.40 3072.00 5.00 4.00
8. UNITED B 30042063 CLARIMIN 500 4`S CNTA6C1 12/27 30 166.88 0.00 95.00 2850.00 5.00 0.00
9. UNITED B 30042095 CLINDATECH 300 8`S CLCK5B14 11/27 10 239.00 0.00 182.10 1821.00 5.00 0.00
10. ZYDUS TO 30049079 AGGRAMED INJ 100ML HA250140B 07/27 2 10239.86 0.00 846.23 1692.46 5.00 0.00
11. ZYDUS TO 30049079 AGGRAMED INJ 100ML HA260046A 01/28 2 10239.86 0.00 846.23 1692.46 5.00 0.00
12. ZYDUS TO 30049079 AGGRAMED INJ 100ML HA260073A 02/28 5 10239.86 0.00 846.23 4231.15 5.00 0.00
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

    def test_parses_non_zydus_comp_rows(self):
        rows = _parse_chanduka_ocr_line_items(CHANDUKA_SB4904_OCR)
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[0]["product_description"], "DIMITAZ INJ")
        self.assertEqual(rows[0]["comp"], "BROOKS")
        self.assertEqual(rows[2]["product_description"], "ANTIFLU CAP")
        self.assertEqual(rows[2]["quantity"], 35)
        self.assertEqual(rows[3]["quantity"], 5)
        self.assertEqual(rows[4]["product_description"], "MEROFIC PLUS 1GM")
        self.assertEqual(rows[8]["product_description"], "CLARIMIN 500")
        self.assertEqual(rows[10]["product_description"], "D PRESSIN NASAL SPRAY")
        self.assertEqual(rows[11]["product_description"], "AGGRAMED INJ")

    def test_drops_recovered_comp_names_keeps_particulars(self):
        items = [
            {"product_description": "ANTIFLU CAP", "quantity": "5",
             "unit_price": "690.89", "total_amount": "3454.45",
             "lot_batch_number": "5BA2877"},
            {"product_description": "CIPLA RE", "quantity": "5",
             "unit_price": "690.89", "total_amount": "3454.45",
             "recovered_from_ocr": True},
            {"product_description": "MEROFIC PLUS 1GM", "quantity": "180",
             "unit_price": "97.00", "total_amount": "17460.00",
             "lot_batch_number": "AK260126A"},
            {"product_description": "GUFIC CR", "quantity": "180",
             "unit_price": "97.00", "total_amount": "17460.00",
             "recovered_from_ocr": True},
            {"product_description": "CLARIMIN 500", "quantity": "40",
             "unit_price": "95.00", "total_amount": "3800.00",
             "lot_batch_number": "CNTA6C1"},
            {"product_description": "UNITED B", "quantity": "40",
             "unit_price": "95.00", "total_amount": "3800.00",
             "recovered_from_ocr": True},
            {"product_description": "DIMITAZ INJ", "quantity": "20",
             "unit_price": "55.00", "total_amount": "1100.00"},
        ]
        out = drop_chanduka_comp_phantom_items(items, CHANDUKA_SB4904_OCR)
        names = [it["product_description"] for it in out]
        self.assertEqual(len(out), 4)
        self.assertEqual(names.count("ANTIFLU CAP"), 1)
        self.assertEqual(names.count("MEROFIC PLUS 1GM"), 1)
        self.assertEqual(names.count("CLARIMIN 500"), 1)
        self.assertEqual(names.count("DIMITAZ INJ"), 1)
        self.assertNotIn("CIPLA RE", names)
        self.assertNotIn("GUFIC CR", names)
        self.assertNotIn("UNITED B", names)

    def test_renames_comp_label_to_particulars(self):
        items = [
            {"product_description": "CIPLA RE", "quantity": "5",
             "unit_price": "690.89", "total_amount": "3454.45"},
        ]
        out = drop_chanduka_comp_phantom_items(items, CHANDUKA_SB4904_OCR)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["product_description"], "ANTIFLU CAP")
        self.assertEqual(out[0]["quantity"], "5")
        self.assertEqual(out[0]["unit_price"], "690.89")

    def test_parses_same_qty_rate_distinct_batches(self):
        rows = _parse_chanduka_ocr_line_items(CHANDUKA_SB4427_OCR)
        self.assertEqual(len(rows), 12)
        aggramed = [r for r in rows if r["product_description"] == "AGGRAMED INJ"]
        self.assertEqual(len(aggramed), 3)
        batches = {r["lot_batch_number"] for r in aggramed}
        self.assertEqual(batches, {"HA250140B", "HA260046A", "HA260073A"})

    def test_keeps_aggramed_same_amount_different_batches(self):
        items = [
            {"product_description": "AGGRAMED INJ", "quantity": "2",
             "unit_price": "846.23", "total_amount": "1692.46",
             "lot_batch_number": "HA250140B"},
            {"product_description": "AGGRAMED INJ", "quantity": "2",
             "unit_price": "846.23", "total_amount": "1692.46",
             "lot_batch_number": "HA260046A"},
            {"product_description": "AGGRAMED INJ", "quantity": "5",
             "unit_price": "846.23", "total_amount": "4231.15",
             "lot_batch_number": "HA260073A"},
        ]
        out = drop_chanduka_comp_phantom_items(items, CHANDUKA_SB4427_OCR)
        self.assertEqual(len(out), 3)
        batches = [it["lot_batch_number"] for it in out]
        self.assertEqual(batches.count("HA250140B"), 1)
        self.assertEqual(batches.count("HA260046A"), 1)
        self.assertEqual(batches.count("HA260073A"), 1)

    def test_recovers_missing_aggramed_batch(self):
        existing = [
            {"product_description": "AGGRAMED INJ", "quantity": "2",
             "unit_price": "846.23", "total_amount": "1692.46",
             "lot_batch_number": "HA260046A"},
            {"product_description": "AGGRAMED INJ", "quantity": "5",
             "unit_price": "846.23", "total_amount": "4231.15",
             "lot_batch_number": "HA260073A"},
        ]
        out = recover_chanduka_line_items_from_ocr(existing, CHANDUKA_SB4427_OCR)
        batches = [it["lot_batch_number"] for it in out]
        self.assertIn("HA250140B", batches)
        missing = next(it for it in out if it["lot_batch_number"] == "HA250140B")
        self.assertEqual(missing["product_description"], "AGGRAMED INJ")
        self.assertEqual(missing["quantity"], "2")
        self.assertEqual(missing["unit_price"], "846.23")
        self.assertEqual(missing["total_amount"], "1692.46")
        self.assertEqual(batches.count("HA250140B"), 1)
        self.assertEqual(batches.count("HA260046A"), 1)

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
