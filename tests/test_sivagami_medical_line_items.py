"""SIVAGAMI MEDICAL landscape CREDIT BILL: restore name/qty/rate from table OCR."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _extract_line_items_for_validation,
    enforce_schema,
    extract_sivagami_medical_line_items_from_ocr,
    fix_sivagami_medical_line_items_from_ocr,
    ocr_suggests_sivagami_medical,
)

SIVAGAMI_OCR = """
SIVAGAMI MEDICAL Trichy Main Road
MEENAKSHI HOSPITAL PHARMACY
Inv.No : SA 021061 Inv.Dt : 18/06/2026
Term : CREDIT BILL
Mfr Product Name Pack Hsn Qty Free Batch Exp MRP Rate Sch Value
ZYDUS *CLOPITORVA 20MG 10's 30049079 100 18007224 02/28 0.00 392.84 224.87 0.00 0.00 0.00 0.00 5 22487.00
ZYDUS *DEXONA INJ 1CC 30043913 12 NB001404 07/27 0.00 108.70 82.70 0.00 0.00 0.00 0.00 5 992.40
ZYDUS *THROMBOPHOB OINT 20GM 30049099 40 ID01106N 04/29 0.00 236.80 139.85 0.00 0.00 0.00 0.00 5 5594.00
ZYDUS *DAPAGLYN M 500 15'S 30049099 20 EMV260704A 02/27 0.00 238.58 151.64 0.00 0.00 0.00 0.00 5 3032.80
ZYDUS *DAPAGLYN S 10/100 TAB 10'S 30049099 200 EMV260060 01/28 0.00 260.91 158.87 0.00 0.00 0.00 0.00 5 31774.00
Total Qty: 372 Total Items: 5 Bill Amount : 67074.00
"""

SIVAGAMI_NOISY_OCR = """
ZADUS *CLOPITORV 4 20MG 10's 30049079 100 1pee22 0228 0.00 392.84 224.87 0.00 0.00 0.00 0.00 5 22487.0
ZYDUS *DENONAING 10 2ML 30043913 12 NB001404 0727 0.00 108.70 82.70 0.00 0.00 0.00 0.00 5 992.40
ZADUS *THROMBOPIIOR OINT 30GM 30049099 40 ID01106N 0429 0.00 236.80 139.85 0.00 0.00 0.00 0.00 5 5594.00
ZYDUS *DAPAGIVN M 500 15s 30049099 20 EMV260704 0227 0.00 238.58 151.64 0.00 0.00 0.00 0.00 5 3032.80
ZYDUS *DAPAGIVS S 10/100 TAB 10s 30049099 200 EMV260060 0128 0.00 260.91 158.87 0.00 0.00 0.00 0.00 5 31774.0
SIVAGAMI MEDICAL Inv.No SA 021061
"""

JACKSON_OCR = """
JACKSON MEDICALS Inv.No. D4151
AMLOPIN 5MG TAB 10 20.32 203.20
"""


class TestSivagamiMedicalLineItems(unittest.TestCase):
    def test_detector(self):
        self.assertTrue(ocr_suggests_sivagami_medical(SIVAGAMI_OCR))
        self.assertTrue(
            ocr_suggests_sivagami_medical("", "Stvagam Medical"))
        self.assertFalse(ocr_suggests_sivagami_medical(JACKSON_OCR))

    def test_parses_qty_rate_name(self):
        rows = extract_sivagami_medical_line_items_from_ocr(SIVAGAMI_OCR)
        by_name = {r["product_description"]: r for r in rows}
        self.assertIn("CLOPITORVA 20MG", by_name)
        self.assertEqual(by_name["CLOPITORVA 20MG"]["quantity"], 100.0)
        self.assertAlmostEqual(by_name["CLOPITORVA 20MG"]["unit_price"], 224.87, places=2)
        self.assertIn("DEXONA INJ", by_name)
        self.assertEqual(by_name["DEXONA INJ"]["quantity"], 12.0)
        self.assertAlmostEqual(by_name["DEXONA INJ"]["unit_price"], 82.70, places=2)
        throm = next(n for n in by_name if "THROMBOPHOB" in n)
        self.assertEqual(by_name[throm]["quantity"], 40.0)
        self.assertAlmostEqual(by_name[throm]["unit_price"], 139.85, places=2)
        self.assertEqual(by_name["DAPAGLYN M 500"]["quantity"], 20.0)
        self.assertAlmostEqual(by_name["DAPAGLYN M 500"]["unit_price"], 151.64, places=2)
        self.assertEqual(by_name["DAPAGLYN S 10/100 TAB"]["quantity"], 200.0)
        self.assertAlmostEqual(by_name["DAPAGLYN S 10/100 TAB"]["unit_price"], 158.87, places=2)

    def test_parses_noisy_ocr_names(self):
        rows = extract_sivagami_medical_line_items_from_ocr(SIVAGAMI_NOISY_OCR)
        names = [r["product_description"] for r in rows]
        self.assertTrue(any("CLOPITORVA" in n for n in names))
        self.assertTrue(any("DEXONA" in n for n in names))
        self.assertTrue(any("THROMBOPHOB" in n for n in names))
        self.assertTrue(any("DAPAGLYN M" in n for n in names))
        self.assertTrue(any("DAPAGLYN S" in n and "10/100" in n for n in names))
        clopi = next(r for r in rows if "CLOPITORVA" in r["product_description"])
        self.assertEqual(clopi["quantity"], 100.0)
        self.assertAlmostEqual(clopi["unit_price"], 224.87, places=2)

    def test_fixes_invented_names_wrong_qty_rate(self):
        items = [
            {
                "product_description": "TULI",
                "quantity": "200",
                "unit_price": "158.87",
                "total_amount": "31774.00",
                "additional_fields": {"mrp": "210.91"},
            },
            {
                "product_description": "NATUROGEST AQ",
                "quantity": "20",
                "unit_price": "151.64",
                "total_amount": "3032.80",
                "additional_fields": {"mrp": "180.00"},
            },
            {
                "product_description": "INTRAVENOUS FLUID",
                "quantity": "10",
                "unit_price": "139.85",
                "total_amount": "1398.50",
                "additional_fields": {"mrp": "230.00"},
            },
            {
                "product_description": "INTRAVENOUS FLUID",
                "quantity": "12",
                "unit_price": "108.78",
                "total_amount": "1305.36",
                "additional_fields": {"mrp": "180.00"},
            },
            {
                "product_description": "INTRAVENOUS FLUID",
                "quantity": "100",
                "unit_price": "191.51",
                "total_amount": "19151.00",
                "additional_fields": {"mrp": "224.87"},
            },
        ]
        totals = [i["total_amount"] for i in items]
        out = fix_sivagami_medical_line_items_from_ocr(
            items, SIVAGAMI_OCR, SIVAGAMI_OCR, "SIVAGAMI MEDICAL")
        by_total = {i["total_amount"]: i for i in out}
        dapa_s = by_total["31774.00"]
        dapa_m = by_total["3032.80"]
        throm = by_total["1398.50"]
        dex = by_total["1305.36"]
        clopi = by_total["19151.00"]
        self.assertIn("DAPAGLYN S", dapa_s["product_description"])
        self.assertEqual(dapa_s["quantity"], "200")
        self.assertEqual(dapa_s["unit_price"], "158.87")
        self.assertIn("DAPAGLYN M", dapa_m["product_description"])
        self.assertEqual(dapa_m["quantity"], "20")
        self.assertEqual(dapa_m["unit_price"], "151.64")
        self.assertIn("THROMBOPHOB", throm["product_description"])
        self.assertEqual(throm["quantity"], "40")
        self.assertEqual(throm["unit_price"], "139.85")
        self.assertEqual(throm["total_amount"], "1398.50")
        self.assertIn("DEXONA", dex["product_description"])
        self.assertEqual(dex["quantity"], "12")
        self.assertEqual(dex["unit_price"], "82.70")
        self.assertIn("CLOPITORVA", clopi["product_description"])
        self.assertEqual(clopi["quantity"], "100")
        self.assertEqual(clopi["unit_price"], "224.87")
        self.assertEqual([i["total_amount"] for i in out], totals)

    def test_does_not_touch_other_formats(self):
        items = [{
            "product_description": "AMLOPIN 5MG TAB",
            "quantity": "10",
            "unit_price": "20.32",
            "total_amount": "203.20",
        }]
        out = fix_sivagami_medical_line_items_from_ocr(
            items, JACKSON_OCR, "", "JACKSON MEDICALS")
        self.assertEqual(out[0]["product_description"], "AMLOPIN 5MG TAB")
        self.assertEqual(out[0]["quantity"], "10")
        self.assertEqual(out[0]["unit_price"], "20.32")

    def test_enforce_schema(self):
        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "SIVAGAMI MEDICAL",
                    "customer": "MEENAKSHI HOSPITAL PHARMACY",
                    "invoice_no": "SA 021061",
                    "invoice_date": "2026-06-18",
                    "total": "67074.00",
                },
                "line_items": {
                    "items": [{
                        "product_description": "TULI",
                        "quantity": "200",
                        "unit_price": "158.87",
                        "total_amount": "31774.00",
                    }],
                    "count": 1,
                },
                "ocr_text": SIVAGAMI_OCR,
                "sivagami_table_ocr": SIVAGAMI_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        self.assertEqual(len(out), 1)
        self.assertIn("DAPAGLYN S", out[0]["product_description"].upper())
        self.assertEqual(float(out[0]["quantity"]), 200.0)
        self.assertAlmostEqual(float(out[0]["unit_price"]), 158.87, places=2)
        self.assertEqual(out[0]["total_amount"], "31774.00")

    def test_proluton_qty_is_billed_qty_not_gst_value_stitch(self):
        """SA 022372: Qty 10 must not become 43 via GST% 5 + Value 1517."""
        ocr = """
