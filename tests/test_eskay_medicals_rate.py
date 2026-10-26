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


if __name__ == "__main__":
    unittest.main()
