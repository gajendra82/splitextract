"""NEW.J.B.SALES PMS: Tesseract 5MG→SMG must not become an extra product."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _collect_sparse_missing_candidates,
    _extract_line_items_for_validation,
    _parse_new_jb_sales_pms_line_items,
    drop_new_jb_sales_pms_extra_items,
    enforce_schema,
    fix_new_jb_sales_pms_line_items_from_ocr,
    ocr_suggests_new_jb_sales_pms,
    recover_missing_items_from_ocr,
)

NEW_JB_OCR = """
NEW.J.B.SALES K.S.C GANDHI PATH,CHAS-827013 B.S.CITY
GSTIN:-20AAKFN8434B1ZN
MS:- MUSKAN DRUGS
INV-Date : 02/09/2026 GST INVOICE INV- No. : JBL-2181 TYPE OF INV- CREDIT
S.no. Particulars HSN MFG Batch EXP MRP Rate QTY Free ACT SHE Dis. Amount Tax% Net.Amt
RAT
1 AMLODAC 2.5 TAB 30049072 ZYDUS IB00942A 03/28 57.64 43.94 200.00 0.00 0.00 24.00 8788.00 GST5% 7012.82
2 AMLODAC -AT TAB 30049072 ZYDUS IB01057A 04/28 264.41 201.46 200.00 0.00 0.00 24.00 40292.00 GST5% 32153.02
3 AMLODAC CH TABLET 30041030 ZYDUS AFAH2603 05/28 183.59 139.88 60.00 0.00 0.00 24.00 8392.80 GST5% 6697.45
4 AMLODAC SMG TAB 30049072 ZYDUS IB01361A 05/28 80.52 61.35 1200.00 0.00 0.00 24.00 73620.00 GST5% 58748.76
Amount in words: Rs. Sixteen Thousand Seven Hundred Sixty Three Only. SGST2.5% 99630.53 2490.76 TOTAL 131092.80
CGST2.5% 99630.53 2490.76 Discount 31462.27
Tax Amt 4981.52
Gross Amount 104612.00
Net Pay Amt 104612.00
E.&.O.E. OUR SOFTWARE PMS 9386871337 For NEW.J.B.SALES
"""

YASHIKA_OCR = """
YASHIKA AGENCIES YASHIKA DISTRIBUTORS
TAX INVOICE-CREDIT Bill No : 31364
RACK MFR HSN PRODUCT NAME PACK BATCH EXDT QTY FREE PD% MRP SRATE GST% VALUE
"""

NEW_JB_ITEMS = [
    {
        "product_description": "AMLODAC 2.5 TAB",
        "lot_batch_number": "IB00942A",
        "quantity": "200.00",
        "unit_price": "43.94",
        "total_amount": "8788.00",
    },
    {
        "product_description": "AMLODAC-AT TAB",
        "lot_batch_number": "IB01057A",
        "quantity": "200.00",
        "unit_price": "201.46",
        "total_amount": "40292.00",
    },
    {
        "product_description": "AMLODAC CH TABLET",
        "lot_batch_number": "AFAH2603",
        "quantity": "60.00",
        "unit_price": "139.88",
        "total_amount": "8392.80",
    },
    {
        "product_description": "AMLODAC 5MG TAB",
        "lot_batch_number": "IB01361A",
        "quantity": "1200.00",
        "unit_price": "61.35",
        "total_amount": "73620.00",
    },
]


class TestNewJBSalesPmsExtraItems(unittest.TestCase):
    def test_detects_new_jb_sales_not_other_formats(self):
        self.assertTrue(ocr_suggests_new_jb_sales_pms(NEW_JB_OCR))
        self.assertTrue(
            ocr_suggests_new_jb_sales_pms(NEW_JB_OCR, "NEW.J.B.SALES"))
        self.assertFalse(ocr_suggests_new_jb_sales_pms(YASHIKA_OCR))
        self.assertFalse(ocr_suggests_new_jb_sales_pms("JACKSON MEDICALS"))

    def test_recovery_does_not_add_smg_duplicate(self):
        out = recover_missing_items_from_ocr(
            [dict(x) for x in NEW_JB_ITEMS], NEW_JB_OCR)
        names = [i["product_description"].upper() for i in out]
        self.assertEqual(len(out), 4)
        self.assertFalse(any("SMG" in n for n in names))
        self.assertEqual(names.count("AMLODAC 5MG TAB"), 1)
        self.assertEqual(str(out[0]["quantity"]), "200.00")
        self.assertEqual(str(out[0]["unit_price"]), "43.94")
        self.assertEqual(str(out[3]["quantity"]), "1200.00")
        self.assertEqual(str(out[3]["unit_price"]), "61.35")

    def test_parses_numbered_particulars_qty_rate(self):
        rows = _parse_new_jb_sales_pms_line_items(NEW_JB_OCR)
        self.assertEqual(len(rows), 4)
        mapped = {
            re.sub(r"\s+", " ", r["product_description"].upper().replace(" -", "-")): r
            for r in rows
        }
        self.assertEqual(mapped["AMLODAC 2.5 TAB"]["quantity"], 200.0)
        self.assertEqual(mapped["AMLODAC 2.5 TAB"]["unit_price"], 43.94)
        self.assertEqual(mapped["AMLODAC-AT TAB"]["quantity"], 200.0)
        self.assertEqual(mapped["AMLODAC-AT TAB"]["unit_price"], 201.46)
        self.assertEqual(mapped["AMLODAC CH TABLET"]["quantity"], 60.0)
        self.assertEqual(mapped["AMLODAC CH TABLET"]["unit_price"], 139.88)
        self.assertEqual(mapped["AMLODAC 5MG TAB"]["quantity"], 1200.0)
        self.assertEqual(mapped["AMLODAC 5MG TAB"]["unit_price"], 61.35)

    def test_sparse_candidates_treat_smg_as_missing_without_format_skip(self):
        cands = _collect_sparse_missing_candidates(NEW_JB_ITEMS, NEW_JB_OCR)
        names = [c["product_description"].upper() for c in cands]
        self.assertTrue(any("SMG" in n for n in names))

    def test_rebuild_drops_unrelated_extra_when_serials_contiguous(self):
        items = [dict(x) for x in NEW_JB_ITEMS] + [{
            "product_description": "AMLODAC PLUS TAB",
            "quantity": "24",
            "unit_price": "24.00",
            "total_amount": "576.00",
        }]
        out = fix_new_jb_sales_pms_line_items_from_ocr(
            items, NEW_JB_OCR, "NEW.J.B.SALES")
        names = [i["product_description"].upper() for i in out]
        self.assertEqual(len(out), 4)
        self.assertFalse(any("PLUS" in n for n in names))
        by_name = {i["product_description"].upper(): i for i in out}
        self.assertEqual(float(by_name["AMLODAC-AT TAB"]["quantity"]), 200.0)
        self.assertEqual(float(by_name["AMLODAC-AT TAB"]["unit_price"]), 201.46)

    def test_rebuild_keeps_non_particulars_form_products(self):
        items = [dict(x) for x in NEW_JB_ITEMS] + [{
            "product_description": "COMBIMIST L RESPULES",
            "quantity": "10",
            "unit_price": "25.00",
            "total_amount": "250.00",
        }]
        out = fix_new_jb_sales_pms_line_items_from_ocr(
            items, NEW_JB_OCR, "NEW.J.B.SALES")
        names = [i["product_description"].upper() for i in out]
        self.assertIn("COMBIMIST L RESPULES", names)
        self.assertEqual(len(out), 5)

    def test_focused_vision_smg_duplicate_is_dropped_without_recovery_flag(self):
        items = [dict(x) for x in NEW_JB_ITEMS] + [{
            "product_description": "AMLODAC SMG TAB",
            "quantity": "1200",
            "unit_price": "61.35",
            "total_amount": "73620.00",
            "lot_batch_number": "IB01361A",
        }]
        out = fix_new_jb_sales_pms_line_items_from_ocr(
            items, NEW_JB_OCR, "NEW.J.B.SALES")
        names = [i["product_description"].upper() for i in out]
        self.assertEqual(len(out), 4)
        self.assertFalse(any("SMG" in n for n in names))
        by_name = {i["product_description"].upper(): i for i in out}
        self.assertEqual(float(by_name["AMLODAC 5MG TAB"]["quantity"]), 1200.0)
        self.assertEqual(float(by_name["AMLODAC 5MG TAB"]["unit_price"]), 61.35)

    def test_drops_smg_typo_and_footer_rows(self):
        items = [dict(x) for x in NEW_JB_ITEMS] + [{
            "product_description": "AMLODAC SMG TAB",
            "quantity": "1200",
            "unit_price": "61.35",
            "total_amount": "73620.00",
            "lot_batch_number": "IB01361A",
            "recovered_from_ocr": True,
        }, {
            "product_description": "Gross Amount",
            "quantity": "1",
            "unit_price": "104612.00",
            "total_amount": "104612.00",
            "recovered_from_ocr": True,
        }]
        out = drop_new_jb_sales_pms_extra_items(
            items, NEW_JB_OCR, "NEW.J.B.SALES")
        names = [i["product_description"].upper() for i in out]
        self.assertEqual(len(out), 4)
        self.assertFalse(any("SMG" in n for n in names))
        self.assertFalse(any("GROSS AMOUNT" in n for n in names))
        self.assertIn("AMLODAC 5MG TAB", names)

    def test_does_not_drop_other_formats(self):
        items = [{
            "product_description": "AMLODAC SMG TAB",
            "quantity": "1200",
            "unit_price": "61.35",
            "total_amount": "73620.00",
        }]
        out = drop_new_jb_sales_pms_extra_items(items, YASHIKA_OCR)
        self.assertEqual(out[0]["product_description"], "AMLODAC SMG TAB")

    def test_enforce_schema_keeps_four_products_drops_extra(self):
        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "NEW.J.B.SALES",
                    "customer": "MUSKAN DRUGS",
                    "invoice_no": "JBL-2181",
                    "total": "104612.00",
                },
                "line_items": {
                    "items": [dict(x) for x in NEW_JB_ITEMS] + [{
                        "product_description": "AMLODAC SMG TAB",
                        "quantity": "1200",
                        "unit_price": "61.35",
                        "total_amount": "73620.00",
                        "lot_batch_number": "IB01361A",
                        "recovered_from_ocr": True,
                    }],
                    "count": 5,
                },
                "ocr_text": NEW_JB_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        names = [i["product_description"].upper() for i in out]
        self.assertEqual(len(out), 4)
        self.assertFalse(any("SMG" in n for n in names))
        by_name = {i["product_description"].upper(): i for i in out}
        self.assertEqual(float(by_name["AMLODAC 2.5 TAB"]["quantity"]), 200.0)
        self.assertEqual(float(by_name["AMLODAC 2.5 TAB"]["unit_price"]), 43.94)
        self.assertEqual(float(by_name["AMLODAC-AT TAB"]["quantity"]), 200.0)
        self.assertEqual(float(by_name["AMLODAC-AT TAB"]["unit_price"]), 201.46)
        self.assertEqual(float(by_name["AMLODAC CH TABLET"]["quantity"]), 60.0)
        self.assertEqual(float(by_name["AMLODAC CH TABLET"]["unit_price"]), 139.88)
        self.assertEqual(float(by_name["AMLODAC 5MG TAB"]["quantity"]), 1200.0)
        self.assertEqual(float(by_name["AMLODAC 5MG TAB"]["unit_price"]), 61.35)


if __name__ == "__main__":
    unittest.main()
