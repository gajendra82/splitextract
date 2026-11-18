"""YASH AGENCIES: AMOUNT parked as rate must not collapse qty→1."""
import unittest

from app import (
    fix_mrp_as_unit_price,
    fix_yash_agencies_rate_from_ocr,
    _parse_yash_agencies_rate_rows,
    _yash_line_items_rates_look_corrupt,
    _ocr_suggests_yash_agencies,
)


YASH_OCR = """
YASH AGENCIES TAX INVOICE
TaxInvNo : YE03417
InvDate : 24/06/2026
PRODUCT NAME PACK HSNCODE BATCH EXP QTY FREE OLDMRP M.R.P RATE DIS% AMOUNT GST%
"""


class TestYashAgenciesRate(unittest.TestCase):
    def test_detect_yash(self):
        self.assertTrue(_ocr_suggests_yash_agencies(YASH_OCR))

    def test_amount_as_rate_derives_unit_price_keeps_qty(self):
        item = {
            "product_description": "THROMBOPHOB GEL 30G",
            "quantity": "12",
            "unit_price": "2422.32",
            "total_amount": "2422.32",
        }
        out = fix_mrp_as_unit_price(
            item, vendor="YASH AGENCIES", ocr_text=YASH_OCR
        )
        self.assertEqual(out["quantity"], "12")
        self.assertAlmostEqual(float(out["unit_price"]), 201.86, places=2)

    def test_cintapro_amount_as_rate(self):
        item = {
            "product_description": "CINTAPRO TABLETS - 8.5",
            "quantity": "350",
            "unit_price": "31080.00",
            "total_amount": "31080.00",
        }
        out = fix_mrp_as_unit_price(
            item, vendor="YASH AGENCIES", ocr_text=YASH_OCR
        )
        self.assertEqual(out["quantity"], "350")
        self.assertAlmostEqual(float(out["unit_price"]), 88.80, places=2)

    def test_corrupt_detector_amount_as_rate(self):
        items = [
            {
                "product_description": "A",
                "quantity": "12",
                "unit_price": "2422.32",
                "total_amount": "2422.32",
            },
            {
                "product_description": "B",
                "quantity": "350",
                "unit_price": "31080.00",
                "total_amount": "31080.00",
            },
            {
                "product_description": "C",
                "quantity": "80",
                "unit_price": "0",
                "total_amount": "0",
            },
        ]
        self.assertTrue(_yash_line_items_rates_look_corrupt(items))

    def test_non_yash_still_collapses_qty_via_generic_path(self):
        # Without YASH marker, generic QTY-misread may still set qty=1
        item = {
            "product_description": "SOME PRODUCT",
            "quantity": "12",
            "unit_price": "2422.32",
            "total_amount": "2422.32",
        }
        out = fix_mrp_as_unit_price(item, vendor="OTHER", ocr_text="OTHER VENDOR")
        # Generic path: nearest_qty from total/rate = 1
        self.assertEqual(str(out["quantity"]), "1")

    def test_amount_invented_total_recovers_rate_from_mrp(self):
        # unit_price holds AMOUNT; total = qty × amount; MRP known
        item = {
            "product_description": "NATUROGEST INJECTION 100",
            "quantity": "40",
            "unit_price": "2542.00",
            "total_amount": "101680.00",
            "additional_fields": {"mrp": "208.72"},
        }
        out = fix_mrp_as_unit_price(
            item, vendor="YASH AGENCIES", ocr_text=YASH_OCR
        )
        self.assertEqual(out["quantity"], "40")
        self.assertAlmostEqual(float(out["unit_price"]), 63.55, places=2)
        self.assertAlmostEqual(float(out["total_amount"]), 2542.00, places=2)

    def test_corrupt_detector_invented_total_amount_as_rate(self):
        # YE06111: Gemini parked AMOUNT in unit_price and set total = qty×amount
        items = [
            {
                "product_description": "NUCOXIA 90",
                "quantity": "105",
                "unit_price": "23265.85",
                "total_amount": "2442914.25",
            },
            {
                "product_description": "NUCOXIA MR",
                "quantity": "360",
                "unit_price": "85543.20",
                "total_amount": "30795552.00",
            },
            {
                "product_description": "OXALGIN NANO GEL",
                "quantity": "200",
                "unit_price": "24404.00",
                "total_amount": "4880800.00",
            },
        ]
        self.assertTrue(_yash_line_items_rates_look_corrupt(items))


