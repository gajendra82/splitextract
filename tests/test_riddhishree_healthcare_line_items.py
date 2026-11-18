"""RIDDHISHREE HEALTHCARE: stamp-safe qty from RATE×AMOUNT; Vision table recovery."""
import unittest

from app import (
    _parse_riddhishree_product_rows,
    _parse_riddhishree_rate_amount_band,
    _riddhishree_parse_irn_candidate,
    _riddhishree_qty_from_rate_amount,
    _riddhishree_rows_reconcile,
    fix_riddhishree_healthcare_line_items_from_ocr,
    fix_riddhishree_healthcare_summary_from_ocr,
    ocr_suggests_riddhishree_healthcare,
)


# Verified against printed RATE/AMOUNT columns and footer Total QTY 9655 /
# TAXABLE 1019317.50 (qty = amount÷rate). User JSON had wrong qty on
# VINGLYN M 500MG (300) and ZYTANIX 2.5MG (305) — PDF math is authoritative.
VERIFIED_VISION_ROWS = [
    {"product_description": "AMLODAC 2.5MG TAB", "lot_batch_number": "IB00942A",
     "hsn_code": "300490", "quantity": 700.0, "unit_price": 30.03, "total_amount": 21021.0},
    {"product_description": "AMLODAC 5MG TAB", "lot_batch_number": "IB01638A",
     "hsn_code": "300490", "quantity": 800.0, "unit_price": 41.90, "total_amount": 33520.0},
    {"product_description": "AMLODAC AT TAB", "lot_batch_number": "IB01057A",
     "hsn_code": "300490", "quantity": 800.0, "unit_price": 138.48, "total_amount": 110784.0},
    {"product_description": "ATORVA 10MG TAB", "lot_batch_number": "IB01296A",
     "hsn_code": "300490", "quantity": 1000.0, "unit_price": 41.70, "total_amount": 41700.0},
    {"product_description": "ATORVA 5MG TAB", "lot_batch_number": "IB00976A",
     "hsn_code": "300490", "quantity": 450.0, "unit_price": 84.11, "total_amount": 37849.5},
    {"product_description": "ATORVA 80MG TAB", "lot_batch_number": "IB01646B",
     "hsn_code": "300490", "quantity": 500.0, "unit_price": 227.03, "total_amount": 113515.0},
    {"product_description": "ATORVA ASP 75MG CAP", "lot_batch_number": "26S2GCA285",
     "hsn_code": "300490", "quantity": 200.0, "unit_price": 28.97, "total_amount": 5794.0},
    {"product_description": "LOSACAR 25MG TAB", "lot_batch_number": "SB00386A",
     "hsn_code": "300490", "quantity": 400.0, "unit_price": 49.94, "total_amount": 19976.0},
    {"product_description": "LOSACAR 50MG TAB", "lot_batch_number": "SB00406A",
     "hsn_code": "300490", "quantity": 400.0, "unit_price": 57.40, "total_amount": 22960.0},
    {"product_description": "LOSACAR H TAB", "lot_batch_number": "SB00011A",
     "hsn_code": "300490", "quantity": 400.0, "unit_price": 123.44, "total_amount": 49376.0},
    {"product_description": "NEBULA 5MG TAB", "lot_batch_number": "BEC1006",
     "hsn_code": "300490", "quantity": 100.0, "unit_price": 101.72, "total_amount": 10172.0},
    {"product_description": "OLMY 10MG TAB", "lot_batch_number": "SB00342A",
     "hsn_code": "300490", "quantity": 400.0, "unit_price": 77.53, "total_amount": 31012.0},
    {"product_description": "OLMY 20MG TAB", "lot_batch_number": "SB00456A",
     "hsn_code": "300490", "quantity": 600.0, "unit_price": 111.53, "total_amount": 66918.0},
    {"product_description": "OLMY 40MG TAB", "lot_batch_number": "SB00521A",
     "hsn_code": "300490", "quantity": 200.0, "unit_price": 166.64, "total_amount": 33328.0},
    {"product_description": "OLMY D 20MG TAB", "lot_batch_number": "MT261895",
     "hsn_code": "300490", "quantity": 200.0, "unit_price": 109.79, "total_amount": 21958.0},
    {"product_description": "VINGLYN M 1000MG TAB", "lot_batch_number": "AXC1011",
     "hsn_code": "300490", "quantity": 300.0, "unit_price": 82.39, "total_amount": 24717.0},
    {"product_description": "VINGLYN M 500MG TAB", "lot_batch_number": "AXC1019",
     "hsn_code": "300490", "quantity": 500.0, "unit_price": 90.63, "total_amount": 45315.0},
    {"product_description": "VINGLYN TAB", "lot_batch_number": "AXC1016",
     "hsn_code": "300490", "quantity": 400.0, "unit_price": 64.52, "total_amount": 25808.0},
    {"product_description": "ZYTANIX 2.5MG 15TAB", "lot_batch_number": "IB01388A",
     "hsn_code": "300490", "quantity": 705.0, "unit_price": 185.20, "total_amount": 130566.0},
    {"product_description": "ZYTANIX 5MG 15TAB", "lot_batch_number": "IB01387A",
     "hsn_code": "300490", "quantity": 600.0, "unit_price": 288.38, "total_amount": 173028.0},
]

