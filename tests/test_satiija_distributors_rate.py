"""SATIJA DISTRIBUTORS Rate vs OldMRP corrections."""
import unittest

from app import fix_satiija_distributors_rate_from_ocr


SATIJA_OCR = """
M/S SATIJA DISTRIBUTORS
GST INVOICE (CREDIT)
S. Mfg Product Name Pack HSN Batch No Exp. OldMRP MRP Qty Free Rate Dis% Sch% GST% Gross
1 EMAMI THROMBOPHOB OINT 20 GMS 30049099 IB00933A 3/29 160.38 236.80 480.00 0 146.28 8.75 0.00 5.00 70214.40
2 AURO DUONEM ER 300 TAB 5*1*10 30049099 FA00394A 11/27 1688.06 1740.80 50.00 0 1061.56 37.10 0.00 5.00 53078.00
3 AURO DUONEM ER 300 TAB 5*1*10 30049099 FB00034A 12/27 1688.06 1740.80 30.00 0 1061.56 37.10 0.00 5.00 31846.80
4 ZYDUS HYPONAT O 15 TAB 2*5*10 30049099 BEC1086 2/28 747.90 848.38 80.00 0 492.74 34.10 0.00 5.00 39419.20
"""


class TestSatiijaDistributorsRate(unittest.TestCase):
    def test_ocr_rate_not_oldmrp(self):
        items = [
            {
                "product_description": "THROMBOPHOB OINT",
                "quantity": "480.00",
                "unit_price": "160.38",
                "total_amount": "77030.40",
                "lot_batch_number": "IB00933A",
                "additional_fields": {"mrp": "236.80"},
            },
            {
                "product_description": "DUONEM ER 300 TAB",
                "quantity": "50.00",
                "unit_price": "1688.06",
                "total_amount": "84403.00",
                "lot_batch_number": "FA00394A",
                "additional_fields": {"mrp": "1740.80"},
            },
            {
                "product_description": "DUONEM ER 300 TAB",
                "quantity": "30.00",
                "unit_price": "1688.06",
                "total_amount": "50641.80",
                "lot_batch_number": "FB00034A",
                "additional_fields": {"mrp": "1740.80"},
            },
            {
                "product_description": "HYPONAT O 15 TAB",
                "quantity": "80.00",
                "unit_price": "747.90",
                "total_amount": "59832.00",
                "lot_batch_number": "BEC1086",
                "additional_fields": {"mrp": "848.38"},
            },
        ]
        out = fix_satiija_distributors_rate_from_ocr(
            items,
            SATIJA_OCR,
            vendor="M/S SATIJA DISTRIBUTORS",
        )
        by_batch = {i["lot_batch_number"]: i for i in out}
        self.assertEqual(by_batch["IB00933A"]["unit_price"], "146.28")
        self.assertEqual(by_batch["IB00933A"]["total_amount"], "70214.40")
        self.assertEqual(by_batch["FA00394A"]["unit_price"], "1061.56")
        self.assertEqual(by_batch["FA00394A"]["total_amount"], "53078.00")
        self.assertEqual(by_batch["FB00034A"]["unit_price"], "1061.56")
        self.assertEqual(by_batch["BEC1086"]["unit_price"], "492.74")

    def test_vision_discount_derives_rate(self):
        items = [
            {
                "product_description": "THROMBOPHOB OINT",
                "quantity": "480.00",
                "unit_price": "160.38",
                "total_amount": "77030.40",
                "lot_batch_number": "IB00933A",
                "additional_fields": {
                    "mrp": "236.80",
                    "discount_percentage": "8.75",
                    "sch_percentage": "0.00",
                },
            }
        ]
        out = fix_satiija_distributors_rate_from_ocr(
            items,
            "",
            vendor="M/S SATIJA DISTRIBUTORS",
        )
        self.assertAlmostEqual(float(out[0]["unit_price"]), 146.35, places=1)
        self.assertAlmostEqual(float(out[0]["total_amount"]), 70248.0, places=0)


if __name__ == "__main__":
    unittest.main()