SIVAGAMI MEDICAL Inv.No : SA 022372
Mfr Product Name Pack Hsn Qty Free Batch Exp MRP Rate Value
ZYDUS *IMEGLYN 500MG 10'S 30049099 50 EMV260767C 03/28 0.00 85.64 54.31 0.00 0.00 0.00 0.00 5 2715.50
ZYDUS *THROMBOPHOB OINT 30GM 30GM 30049099 10 IB01106A 04/29 0.00 236.80 139.87 0.00 0.00 0.00 0.00 5 1398.70
ZYDUS *PROLUTON DEPOT 250MG AMP 1ML 30043190 10 GB0125A 01/29 0.00 287.00 151.70 0.00 0.00 0.00 0.00 5 1517.00
"""
        noisy = (
            "ZYDUS _*PROLUTON DEPOT 250MG AMP IML e190 | 10° "
            '"| GBOI2SA "01729 0,00 |: 287.00 | 151.70 0.00 | 0.00 0.00 000 5 1517.00 |'
        )
        rows = extract_sivagami_medical_line_items_from_ocr(ocr + "\n" + noisy)
        prol = [r for r in rows if "PROLUTON" in r["product_description"]]
        self.assertTrue(prol)
        self.assertEqual(prol[0]["quantity"], 10.0)
        self.assertAlmostEqual(prol[0]["unit_price"], 151.70, places=2)
        items = [
            {
                "product_description": "IMEGLYN SOOMG GERS",
                "quantity": "50",
                "unit_price": "54.31",
                "total_amount": "2715.50",
            },
            {
                "product_description": "THROMBOPHOB OINT 30GM",
                "quantity": "10",
                "unit_price": "139.87",
                "total_amount": "1398.70",
            },
            {
                "product_description": "PROLUTON DEPOT 250MG AMP IML E190 10",
                "quantity": "43",
                "unit_price": "151.70",
                "total_amount": "1517.00",
            },
        ]
        snapshot = [
            (i["product_description"], i["quantity"], i["unit_price"], i["total_amount"])
            for i in items
        ]
        out = fix_sivagami_medical_line_items_from_ocr(
            items, ocr, noisy, "SIVAGAMI MEDICAL")
        self.assertEqual(out[2]["quantity"], "10")
        self.assertEqual(out[2]["unit_price"], "151.70")
        self.assertEqual(out[2]["total_amount"], "1517.00")
        self.assertEqual(out[0]["product_description"], snapshot[0][0])
        self.assertEqual(out[0]["quantity"], snapshot[0][1])
        self.assertEqual(out[0]["unit_price"], snapshot[0][2])
        self.assertEqual(out[1]["product_description"], snapshot[1][0])
        self.assertEqual(out[1]["quantity"], snapshot[1][1])
        self.assertEqual(out[1]["unit_price"], snapshot[1][2])
        self.assertIn("PROLUTON", out[2]["product_description"])

    def test_clopitorva_10mg_rate_not_mrp_when_comma_decimal(self):
        ocr = """
