"""ESKAY MEDICALS: Rate column must not be replaced by Amount/qty on Dis% rows."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _extract_line_items_for_validation,
    _parse_eskay_medicals_rate_rows,
    enforce_schema,
    fix_eskay_medicals_rate_from_ocr,
    fix_mrp_as_unit_price,
    ocr_suggests_eskay_medicals_marg_table,
    recover_missing_items_from_ocr,
    fix_eskay_medicals_dash_qty_from_ocr,
)

ESKAY_OCR = """
GST INVOICE
ESKAY MEDICALS ENTERPRISES M/s METRO MEDICALS Inv No : F003514
Mfr Qty Free Pack Item Description Batch Exp. HSN M.R.P Rate Dis. SGST Value CGST Value Amount
MANK 9 1 1'S BANDY PLUS A8AGZ009 7/28 30049021 31.73 24.18 0.0 2.50 5.22 2.50 5.22 219.36
MANK 50 10 10'S BISOHEART 2.5 TAB L75Z005 2/28 30043110 69.41 52.88 0.0 2.50 63.46 2.50 63.46 2665.16
MACL 200 - 10'S TIGEMAC 90 MT261313 2/28 30041010 343.14 261.44 20.0 2.50 1003.93 2.50 1003.93 42165.04
ZYDU 50 - 10'S VINGLYN SR EMV260665A 2/28 30049099 150.25 99.37 0.0 2.50 119.24 2.50 119.24 5008.24
SUN 5 - 10'S VSL#3 JMG1514A 12/27 30031000 451.88 344.29 0.0 2.50 41.31 2.50 41.31 1735.21
GRAND TOTAL 81547.00
"""

JACKSON_OCR = """
JACKSON MEDICALS Inv.No. D4151
AMLOPIN 5MG TAB 10 20.32 203.20
"""


class TestEskayMedicalsRate(unittest.TestCase):
    def test_detects_eskay_not_other_formats(self):
        self.assertTrue(ocr_suggests_eskay_medicals_marg_table(ESKAY_OCR))
        self.assertTrue(
            ocr_suggests_eskay_medicals_marg_table(
                ESKAY_OCR, "ESKAY MEDICALS ENTERPRISES"))
        self.assertFalse(ocr_suggests_eskay_medicals_marg_table(JACKSON_OCR))

    def test_parses_printed_rate_column(self):
        rows = _parse_eskay_medicals_rate_rows(ESKAY_OCR)
        tige = next(r for r in rows if "TIGEMAC" in r["product_description"].upper())
        bandy = next(r for r in rows if "BANDY" in r["product_description"].upper())
        self.assertEqual(tige["quantity"], 200.0)
        self.assertAlmostEqual(tige["unit_price"], 261.44, places=2)
        self.assertAlmostEqual(bandy["unit_price"], 24.18, places=2)

    def test_restores_discounted_rate_only(self):
        items = [
            {
                "product_description": "BANDY PLUS",
                "lot_batch_number": "A8AGZ009",
                "quantity": "9",
                "unit_price": "24.18",
                "total_amount": "219.36",
            },
            {
                "product_description": "TIGEMAC 90",
                "lot_batch_number": "MT261313",
                "quantity": "200",
                "unit_price": "210.83",
                "total_amount": "42165.04",
            },
            {
                "product_description": "VINGLYN SR",
                "lot_batch_number": "EMV260665A",
                "quantity": "50",
                "unit_price": "99.37",
                "total_amount": "5008.24",
            },
        ]
        out = fix_eskay_medicals_rate_from_ocr(
            items, ESKAY_OCR, "ESKAY MEDICALS ENTERPRISES")
        bandy = next(i for i in out if "BANDY" in i["product_description"].upper())
        tige = next(i for i in out if "TIGEMAC" in i["product_description"].upper())
        ving = next(i for i in out if "VINGLYN" in i["product_description"].upper())
        self.assertEqual(bandy["quantity"], "9")
        self.assertEqual(bandy["unit_price"], "24.18")
        self.assertEqual(bandy["total_amount"], "219.36")
        self.assertEqual(tige["quantity"], "200")
        self.assertEqual(tige["unit_price"], "261.44")
        self.assertEqual(tige["total_amount"], "42165.04")
        self.assertEqual(ving["quantity"], "50")
        self.assertEqual(ving["unit_price"], "99.37")

    def test_shared_mrp_fix_does_not_rewrite_discounted_rate(self):
        item = {
            "product_description": "TIGEMAC 90",
            "lot_batch_number": "MT261313",
            "quantity": "200",
            "unit_price": "261.44",
            "total_amount": "42165.04",
            "additional_fields": {"mrp": "343.14"},
        }
        out = fix_mrp_as_unit_price(
            item, vendor="ESKAY MEDICALS ENTERPRISES", ocr_text=ESKAY_OCR)
        self.assertEqual(out["quantity"], "200")
        self.assertEqual(out["unit_price"], "261.44")
        self.assertEqual(out["total_amount"], "42165.04")

    def test_enforce_schema_keeps_printed_rate(self):
        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "ESKAY MEDICALS ENTERPRISES",
                    "customer": "M/s METRO MEDICALS",
                    "invoice_no": "F003514",
                    "total": "81547.00",
                },
                "line_items": {
                    "items": [
                        {
                            "product_description": "BANDY PLUS",
                            "lot_batch_number": "A8AGZ009",
                            "quantity": "9",
                            "unit_price": "24.18",
                            "total_amount": "219.36",
                            "additional_fields": {"mrp": "31.73"},
                        },
                        {
                            "product_description": "TIGEMAC 90",
                            "lot_batch_number": "MT261313",
                            "quantity": "200",
                            "unit_price": "210.83",
                            "total_amount": "42165.04",
                            "additional_fields": {"mrp": "343.14"},
                        },
                    ],
                    "count": 2,
                },
                "ocr_text": ESKAY_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        bandy = next(i for i in out if "BANDY" in i["product_description"].upper())
        tige = next(i for i in out if "TIGEMAC" in i["product_description"].upper())
        self.assertEqual(float(bandy["quantity"]), 9.0)
        self.assertAlmostEqual(float(bandy["unit_price"]), 24.18, places=2)
        self.assertEqual(bandy["total_amount"], "219.36")
        self.assertEqual(float(tige["quantity"]), 200.0)
        self.assertAlmostEqual(float(tige["unit_price"]), 261.44, places=2)
        self.assertEqual(tige["total_amount"], "42165.04")

    def test_does_not_touch_other_formats(self):
        items = [{
            "product_description": "AMLOPIN 5MG TAB",
            "quantity": "10",
            "unit_price": "20.32",
            "total_amount": "203.20",
        }]
        fixed = fix_eskay_medicals_rate_from_ocr(items, JACKSON_OCR)
        self.assertEqual(fixed[0]["unit_price"], "20.32")
        self.assertEqual(fixed[0]["quantity"], "10")


ESKAY_F004173_OCR = """
GST INVOICE
ESKAY MEDICALS ENTERPRISES M/s SARJI PHARMA Inv No : F004173
Mfr Qty Free Pack Item Description Batch Exp. HSN M.R.P Rate Dis. SGST Value CGST Value Amount
ZYDU 4 - 30 ML BIOCLAR DRY SYR GD250051 9/27 30049099 148.99 74.49 0.0 2.50 7.00 2.50 7.00 294.08
ZYDU 56 - 30 ML BIOCLAR DRY SYR GD260006 12/27 30049099 148.99 74.49 0.0 2.50 98.03 2.50 98.03 4117.21
ZYDU 25 - 200GM GRD SF V/F CEC0010 3/28 19019090 483.44 310.00 0.0 2.50 182.13 2.50 182.13 7649.26
ZYDU 50 - VIAL MEROZA 1 GM INJ 1616005A 1/28 30042019 1017.77 125.00 0.0 2.50 146.88 2.50 146.88 6168.76
GRAND TOTAL 18229.00
"""

ESKAY_F004173_ITEMS = [
    {
        "product_description": "BIOCLAR DRY SYR",
        "lot_batch_number": "GD250051",
        "quantity": "4",
        "unit_price": "74.49",
        "total_amount": "294.08",
    },
    {
        "product_description": "BIOCLAR DRY SYR",
        "lot_batch_number": "GD260006",
        "quantity": "56",
        "unit_price": "74.49",
        "total_amount": "4117.21",
    },
    {
        "product_description": "GRD SF V/F",
        "lot_batch_number": "CEC0010",
        "quantity": "25",
        "unit_price": "310.00",
        "total_amount": "7649.26",
    },
    {
        "product_description": "VIAL MEROZA 1 GM INJ",
        "lot_batch_number": "1616005A",
        "quantity": "50",
        "unit_price": "125.00",
        "total_amount": "6168.76",
    },
]


class TestEskayMedicalsExtraProduct(unittest.TestCase):
    def test_does_not_recover_mfr_pack_fragment(self):
        out = recover_missing_items_from_ocr(
            [dict(x) for x in ESKAY_F004173_ITEMS], ESKAY_F004173_OCR)
        names = [i["product_description"] for i in out]
        self.assertEqual(len(out), 4)
        self.assertNotIn("ZYDU 50 - VIAL", names)
        self.assertEqual(names.count("BIOCLAR DRY SYR"), 2)
        self.assertEqual(out[0]["quantity"], "4")
        self.assertEqual(out[1]["quantity"], "56")
        self.assertEqual(out[3]["unit_price"], "125.00")

    def test_enforce_schema_keeps_repeated_bioclar_drops_phantom(self):
        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "ESKAY MEDICALS ENTERPRISES",
                    "customer": "SARJI PHARMA",
                    "invoice_no": "F004173",
                    "total": "18229.00",
                },
                "line_items": {
                    "items": [dict(x) for x in ESKAY_F004173_ITEMS],
                    "count": 4,
                },
                "ocr_text": ESKAY_F004173_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        names = [i["product_description"] for i in out]
        self.assertEqual(len(out), 4)
        self.assertFalse(any("ZYDU 50" in n.upper() for n in names))
        self.assertEqual(names.count("BIOCLAR DRY SYR"), 2)
        meroza = next(i for i in out if "MEROZA" in i["product_description"].upper())
        self.assertEqual(float(meroza["quantity"]), 50.0)
        self.assertAlmostEqual(float(meroza["unit_price"]), 125.00, places=2)


ESKAY_DASH_QTY_OCR = """
GST INVOICE
ESKAY MEDICALS ENTERPRISES M/s SARJI SS PHARMA Inv No : F003673
Mfr Qty Free Pack Item Description Batch Exp. HSN M.R.P Rate Dis. SGST Value CGST Value Amount
LINU 20 2 10'S CITALIN LITE D2509088LLB 8/27 30049099 120.15 91.54 0.0 2.50 43.02 2.50 43.02 1806.99
LINU - 2 10'S CITALIN LITE D2511041LLB 10/27 30049099 120.15 91.54 0.0 2.50 0.00 2.50 0.00 0.00
LINU 25 5 10'S CITALIN LITE D2601065LLB 12/27 30049099 120.15 91.54 0.0 2.50 53.78 2.50 53.78 2258.75
MACL 30 - 15'S ETIZOLA 0.25 18260371A 12/29 30049088 76.40 58.21 0.0 2.50 41.04 2.50 41.04 1723.60
GRAND TOTAL 86451.00
"""


class TestEskayMedicalsDashQty(unittest.TestCase):
    def test_restores_dash_qty_not_free_pack(self):
        items = [
            {
                "product_description": "CITALIN LITE",
                "lot_batch_number": "D2509088LLB",
                "quantity": "20",
                "unit_price": "91.54",
                "total_amount": "1806.99",
                "additional_fields": {"free_quantity": "2"},
            },
            {
                "product_description": "CITALIN LITE",
                "lot_batch_number": "D2511041LLB",
                "quantity": "2",
                "unit_price": "91.54",
                "total_amount": "0.00",
                "additional_fields": {"free_quantity": "2"},
            },
            {
                "product_description": "CITALIN LITE",
                "lot_batch_number": "D2601065LLB",
                "quantity": "25",
                "unit_price": "91.54",
                "total_amount": "2258.75",
                "additional_fields": {"free_quantity": "5"},
            },
            {
                "product_description": "ETIZOLA 0.25",
                "lot_batch_number": "18260371A",
                "quantity": "30",
                "unit_price": "58.21",
                "total_amount": "1723.60",
                "additional_fields": {"free_quantity": "-"},
            },
        ]
        out = fix_eskay_medicals_dash_qty_from_ocr(
            items, ESKAY_DASH_QTY_OCR, "ESKAY MEDICALS ENTERPRISES")
        paid = next(i for i in out if i["lot_batch_number"] == "D2509088LLB")
        dash = next(i for i in out if i["lot_batch_number"] == "D2511041LLB")
        paid2 = next(i for i in out if i["lot_batch_number"] == "D2601065LLB")
        etiz = next(i for i in out if i["lot_batch_number"] == "18260371A")
        self.assertEqual(paid["quantity"], "20")
        self.assertEqual(paid["unit_price"], "91.54")
        self.assertEqual(dash["quantity"], "-")
        self.assertEqual(dash["unit_price"], "91.54")
        self.assertEqual(dash["total_amount"], "0.00")
        self.assertEqual(dash["additional_fields"]["free_quantity"], "2")
        self.assertEqual(paid2["quantity"], "25")
        self.assertEqual(etiz["quantity"], "30")

    def test_does_not_touch_other_formats(self):
        items = [{
            "product_description": "AMLOPIN 5MG TAB",
            "lot_batch_number": "D2511041LLB",
            "quantity": "2",
            "unit_price": "20.32",
            "total_amount": "0.00",
        }]
        fixed = fix_eskay_medicals_dash_qty_from_ocr(items, JACKSON_OCR)
        self.assertEqual(fixed[0]["quantity"], "2")

    def test_enforce_schema_keeps_dash_qty(self):
        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "ESKAY MEDICALS ENTERPRISES",
                    "customer": "SARJI SS PHARMA",
                    "invoice_no": "F003673",
                    "total": "86451.00",
                },
                "line_items": {
                    "items": [
                        {
                            "product_description": "CITALIN LITE",
                            "lot_batch_number": "D2509088LLB",
                            "quantity": "20",
                            "unit_price": "91.54",
                            "total_amount": "1806.99",
                            "additional_fields": {"free_quantity": "2"},
                        },
                        {
                            "product_description": "CITALIN LITE",
                            "lot_batch_number": "D2511041LLB",
                            "quantity": "2",
                            "unit_price": "91.54",
                            "total_amount": "0.00",
                            "additional_fields": {"free_quantity": "2"},
                        },
                    ],
                    "count": 2,
                },
                "ocr_text": ESKAY_DASH_QTY_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        dash = next(i for i in out if i["lot_batch_number"] == "D2511041LLB")
        paid = next(i for i in out if i["lot_batch_number"] == "D2509088LLB")
        self.assertEqual(str(dash["quantity"]).strip(), "-")
        self.assertEqual(float(paid["quantity"]), 20.0)
        self.assertAlmostEqual(float(dash["unit_price"]), 91.54, places=2)
        self.assertEqual(dash["total_amount"], "0.00")


ESKAY_F007147_OCR = """
GST INVOICE
ESKAY MEDICALS ENTERPRISES M/s METRO MEDICALS Inv No : F007147
Mfr Qty Free Pack Item Description Batch Exp. HSN M.R.P Rate Dis. SGST Value CGST Value Amount
LUPI 10 - 10'S AJADUO 25/5 JC01095 3/28 30049099 214.25 163.24 0.0 2.50 39.18 2.50 39.18 1645.46
LUPI 26 - 15'S CARVISTAR 3.125 COA-26009 2/28 30049099 60.50 46.10 0.0 2.50 28.77 2.50 28.77 1208.20
LINU 10 2 10'S COGNITAM 800 COMB26001 5/28 30041010 180.05 137.18 0.0 2.50 32.92 2.50 32.92 1382.77
CHE 10 4 4'S D3 GIFT SOFT CAP 3D260218D 2/28 30043110 121.87 92.85 0.0 2.50 22.28 2.50 22.28 935.92
RANB 3 - 10GM FUNGICROS CREAM ZND0018 3/28 30043110 202.00 153.90 0.0 2.50 11.08 2.50 11.08 465.39
H&H 1 - 50ML KZ LOTION CC226 11/27 30049029 285.94 217.86 0.0 2.50 5.23 2.50 5.23 219.61
COG 50 15 10'S METOHEAL XL R25/2.5 T26B803A 1/28 30049074 155.00 118.10 0.0 2.50 141.72 2.50 141.72 5952.24
CADE 10 4 15'S VILAPIL 50 LGQ01/217/10 12/27 30049099 126.00 96.00 0.0 2.50 23.04 2.50 23.04 967.68
MACL 2 - 120'S THYROX 125 16260221A 1/28 30043990 216.63 165.06 0.0 2.50 7.92 2.50 7.92 332.76
GRAND TOTAL 33279.00
"""


class TestEskayMedicalsF007147QtyRate(unittest.TestCase):
    def test_parses_pack_gm_ml_hyphen_and_slash_batch(self):
        rows = _parse_eskay_medicals_rate_rows(ESKAY_F007147_OCR)
        by_name = {
            r["product_description"].upper(): r for r in rows
        }

        def q(name):
            row = next(r for n, r in by_name.items() if name in n)
            return row["quantity"], row["unit_price"]

        self.assertEqual(q("AJADUO")[0], 10.0)
        self.assertAlmostEqual(q("AJADUO")[1], 163.24, places=2)
        self.assertEqual(q("CARVISTAR")[0], 26.0)
        self.assertAlmostEqual(q("CARVISTAR")[1], 46.10, places=2)
        self.assertEqual(q("FUNGICROS")[0], 3.0)
        self.assertAlmostEqual(q("FUNGICROS")[1], 153.90, places=2)
        self.assertEqual(q("KZ LOTION")[0], 1.0)
        self.assertAlmostEqual(q("KZ LOTION")[1], 217.86, places=2)
        self.assertEqual(q("METOHEAL")[0], 50.0)
        self.assertAlmostEqual(q("METOHEAL")[1], 118.10, places=2)
        self.assertEqual(q("VILAPIL")[0], 10.0)
        self.assertAlmostEqual(q("VILAPIL")[1], 96.00, places=2)
        self.assertEqual(q("THYROX")[0], 2.0)
        self.assertAlmostEqual(q("THYROX")[1], 165.06, places=2)

    def test_restores_pack_as_qty_and_mrp_as_rate(self):
        items = [
            {
                "product_description": "AJADUO 25/5",
                "lot_batch_number": "JC01095",
                "quantity": "10",
                "unit_price": "214.25",
                "total_amount": "1645.46",
            },
            {
                "product_description": "CARVISTAR 3.125",
                "lot_batch_number": "COA-26009",
                "quantity": "15",
                "unit_price": "60.50",
                "total_amount": "1208.20",
            },
            {
                "product_description": "FUNGICROS CREAM",
                "lot_batch_number": "ZND0018",
                "quantity": "10",
                "unit_price": "202.00",
                "total_amount": "465.39",
            },
            {
                "product_description": "KZ LOTION",
                "lot_batch_number": "CC226",
                "quantity": "50",
                "unit_price": "285.94",
                "total_amount": "219.61",
            },
            {
                "product_description": "METOHEAL XL R25/2.5",
                "lot_batch_number": "T26B803A",
                "quantity": "10",
                "unit_price": "155.00",
                "total_amount": "5952.24",
            },
            {
                "product_description": "VILAPIL 50",
                "lot_batch_number": "LGQ01/217/10",
                "quantity": "15",
                "unit_price": "126.00",
                "total_amount": "967.68",
            },
            {
                "product_description": "THYROX 125",
                "lot_batch_number": "16260221A",
                "quantity": "120",
                "unit_price": "216.63",
                "total_amount": "332.76",
            },
        ]
        out = fix_eskay_medicals_rate_from_ocr(
            items, ESKAY_F007147_OCR, "ESKAY MEDICALS ENTERPRISES")
        ajaduo = next(i for i in out if "AJADUO" in i["product_description"].upper())
        carv = next(i for i in out if "CARVISTAR" in i["product_description"].upper())
        fung = next(i for i in out if "FUNGICROS" in i["product_description"].upper())
        kz = next(i for i in out if "KZ LOTION" in i["product_description"].upper())
        meto = next(i for i in out if "METOHEAL" in i["product_description"].upper())
        vila = next(i for i in out if "VILAPIL" in i["product_description"].upper())
        thyro = next(i for i in out if "THYROX" in i["product_description"].upper())
        self.assertEqual(ajaduo["quantity"], "10")
        self.assertEqual(ajaduo["unit_price"], "163.24")
        self.assertEqual(carv["quantity"], "26")
        self.assertEqual(carv["unit_price"], "46.10")
        self.assertEqual(fung["quantity"], "3")
        self.assertEqual(fung["unit_price"], "153.90")
        self.assertEqual(kz["quantity"], "1")
        self.assertEqual(kz["unit_price"], "217.86")
        self.assertEqual(meto["quantity"], "50")
        self.assertEqual(meto["unit_price"], "118.10")
        self.assertEqual(vila["quantity"], "10")
        self.assertEqual(vila["unit_price"], "96.00")
        self.assertEqual(thyro["quantity"], "2")
        self.assertEqual(thyro["unit_price"], "165.06")
        self.assertEqual(ajaduo["total_amount"], "1645.46")
        self.assertEqual(meto["total_amount"], "5952.24")

    def test_enforce_schema_restores_qty_and_rate(self):
        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "ESKAY MEDICALS ENTERPRISES",
                    "customer": "M/s METRO MEDICALS",
                    "invoice_no": "F007147",
                    "total": "33279.00",
                },
                "line_items": {
                    "items": [
                        {
                            "product_description": "METOHEAL XL R25/2.5",
                            "lot_batch_number": "T26B803A",
                            "quantity": "10",
                            "unit_price": "155.00",
                            "total_amount": "5952.24",
                            "additional_fields": {"mrp": "155.00"},
                        },
                        {
                            "product_description": "FUNGICROS CREAM",
                            "lot_batch_number": "ZND0018",
                            "quantity": "10",
                            "unit_price": "202.00",
                            "total_amount": "465.39",
                            "additional_fields": {"mrp": "202.00"},
                        },
                    ],
                    "count": 2,
                },
                "ocr_text": ESKAY_F007147_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        meto = next(i for i in out if "METOHEAL" in i["product_description"].upper())
        fung = next(i for i in out if "FUNGICROS" in i["product_description"].upper())
        self.assertEqual(float(meto["quantity"]), 50.0)
        self.assertAlmostEqual(float(meto["unit_price"]), 118.10, places=2)
        self.assertEqual(float(fung["quantity"]), 3.0)
        self.assertAlmostEqual(float(fung["unit_price"]), 153.90, places=2)


if __name__ == "__main__":
    unittest.main()