FOOTER_OCR = """
RIDDHISHREE HEALTHCARE TAX INVOICE
Inv No : RSB2627/091 Inv Dt : 31/08/2026
GST : 27ABEFR6671D1Z2
GST : 27AACPS6252J1ZE
Total Prod : 20 Total QTY : 9655
TAXABLE 1019317.50 CGST 2.5% 25482.96 SGST 2.5% 25482.96 TOTAL AMT 1070283.42
TO PAY 1069264.00
IRN NO : 806b64d30714462a503ecad9dd551c03b28c1ea5c30dcb29e7fd0de513398221
"""


class TestRiddhishreeHealthcare(unittest.TestCase):
    def test_detects_riddhishree(self):
        self.assertTrue(ocr_suggests_riddhishree_healthcare(FOOTER_OCR))
        self.assertFalse(
            ocr_suggests_riddhishree_healthcare("OTHER VENDOR TAX INVOICE")
        )

    def test_qty_from_rate_amount(self):
        self.assertEqual(_riddhishree_qty_from_rate_amount(185.20, 130566.00), 705)
        self.assertEqual(_riddhishree_qty_from_rate_amount(90.63, 45315.00), 500)
        self.assertEqual(_riddhishree_qty_from_rate_amount(30.03, 21021.00), 700)
        self.assertIsNone(_riddhishree_qty_from_rate_amount(30.03, 14021.00))

    def test_reconcile_verified_rows(self):
        self.assertTrue(_riddhishree_rows_reconcile(VERIFIED_VISION_ROWS, FOOTER_OCR))
        self.assertEqual(sum(r["quantity"] for r in VERIFIED_VISION_ROWS), 9655)
        self.assertAlmostEqual(
            sum(r["total_amount"] for r in VERIFIED_VISION_ROWS), 1019317.50, places=2
        )

    def test_parse_product_band_truncated_names(self):
        band = """
LODAC 2.5MG TAB 700 IB00942A
LODAC 5MG TAB 800 IB01638A
LODAC AT TAB 800 IB01057A
RVA 10MG TAB IB01296A
TANIX 2.5MG 15TAB IB01388A
TANIX 5MG 15TAB IB01387A
"""
        rows = _parse_riddhishree_product_rows(band)
        names = [r["product_description"] for r in rows]
        self.assertIn("AMLODAC 2.5MG TAB", names)
        self.assertIn("ZYTANIX 2.5MG 15TAB", names)

    def test_parse_clean_rate_amount_band(self):
        band = """
RATE | AMOUNT
30.03 21021.00
41.90 33520.00
138.48 110784.00
41.70 41700.00
84.11 37849.50
227.03 113515.00
28.97 5794.00
49.94 19976.00
57.40 22960.00
123.44 49376.00
101.72 10172.00
77.53 31012.00
111.53 66918.00
166.64 33328.00
109.79 21958.00
82.39 24717.00
90.63 45315.00
64.52 25808.00
185.20 130566.00
288.38 173028.00
"""
        rows = _parse_riddhishree_rate_amount_band(band)
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[0]["quantity"], 700.0)
        self.assertAlmostEqual(rows[18]["quantity"], 705.0)
        self.assertAlmostEqual(rows[16]["quantity"], 500.0)

    def test_fix_rebuilds_from_vision_rows(self):
        bad = [
            {
                "product_description": "AMLODAC 2.5MG TAB",
                "quantity": "30",
                "unit_price": "57.64",
                "total_amount": "1729.20",
                "lot_batch_number": "1e00942A",
            }
        ]
        out = fix_riddhishree_healthcare_line_items_from_ocr(
            bad,
            FOOTER_OCR,
            "",
            vendor="RIDDHISHREE HEALTHCARE",
            vision_rows=VERIFIED_VISION_ROWS,
        )
        self.assertEqual(len(out), 20)
        first = next(i for i in out if "2.5MG" in i["product_description"] and "AMLODAC" in i["product_description"])
        self.assertEqual(first["quantity"], "700")
        self.assertEqual(first["unit_price"], "30.03")
        self.assertEqual(first["total_amount"], "21021.00")
        zyt = next(i for i in out if "ZYTANIX 2.5" in i["product_description"])
        self.assertEqual(zyt["quantity"], "705")
        v500 = next(i for i in out if "VINGLYN M 500" in i["product_description"])
        self.assertEqual(v500["quantity"], "500")

    def test_summary_gstin_and_irn(self):
        summary = {
            "vendor": "RIDDHISHREE HEALTHCARE",
            "vendor_gstin": "",
            "customer_gstin": "",
            "irn": "",
            "tax": "0",
            "total": "0",
        }
        fix_riddhishree_healthcare_summary_from_ocr(summary, FOOTER_OCR)
        self.assertEqual(summary["vendor_gstin"], "27ABEFR6671D1Z2")
        self.assertEqual(summary["customer_gstin"], "27AACPS6252J1ZE")
        self.assertEqual(
            summary["irn"],
            "806b64d30714462a503ecad9dd551c03b28c1ea5c30dcb29e7fd0de513398221",
        )
        self.assertEqual(summary["tax"], "50965.92")
        self.assertEqual(summary["total"], "1070283.42")

    def test_non_riddhishree_unchanged(self):
        items = [{"product_description": "X", "quantity": "30", "unit_price": "1", "total_amount": "30"}]
        out = fix_riddhishree_healthcare_line_items_from_ocr(
            items, "OTHER VENDOR", "", vendor="OTHER"
        )
        self.assertEqual(out[0]["quantity"], "30")

    def test_irn_candidate_parser(self):
        irn = "806b64d30714462a503ecad9dd551c03b28c1ea5c30dcb29e7fd0de513398221"
        self.assertEqual(
            _riddhishree_parse_irn_candidate(f"IRN NO : {irn} Ack No : 1"),
            irn,
        )
        # Do not invent from short/garbled runs
        self.assertEqual(_riddhishree_parse_irn_candidate("IRN NO : 806b64d3"), "")

    def test_reconcile_uses_taxable_label_not_hardcoded_amount(self):
        """Footer TAXABLE label must drive reconcile for any RIDDHISHREE total."""
        footer = (
            "RIDDHISHREE HEALTHCARE Total Prod : 20 Total QTY : 9655 "
            "TAXABLE 1019317.50 TOTAL AMT 1070283.42"
        )
        self.assertTrue(_riddhishree_rows_reconcile(VERIFIED_VISION_ROWS, footer))
        # Wrong taxable must fail — do not force-fit
        bad = footer.replace("1019317.50", "999999.99")
        self.assertFalse(_riddhishree_rows_reconcile(VERIFIED_VISION_ROWS, bad))

    def test_pack_not_used_as_quantity(self):
        """PACK tokens like 15TAB must never become quantity."""
        bad = [{
            "product_description": "ZYTANIX 2.5MG 15TAB",
            "quantity": "15",
            "unit_price": "353.63",
            "total_amount": "5304.45",
            "lot_batch_number": "IB01388A",
        }]
        out = fix_riddhishree_healthcare_line_items_from_ocr(
            bad,
            FOOTER_OCR,
            "",
            vendor="RIDDHISHREE HEALTHCARE",
            vision_rows=VERIFIED_VISION_ROWS,
        )
        zyt = next(i for i in out if "ZYTANIX 2.5" in i["product_description"])
        self.assertEqual(zyt["quantity"], "705")
        self.assertEqual(zyt["unit_price"], "185.20")


if __name__ == "__main__":
    unittest.main()