SIVAGAMI MEDICAL Inv.No : SA 017135
ZYDUS *CLOPITORVA 10MG 15'S 30049079 100 IB00188A 12/27 0.00 426.46 244,00 0.00 0.00 0.00 0.00 5 24400.0
ZYDUS *CLOPITORVA 20MG 10's 30049079 100 IB00722A 02/28 0.00 392.54 224.87 0.00 0.00 0.00 0.00 5 22487.0
"""
        rows = extract_sivagami_medical_line_items_from_ocr(ocr)
        ten = next(r for r in rows if "10MG" in r["product_description"])
        self.assertAlmostEqual(ten["unit_price"], 244.00, places=2)
        items = [
            {
                "product_description": "CWPITORVA 10MG",
                "quantity": "57",
                "unit_price": "426.46",
                "total_amount": "24400.00",
                "additional_fields": {"mrp": "426.46"},
            },
            {
                "product_description": "CWPITORVA 20MG",
                "quantity": "100",
                "unit_price": "224.87",
                "total_amount": "22487.00",
            },
        ]
        out = fix_sivagami_medical_line_items_from_ocr(
            items, ocr, ocr, "SIVAGAMI MEDICAL")
        self.assertEqual(out[0]["unit_price"], "244.00")
        self.assertEqual(out[0]["quantity"], "57")
        self.assertEqual(out[0]["product_description"], "CWPITORVA 10MG")
        self.assertEqual(out[1]["unit_price"], "224.87")
        self.assertEqual(out[1]["quantity"], "100")


if __name__ == "__main__":
    unittest.main()
