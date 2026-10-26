"""MAA PHARMACEUTICALS: restore QTY when Free column (0) is used as quantity."""
import unittest

from app import (
    fix_maa_pharmaceuticals_base_amount_from_qty_rate,
    fix_maa_pharmaceuticals_zero_qty_from_ocr,
    ocr_suggests_maa_pharmaceuticals,
)


MAA_TABLE_OCR = """
MAA PHARMACEUTICALS
GST INVOICE CREDIT
Sr HSN| Product Name Pack Batch exp MRP Rate QTY Free Amount
1. | 30049082] ATORVA 10 TAB 15S 1A01432B 8/28 79.63 46.65 30 0 1399.50
2. | 30042091] DERIPHYLIN INJ NB00213A 3/30 11.22 6.67 14 0 93.38
3. | 30049099] DUONEM ER 300 TAB 10T FB00035A 12/27 1740.80 1066.20 30 0 31986.00
4. | 30049023] FORMONIDE FORTE RESPICAP 30CAP 1B01413A 5/28 545.53 255.00 10 0 2550.00
5. | 30049011] FORMONIDE 400 RESPICAP 30C 1B01072A 4/28 224.14 126.00 8 0 1008.00
6. | 30049011] FORMONIDE 200 RESPICAP 30C 1B01146A 4/28 184.50 96.00 7 0 672.00
7. | 30049011] OXEMIA 100MG TAB 6T MB02165A 2/28 1493.00 1025.54 10 0 10255.40
8. | 30049099] ZYCOLCHIN TABLET 10S 1B01319A 4/28 35.04 23.80 70 0 1666.00
9. | 30049029] ZYTANIX 2.5 TAB 15 1B00863A 3/28 353.63 219.00 10 0 2190.00
"""

BETA_OCR = """
BETA AGENCIES & PROJECTS PVT. LTD.
INV.NO: 433
"""


def _item(desc, qty, rate, total, batch, hsn):
    return {
        "product_description": desc,
        "quantity": qty,
        "unit_price": rate,
        "total_amount": total,
        "lot_batch_number": batch,
        "hsn_code": hsn,
        "additional_fields": {"free_quantity": "0"},
    }


