"""TRILOK ENTERPRISES product-name cleanup (HSN MFG PRODUCT PACK)."""
import unittest

from app import (
    fix_trilok_enterprises_product_names_from_ocr,
    fix_trilok_enterprises_mfg_prefix_product_fallback,
    fix_trilok_enterprises_embedded_pack_in_product_fallback,
    fix_trilok_enterprises_ocr_garbage_product_fallback,
    fix_trilok_enterprises_overlap_pack_product_fallback,
)


TRILOK_OCR = """
TAX INVOICE
TRILOK ENTERPRISES To:- WELLNESS FOREVER CHEMIST AND LIFESTYLE STORE
HSN MFG PRODUCT PACK BATCH EXP MRP QTY RATE Dis VALUE
30049039 GER CINTODAC CAP 10'S IA01746A 9/27 405.33 10 257.35 3.00 0.00 5 2573.50
MARG ERP NANO
"""

TRILOK_XYLO_OCR = """
TAX INVOICE
TRILOK ENTERPRISES To:- WELLNESS FOREVER
HSN MFG PRODUCT PACK BATCH EXP MRP QTY RATE Dis VALUE
30039034 GER XYLOCAINE 2% JELLY 30 G GB0489 3/28 34.82 5 23.88 3.00 0.00 5 119.40
30049099 GER XYLOCAINE 10% SPARY 1X50 AFC1048 2/28 654.07 5 498.34 3.00 0.00 5 2491.70
MARG ERP NANO
"""

TRILOK_BOLDINE_OCR = """
TAX INVOICE
TRILOK ENTERPRISES To:- WELLNESS FOREVER CHEMIST AND LIFESTYLE STORE
HSN MFG PRODUCT PACK BATCH EXP MRP QTY RATE Dis VALUE
30049079 GER ATORVA 40 TAB 10'S IB00460A 1/29 206.18 3 125.67 3.00 0.00 5 377.01 3
300490 ALK PROVIDIP OINMENT 15GM 15GM PR25007 6/27 36.00 20 14.13 3.00 0.00 5 282.60 20
30049099 GLE BOLDINE 10GM POWDER 10GM N4360001 12/28 118.50 20 13.20 3.00 0.00 5 264.00 20
30042099 GSK T-BACT OINT 5GM Y53F 4/27 108.36 20 78.43 3.00 0.00 5 1568.60 20
MARG ERP NANO
"""


