"""MANGLAM ASSOCIATES: manufacturer wrap / receiving stamp must not be products."""
import unittest

from app import (
    drop_manglam_associates_extra_items,
    ocr_suggests_manglam_associates,
    recover_missing_items_from_ocr,
)


MANGLAM_OCR = """
MANGLAM ASSOCIATES Party Name:
179/33,GOPI NATH ROAD, TAX INVOICE MAX SUPER SPECIALITY HOSPITAL OPD
BAROOD KHANA,GOLAGANJ,LUCKNOW CREDIT
Invoice No M04666
SN HSN ProductName Pack Qty Batch MFG EXP MRP RATE Dis% Amount
1 30041010) CILNY 20 TAB 1*10*1 10 N2601254 3/29 199.50 111.85 3.00% 1118.50
INTAS INSTITUTION
2 30049099} ENTACOM PLUS 100MG TAB 1*10 50 T0058 6/29 155.70 86.29 3.00% 4314.50
INTAS INSTITUTION
3 30049082} LEVERA XR 500MG TAB 1X10 30 N2601259 3/28 139.65 84.54 3.00% 2536.20
INTAS INSTITUTION
4 21069099] MELOCURE 3MG TAB 1*10 80 INP260001 7/27 58.50 32.50 3.00% 2600.00
INTAS INSTITUTION
5 30049099] VENTAB XL 75 10 TAB 1X10 60 K2601480 5/29 152.50 77.81 3.00% 4668.60
INTAS INSTITUTION
MAX SUPER SPECIALITY HOSPITAL
MATERIAL RECEIVED
SAMPLE CHECKED 20%
SUBJECT TO PHYSICAL VERIFICATION
Total Items :- 5 SUB TOTAL 14780.65
"""


VISION_WITH_EXTRAS = [
    {
        "product_description": "CILNY 20 TAB",
        "quantity": "10",
        "unit_price": "111.85",
        "total_amount": "1118.50",
        "hsn_code": "30041010",
        "lot_batch_number": "N2601254",
    },
    {
        "product_description": "INTAS INSTITUTION",
        "quantity": "10",
        "unit_price": "111.85",
        "total_amount": "1118.50",
        "hsn_code": "30041010",
        "lot_batch_number": "N2601254",
    },
    {
        "product_description": "ENTACOM PLUS 100MG TAB",
        "quantity": "50",
        "unit_price": "86.29",
        "total_amount": "4314.50",
        "hsn_code": "30049099",
        "lot_batch_number": "T0058",
    },
    {
        "product_description": "INTAS INSTITUTION",
        "quantity": "50",
        "unit_price": "86.29",
        "total_amount": "4314.50",
    },
    {
        "product_description": "LEVERA XR 500MG TAB",
        "quantity": "30",
        "unit_price": "84.54",
        "total_amount": "2536.20",
        "hsn_code": "30049082",
        "lot_batch_number": "N2601259",
    },
    {
        "product_description": "MELOCURE 3MG TAB",
        "quantity": "80",
        "unit_price": "32.50",
        "total_amount": "2600.00",
        "hsn_code": "21069099",
        "lot_batch_number": "INP260001",
    },
    {
        "product_description": "VENTAB XL 75 10 TAB",
        "quantity": "60",
        "unit_price": "77.81",
        "total_amount": "4668.60",
        "hsn_code": "30049099",
        "lot_batch_number": "K2601480",
    },
    {
        "product_description": "MAX SUPER SPECIALITY HOSPITAL",
        "quantity": "1",
        "unit_price": "0",
        "total_amount": "0",
    },
    {
        "product_description": "MATERIAL RECEIVED",
        "quantity": "1",
        "unit_price": "0",
        "total_amount": "0",
    },
]


class TestManglamAssociatesLineItems(unittest.TestCase):
    def test_detects_manglam_not_other_formats(self):
        self.assertTrue(ocr_suggests_manglam_associates(MANGLAM_OCR))
        self.assertFalse(
            ocr_suggests_manglam_associates(
                "CHANDUKA AGENCIES\nCOMP PARTICULARS N_MRP"
            )
        )
        self.assertFalse(
            ocr_suggests_manglam_associates(
                "PRAKASH MEDICAL STORES\nCREDIT COPY"
            )
        )

    def test_drops_institution_wrap_and_stamp_keeps_real_rows(self):
        out = drop_manglam_associates_extra_items(
            [dict(item) for item in VISION_WITH_EXTRAS], MANGLAM_OCR)
        names = [it["product_description"] for it in out]
        self.assertEqual(names, [
            "CILNY 20 TAB",
            "ENTACOM PLUS 100MG TAB",
            "LEVERA XR 500MG TAB",
            "MELOCURE 3MG TAB",
            "VENTAB XL 75 10 TAB",
        ])
        self.assertEqual(out[0]["unit_price"], "111.85")
        self.assertEqual(out[0]["total_amount"], "1118.50")
        self.assertEqual(out[4]["lot_batch_number"], "K2601480")

    def test_keeps_legitimate_name_with_institution_suffix(self):
        items = [{
            "product_description": "CILNY 20 TAB INTAS INSTITUTION",
            "quantity": "10",
            "unit_price": "111.85",
            "total_amount": "1118.50",
            "hsn_code": "30041010",
        }]
        out = drop_manglam_associates_extra_items(items, MANGLAM_OCR)
        self.assertEqual(len(out), 1)
        self.assertEqual(
            out[0]["product_description"],
            "CILNY 20 TAB INTAS INSTITUTION",
        )

    def test_recovery_does_not_add_wrap_or_stamp(self):
        real = [dict(item) for item in VISION_WITH_EXTRAS if item[
            "product_description"] in {
            "CILNY 20 TAB",
            "ENTACOM PLUS 100MG TAB",
            "LEVERA XR 500MG TAB",
            "MELOCURE 3MG TAB",
            "VENTAB XL 75 10 TAB",
        }]
        out = recover_missing_items_from_ocr(real, MANGLAM_OCR)
        self.assertEqual(len(out), 5)
        names = [it["product_description"] for it in out]
        self.assertNotIn("INTAS INSTITUTION", names)
        self.assertNotIn("MATERIAL RECEIVED", names)


if __name__ == "__main__":
    unittest.main()