YE06111_TABLE_OCR = """
YASH AGENCIES TAX INVOICE
TaxInvNo : YE06111
PRODUCT NAME PACK HSNCODE BATCH EXP QTY FREE OLDMRP M.R.P RATE DIS% AMOUNT GST%
NUCOXIA P 18s 30049099 IB01373A 06/28 350 268.29 268.29 170.34 5.0 59619.00 5.0
NUCOXIA 90 15s 30049099 IB01099A 05/28 105 349.28 349.28 221.77 5.0 23285.85 5.0
NUCOXIA MR 10s 30049069 IB01857A 06/28 360 374.26 374.26 237.62 5.0 85543.20 5.0
NUCOXIA 60 15s 30049069 IB00803A 03/28 105 276.22 276.22 175.38 5.0 18414.90 5.0
CUTICOLOR BLACK 60GM 30049099 26D271 03/29 60 1799.00 1799.00 907.54 5.0 54452.40 18.0
CLOPITORVA 40 - 8.5 10s 30049099 CPR26003 04/28 40 390.23 390.23 249.74 5.0 9989.60 5.0
OXALGIN NANO GEL 10S 30049099 IB01947A 01/28 200 208.20 208.20 122.02 5.0 24404.00 5.0
NASOCLEAR NASAL DROP 20ML 30049099 TBO748A 04/29 800 72.98 72.98 40.94 5.0 32752.00 5.0
CLOPITORVA 10 CAPSULE - 8.5 15S 30049079 IB01339A 04/28 50 426.46 426.46 272.93 5.0 13646.50 5.0
BILOVAS TABS 10 30049099 IB01148A 04/29 100 359.90 359. 9p FFaRR 436.00 5.0
"""