class TestMaaPharmaceuticalsQty(unittest.TestCase):
    def test_detector_vendor_only(self):
        self.assertTrue(ocr_suggests_maa_pharmaceuticals(
            "", "MAA PHARMACEUTICALS"))
        self.assertTrue(ocr_suggests_maa_pharmaceuticals(MAA_TABLE_OCR))
        self.assertFalse(ocr_suggests_maa_pharmaceuticals(BETA_OCR))

    def test_restores_only_zero_qty_formonide_400(self):
        items = [
            _item("ATTORVA 10 TAB", "30", "46.65", "1399.50", "IA01432B", "30049082"),
            _item("DERIPHYLIN", "14", "6.67", "93.38", "NB00213A", "30042091"),
            _item("DUONEM ER 300 TAB", "30", "1066.20", "31986.00", "FB00035A", "30049099"),
            _item("FORMONIDE FORTE RESPICAP", "10", "255.00", "2550.00", "IB01413A", "30049023"),
            _item("FORMONIDE 400 RESPICAP", "0", "126.00", "0.00", "IB01072A", "30049011"),
            _item("FORMONIDE 200 RESPICAP", "7", "96.00", "672.00", "IB01146A", "30049011"),
            _item("OXEMIA 100MG TAB", "10", "1025.54", "10255.40", "MB02165A", "30049011"),
            _item("ZYCOLCHIN TABLET", "70", "23.80", "1666.00", "IB01319A", "30049099"),
            _item("ZYTANIX 2.5 TAB", "10", "219.00", "2190.00", "IB00863A", "30049029"),
        ]
        snapshot = [(i["quantity"], i["unit_price"], i["total_amount"],
                     i["product_description"]) for i in items]
        out = fix_maa_pharmaceuticals_zero_qty_from_ocr(
            items, MAA_TABLE_OCR, vendor="MAA PHARMACEUTICALS")
        self.assertEqual(out[4]["quantity"], "8")
        self.assertEqual(out[4]["unit_price"], "126.00")
        self.assertEqual(out[4]["total_amount"], "1008.00")
        self.assertEqual(out[4]["product_description"], "FORMONIDE 400 RESPICAP")
        for idx, (qty, rate, total, desc) in enumerate(snapshot):
            if idx == 4:
                continue
            self.assertEqual(out[idx]["quantity"], qty)
            self.assertEqual(out[idx]["unit_price"], rate)
            self.assertEqual(out[idx]["total_amount"], total)
            self.assertEqual(out[idx]["product_description"], desc)

    def test_non_maa_invoice_unchanged(self):
        items = [
            _item("FORMONIDE 400 RESPICAP", "0", "126.00", "0.00", "IB01072A", "30049011"),
        ]
        out = fix_maa_pharmaceuticals_zero_qty_from_ocr(
            items, BETA_OCR, vendor="BETA AGENCIES")
        self.assertEqual(out[0]["quantity"], "0")

    def test_base_amount_is_qty_times_rate_not_amount_after_discount(self):
        items = [
            _item("THROMBOPHOB GEL", "440", "200.00", "82720.00", "IB01448A", "30049099"),
            _item("THROMBOPHOB OINTMENT", "10", "113.00", "1062.20", "IB01574A", "30049011"),
        ]
        out = fix_maa_pharmaceuticals_base_amount_from_qty_rate(
            items, MAA_TABLE_OCR, vendor="MAA PHARMACEUTICALS")
        self.assertEqual(out[0]["quantity"], "440")
        self.assertEqual(out[0]["unit_price"], "200.00")
        self.assertEqual(out[0]["total_amount"], "88000.00")
        self.assertEqual(out[0]["product_description"], "THROMBOPHOB GEL")
        self.assertEqual(out[1]["quantity"], "10")
        self.assertEqual(out[1]["unit_price"], "113.00")
        self.assertEqual(out[1]["total_amount"], "1130.00")

    def test_aligns_gross_amount_so_rate_is_not_rewritten(self):
        items = [
            _item("THROMBOPHOB GEL", "440", "200.00", "82720.00", "IB01448A", "30049099"),
        ]
        items[0]["additional_fields"]["gross_amount"] = "82720.00"
        out = fix_maa_pharmaceuticals_base_amount_from_qty_rate(
            items, MAA_TABLE_OCR, vendor="MAA PHARMACEUTICALS")
        self.assertEqual(out[0]["unit_price"], "200.00")
        self.assertEqual(out[0]["total_amount"], "88000.00")
        self.assertEqual(out[0]["additional_fields"]["gross_amount"], "88000.00")

    def test_base_amount_unchanged_when_already_qty_times_rate(self):
        items = [
            _item("ATORVA 10 TAB", "30", "46.65", "1399.50", "IA01432B", "30049082"),
        ]
        out = fix_maa_pharmaceuticals_base_amount_from_qty_rate(
            items, MAA_TABLE_OCR, vendor="MAA PHARMACEUTICALS")
        self.assertEqual(out[0]["total_amount"], "1399.50")
        self.assertEqual(out[0]["quantity"], "30")
        self.assertEqual(out[0]["unit_price"], "46.65")

    def test_base_amount_skips_non_maa(self):
        items = [
            _item("THROMBOPHOB GEL", "440", "200.00", "82720.00", "IB01448A", "30049099"),
        ]
        out = fix_maa_pharmaceuticals_base_amount_from_qty_rate(
            items, BETA_OCR, vendor="BETA AGENCIES")
        self.assertEqual(out[0]["total_amount"], "82720.00")


if __name__ == "__main__":
    unittest.main()