class TestTrilokEnterprisesProductNames(unittest.TestCase):
    def test_strips_mfg_and_pack_from_cintodac(self):
        items = [
            {
                "product_description": "GER CINTODAC CAP 10'S",
                "quantity": "10",
                "unit_price": "257.35",
                "total_amount": "2573.50",
                "lot_batch_number": "IA01746A",
            }
        ]
        out = fix_trilok_enterprises_product_names_from_ocr(items, TRILOK_OCR)
        self.assertEqual(out[0]["product_description"], "CINTODAC CAP")
        self.assertEqual(out[0]["additional_fields"]["mfg"], "GER")
        self.assertEqual(out[0]["additional_fields"]["pack"], "10'S")

    def test_strips_pack_30g_and_1x50_and_fixes_spary(self):
        items = [
            {
                "product_description": "XYLOCAINE 2% JELLY 30 G",
                "quantity": "5",
                "lot_batch_number": "GB0489",
                "additional_fields": {"mfg": "GER"},
            },
            {
                "product_description": "XYLOCAINE 10% SPARY 1X50",
                "quantity": "5",
                "lot_batch_number": "AFC1048",
                "additional_fields": {"mfg": "GER"},
            },
        ]
        out = fix_trilok_enterprises_product_names_from_ocr(items, TRILOK_XYLO_OCR)
        self.assertEqual(out[0]["product_description"], "XYLOCAINE 2% JELLY")
        self.assertEqual(out[0]["additional_fields"]["pack"], "30 G")
        self.assertEqual(out[1]["product_description"], "XYLOCAINE 10% SPRAY")
        self.assertEqual(out[1]["additional_fields"]["pack"], "1X50")

    def test_ts02507_boldine_strips_gle_mfg_prefix(self):
        """TS02507: PRODUCT embeds 10GM so primary regex misses; strip GLE via fallback."""
        items = [
            {
                "product_description": "ATORVA 40 TAB",
                "lot_batch_number": "IB00460A",
                "additional_fields": {"mfg": "GER", "pack": "10'S"},
            },
            {
                "product_description": "PROVIDIP OINMENT",
                "lot_batch_number": "PR25007",
                "additional_fields": {"mfg": "ALK", "pack": "15GM"},
            },
            {
                "product_description": "GLE BOLDINE 10GM POWDER",
                "lot_batch_number": "N4360001",
                "quantity": "20",
                "unit_price": "13.20",
            },
            {
                "product_description": "T-BACT OINT",
                "lot_batch_number": "Y53F",
                "additional_fields": {"mfg": "GSK", "pack": "5GM"},
            },
        ]
        primary = fix_trilok_enterprises_product_names_from_ocr(
            [dict(i) for i in items], TRILOK_BOLDINE_OCR
        )
        # Primary leaves GLE fused (inner 10GM confuses PACK/BATCH parse)
        self.assertEqual(primary[2]["product_description"], "GLE BOLDINE 10GM POWDER")

        out = fix_trilok_enterprises_mfg_prefix_product_fallback(
            primary, TRILOK_BOLDINE_OCR
        )
        self.assertEqual(out[2]["product_description"], "BOLDINE 10GM POWDER")
        self.assertEqual(out[2]["additional_fields"]["mfg"], "GLE")
        self.assertEqual(out[2]["additional_fields"]["pack"], "10GM")
        # Already-correct rows unchanged
        self.assertEqual(out[0]["product_description"], "ATORVA 40 TAB")
        self.assertEqual(out[1]["product_description"], "PROVIDIP OINMENT")
        self.assertEqual(out[3]["product_description"], "T-BACT OINT")

    def test_ts02507_providip_restores_embedded_15gm(self):
        """TS02507: OCR has PROVIDIP OINMENT 15GM 15GM — keep 15GM in description."""
        items = [
            {
                "product_description": "ATORVA 40 TAB",
                "lot_batch_number": "IB00460A",
                "additional_fields": {"mfg": "GER", "pack": "10'S"},
            },
            {
                "product_description": "PROVIDIP OINMENT",
                "lot_batch_number": "PR25007",
                "additional_fields": {"mfg": "ALK", "pack": "15GM"},
            },
            {
                "product_description": "BOLDINE 10GM POWDER",
                "lot_batch_number": "N4360001",
                "additional_fields": {"mfg": "GLE", "pack": "10GM"},
            },
            {
                "product_description": "T-BACT OINT",
                "lot_batch_number": "Y53F",
                "additional_fields": {"mfg": "GSK", "pack": "5GM"},
            },
        ]
        out = fix_trilok_enterprises_embedded_pack_in_product_fallback(
            [dict(i) for i in items], TRILOK_BOLDINE_OCR
        )
        self.assertEqual(out[1]["product_description"], "PROVIDIP OINMENT 15GM")
        # Non-duplicated pack rows unchanged
        self.assertEqual(out[0]["product_description"], "ATORVA 40 TAB")
        self.assertEqual(out[2]["product_description"], "BOLDINE 10GM POWDER")
        self.assertEqual(out[3]["product_description"], "T-BACT OINT")

    def test_ts02702_xylomist_ocr_garbage_recovered(self):
        """TS02702: glued DROPS*10ML + Gemini pastes OCR tail into description."""
        ocr = """
        TAX INVOICE
        TRILOK ENTERPRISES To:- WELLNESS FOREVER CHEMIST AND LIFESTYLE STORE
        HSN MFG PRODUCT PACK BATCH EXP MRP QTY RATE Dis VALUE
        30049069 ZYD NUCOXIA P TAB 15'S IB00152A 12/27 268.29 20 170.34 3.00 0.00 5 3406.80 20
        30049094 GER XYLOMIST-P NASAL DROPS*10ML DBC1011 12/28 44.73 10 28.40 3.00 0.00 5 284.00 10
        /GER XYLOMIST-P NASAL DROPS 10ML DBC1011 12/28 44.73 10 28.40 3.00 0.00 284.00 5 10
        MARG ERP NANO
        """
        items = [
            {
                "product_description": "NUCOXIA P TAB",
                "lot_batch_number": "IB00152A",
                "additional_fields": {"mfg": "ZYD", "pack": "15'S"},
            },
            {
                "product_description": (
                    "XYLOMIST-P NASAL DROPS*10ML DBC1011 12/28 44.73 10 "
                    "28.40 3.00 0.00 5 284.00 10 /GER XYLOMIST-P NASAL DROPS"
                ),
                "lot_batch_number": "DBC1011",
                "quantity": "10",
                "unit_price": "28.40",
                "additional_fields": {"mfg": "GER", "pack": "10ML"},
            },
        ]
        out = fix_trilok_enterprises_ocr_garbage_product_fallback(items, ocr)
        self.assertEqual(out[1]["product_description"], "XYLOMIST-P NASAL DROPS")
        self.assertEqual(out[1]["additional_fields"]["pack"], "10ML")
        self.assertEqual(out[1]["additional_fields"]["mfg"], "GER")
        self.assertEqual(out[0]["product_description"], "NUCOXIA P TAB")

    def test_ts03070_xylomist_overlap_drop1_star_0ml(self):
        """TS03070: overlap turns DROP* 10ML into DROP1*0ML → DROP + 10ML."""
        ocr = """
        TAX INVOICE
        TRILOK ENTERPRISES To:- WELLNESS FOREVER CHEMIST AND LIFESTYLE STORE
        HSN MFG PRODUCT PACK BATCH EXP MRP QTY RATE Dis VALUE
        30049094 GER XYLOMIST 0.1% NASAL DROP1*0ML DBC1026 2/29 54.68 20 41.66 3.00 0.00 5 833.20 20
        -GER XYLOMIST 0.1% NASAL DR 10ML DBC1026 : 2/29 54.68 20 41.66 3.00 0.00 833.20 5 20
        30039034 GER XYLOCAINE 2% JELLY 30 G GB0489 3/28 34.82 100 23.88 3.00 0.00 5 2388.00 100
        MARG ERP NANO
        """
        items = [
            {
                "product_description": "XYLOMIST 0.1% NASAL DROP1",
                "lot_batch_number": "DBC1026",
                "quantity": "20",
                "unit_price": "41.66",
                "additional_fields": {"mfg": "GER", "pack": "10ML"},
            },
            {
                "product_description": "XYLOCAINE 2% JELLY",
                "lot_batch_number": "GB0489",
                "additional_fields": {"mfg": "GER", "pack": "30 G"},
            },
        ]
        out = fix_trilok_enterprises_overlap_pack_product_fallback(items, ocr)
        self.assertEqual(out[0]["product_description"], "XYLOMIST 0.1% NASAL DROP")
        self.assertEqual(out[0]["additional_fields"]["pack"], "10ML")
        self.assertEqual(out[0]["additional_fields"]["mfg"], "GER")
        self.assertEqual(out[1]["product_description"], "XYLOCAINE 2% JELLY")


if __name__ == "__main__":
    unittest.main()