class TestYashAgenciesYe06111OcrRate(unittest.TestCase):
    def test_parse_rate_column_not_mrp_or_amount(self):
        rows = _parse_yash_agencies_rate_rows(YE06111_TABLE_OCR)
        by_name = {
            r["product_description"].upper(): r for r in rows
        }
        self.assertGreaterEqual(len(rows), 9)
        self.assertAlmostEqual(by_name["NUCOXIA P"]["unit_price"], 170.34, places=2)
        self.assertAlmostEqual(by_name["NUCOXIA P"]["quantity"], 350.0, places=1)
        self.assertAlmostEqual(by_name["NUCOXIA P"]["total_amount"], 59619.00, places=2)
        self.assertAlmostEqual(by_name["NUCOXIA 90"]["unit_price"], 221.77, places=2)
        self.assertAlmostEqual(by_name["CUTICOLOR BLACK"]["unit_price"], 907.54, places=2)
        self.assertAlmostEqual(by_name["NASOCLEAR NASAL DROP"]["unit_price"], 40.94, places=2)
        # RATE must not be MRP or AMOUNT
        self.assertNotAlmostEqual(by_name["NUCOXIA P"]["unit_price"], 268.29, places=1)
        self.assertNotAlmostEqual(by_name["NUCOXIA 90"]["unit_price"], 23285.85, places=1)

    def test_restores_amount_as_rate_and_gst_as_rate(self):
        items = [
            {
                "product_description": "NUCOXIA P 158",
                "quantity": "350",
                "unit_price": "96.19",
                "total_amount": "33666.50",
                "lot_batch_number": "IB01373A",
            },
            {
                "product_description": "NUCOXIA 90 1Ss",
                "quantity": "105",
                "unit_price": "23265.85",
                "total_amount": "2442914.25",
                "lot_batch_number": "IB01099A",
            },
            {
                "product_description": "CUTICOLOR BLACK 60GM",
                "quantity": "60",
                "unit_price": "4452.40",
                "total_amount": "267144.00",
                "lot_batch_number": "26D271",
            },
            {
                "product_description": "NASOCLEAR NASAL DROP 20ML",
                "quantity": "800",
                "unit_price": "32782.00",
                "total_amount": "26225600.00",
                "lot_batch_number": "TBO748A",
            },
            {
                "product_description": "BILOVAS TABS 10",
                "quantity": "100",
                "unit_price": "5.0",
                "total_amount": "500.0",
                "lot_batch_number": "IB01148A",
            },
        ]
        out = fix_yash_agencies_rate_from_ocr(
            items, YE06111_TABLE_OCR, YE06111_TABLE_OCR, "YASH AGENCIES"
        )
        by_name = {i["product_description"]: i for i in out}
        self.assertAlmostEqual(float(by_name["NUCOXIA P 158"]["unit_price"]), 170.34, places=2)
        self.assertEqual(by_name["NUCOXIA P 158"]["quantity"], "350")
        self.assertAlmostEqual(float(by_name["NUCOXIA P 158"]["total_amount"]), 59619.00, places=2)
        self.assertAlmostEqual(float(by_name["NUCOXIA 90 1Ss"]["unit_price"]), 221.77, places=2)
        self.assertAlmostEqual(float(by_name["CUTICOLOR BLACK 60GM"]["unit_price"]), 907.54, places=2)
        self.assertAlmostEqual(float(by_name["NASOCLEAR NASAL DROP 20ML"]["unit_price"]), 40.94, places=2)
        # Stamp-damaged BILOVAS has no coherent RATE/AMOUNT — leave as-is
        self.assertAlmostEqual(float(by_name["BILOVAS TABS 10"]["unit_price"]), 5.0, places=2)

    def test_does_not_overwrite_already_correct_rate(self):
        items = [{
            "product_description": "NUCOXIA P",
            "quantity": "350",
            "unit_price": "170.34",
            "total_amount": "59619.00",
            "lot_batch_number": "IB01373A",
        }]
        out = fix_yash_agencies_rate_from_ocr(
            items, YE06111_TABLE_OCR, YE06111_TABLE_OCR, "YASH AGENCIES"
        )
        self.assertEqual(out[0]["unit_price"], "170.34")
        self.assertEqual(out[0]["quantity"], "350")

    def test_does_not_cross_match_clopitorva_strengths(self):
        items = [
            {
                "product_description": "CLOPITORVA 40 - 8.5 10s",
                "quantity": "40",
                "unit_price": "9989.60",
                "total_amount": "399584.00",
                "lot_batch_number": "CPR26003",
            },
            {
                "product_description": "CLOPITORVA 10 CAPSULE - 8.5 155",
                "quantity": "50",
                "unit_price": "13646.50",
                "total_amount": "682325.00",
                "lot_batch_number": "IB01339A",
            },
        ]
        out = fix_yash_agencies_rate_from_ocr(
            items, YE06111_TABLE_OCR, YE06111_TABLE_OCR, "YASH AGENCIES"
        )
        self.assertAlmostEqual(float(out[0]["unit_price"]), 249.74, places=2)
        self.assertEqual(out[0]["quantity"], "40")
        self.assertAlmostEqual(float(out[0]["total_amount"]), 9989.60, places=2)
        self.assertAlmostEqual(float(out[1]["unit_price"]), 272.93, places=2)
        self.assertEqual(out[1]["quantity"], "50")

    def test_non_yash_vendor_untouched(self):
        items = [{
            "product_description": "NUCOXIA P",
            "quantity": "350",
            "unit_price": "23265.85",
            "total_amount": "2442914.25",
        }]
        out = fix_yash_agencies_rate_from_ocr(
            items, "OTHER STOCKIST TAX INVOICE", "", "OTHER STOCKIST"
        )
        self.assertEqual(out[0]["unit_price"], "23265.85")


if __name__ == "__main__":
    unittest.main()
