"""BETA AGENCIES SalePrint: schedule+qty pipe cell must not become an extra product."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _extract_line_items_for_validation,
    drop_beta_agencies_saleprint_extra_items,
    enforce_schema,
    ocr_suggests_beta_agencies_saleprint,
    recover_missing_items_from_ocr,
)

BETA_SALEPRINT_OCR = """
BETA AGENCIES & PROJECTS PVT.LTD.,
TAX INVOICE CREDIT INV.NO: 5916
RCK MFR QTY DESCRIPTION PACK DIS MRP BATCH EXP RATE VALUE SCM Total GST Hsn
ZYDUS 2800 TRAMAZAC HOSP 50MG * 1ML S* 0.00 12.65 NB00209A 3/28 9.64 26992.00 0.00 26992.00 5 30049069
ZYD 200 NASOCLEAR DROPS 20ML S 0.00 72.98 *TB0680A 4/29 45.00 9000.00 0.00 9000.00 5 30049069
ZYDUS 10 ZYTANIX 5 15'S S* 0.00 605.69 *IB00975A 4/28 409.79 4097.90 0.00 4097.90 5 30049079
ITEM 3 QTY 3010
 |  | ZYDU | S 2800 | TRAMAZAC HOSP 50MG * | 1ML S* | 0.00 | 12.65 |  | NB00209A | 3 /28 | 9.64 | 26992.00 | 0.00 |  | 26992.00 | 5 | 30049069
Please start using MEDIBILL RETAIL APP
"""

YEN_OCR = """
YEN-PHARMA (A Unit of Yenepoya Pharmaceuticals & Surgicals)
CADIL 10 OXALGIN NANO GEL 30GM 1'S S 4.00 208.20IB00656A# 8 /27 142.76 1427.60 0.00 1427.60 5 300490
ITEM 3
"""

BETA_HEADER_ONLY = """
BETA AGENCIES & PROJECTS PVT. LTD.
INV.NO: 433
"""

BETA_ITEMS = [
    {
        "product_description": "TRAMAZAC HOSP 50MG * 1ML S*",
        "lot_batch_number": "NB00209A",
        "quantity": "2800",
        "unit_price": "9.64",
        "total_amount": "26992.00",
    },
    {
        "product_description": "NASOCLEAR DROPS",
        "lot_batch_number": "TB0680A",
        "quantity": "200",
        "unit_price": "45.00",
        "total_amount": "9000.00",
    },
    {
        "product_description": "ZYTANIX 5",
        "lot_batch_number": "IB00975A",
        "quantity": "10",
        "unit_price": "409.79",
        "total_amount": "4097.90",
    },
]


class TestBetaAgenciesSaleprintExtraItems(unittest.TestCase):
    def test_detects_beta_saleprint_not_yen_or_header_only(self):
        self.assertTrue(ocr_suggests_beta_agencies_saleprint(BETA_SALEPRINT_OCR))
        self.assertTrue(ocr_suggests_beta_agencies_saleprint(
            BETA_SALEPRINT_OCR, "BETA AGENCIES & PROJECTS PVT.LTD."))
        self.assertFalse(ocr_suggests_beta_agencies_saleprint(YEN_OCR))
        self.assertFalse(ocr_suggests_beta_agencies_saleprint(BETA_HEADER_ONLY))
        self.assertFalse(ocr_suggests_beta_agencies_saleprint("JACKSON MEDICALS"))

    def test_recovery_does_not_add_schedule_qty_phantom(self):
        out = recover_missing_items_from_ocr(
            [dict(x) for x in BETA_ITEMS], BETA_SALEPRINT_OCR)
        names = [i["product_description"].upper() for i in out]
        self.assertEqual(len(out), 3)
        self.assertFalse(any(n.startswith("S ") or n.startswith("S*") for n in names))
        self.assertEqual(names.count("NASOCLEAR DROPS"), 1)

    def test_drops_s_2800_phantom(self):
        items = [dict(x) for x in BETA_ITEMS] + [{
            "product_description": "S 2800",
            "quantity": "5",
            "unit_price": "12.65",
            "total_amount": "26992.00",
            "lot_batch_number": "NB00209A",
            "recovered_from_ocr": True,
        }]
        out = drop_beta_agencies_saleprint_extra_items(
            items, BETA_SALEPRINT_OCR, "BETA AGENCIES & PROJECTS PVT.LTD.")
        names = [i["product_description"] for i in out]
        self.assertEqual(len(out), 3)
        self.assertNotIn("S 2800", names)
        self.assertIn("NASOCLEAR DROPS", names)

    def test_does_not_drop_other_formats(self):
        items = [{
            "product_description": "S 2800",
            "quantity": "5",
            "unit_price": "12.65",
            "total_amount": "26992.00",
        }]
        out = drop_beta_agencies_saleprint_extra_items(items, YEN_OCR)
        self.assertEqual(out[0]["product_description"], "S 2800")

    def test_enforce_schema_keeps_three_products_drops_phantom(self):
        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "BETA AGENCIES & PROJECTS PVT.LTD.",
                    "customer": "FR.MULLERS HOSPITAL",
                    "invoice_no": "5916",
                    "total": "42094.00",
                },
                "line_items": {
                    "items": [dict(x) for x in BETA_ITEMS] + [{
                        "product_description": "S 2800",
                        "quantity": "5",
                        "unit_price": "5398.40",
                        "total_amount": "26992.00",
                        "lot_batch_number": "NB00209A",
                        "recovered_from_ocr": True,
                    }],
                    "count": 4,
                },
                "ocr_text": BETA_SALEPRINT_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        names = [i["product_description"].upper() for i in out]
        self.assertEqual(len(out), 3)
        self.assertFalse(any(n == "S 2800" or n.startswith("S 2") for n in names))
        self.assertTrue(any("TRAMAZAC" in n for n in names))
        self.assertTrue(any("NASOCLEAR" in n for n in names))
        self.assertTrue(any("ZYTANIX" in n for n in names))


if __name__ == "__main__":
    unittest.main()
