"""Pharmacea Link: Nucoxia 90 Tab qty/rate when OCR drops decimals (5.0→50, 231.4→2314)."""
import unittest

from app import (
    _restore_pharmacea_dropped_qty_rate_decimals,
    enforce_schema,
    _extract_line_items_for_validation,
)


INVOICE_2848_OCR = """
Pharmacea Link
Document No. : 2848/26-27
SINo | Item Description HSN Code | Quantity |Unit |Unit Price(Rs) |Discount(Rs) | Taxable Tax Rate Total
Amount(Rs)
Gerbisa L Syp 120ml [300490 1.0 NOS |174.83 6.99 167.84 5.00 + 0.00 | 176.24
2 Nasoclear Gel 15Gms 130049099 |13.0 NOS /96.1 49.97 1199.33 5.00 + 0.00 | 1259.29
3 |Atorva 80 30049079 |10.0  |NOS|261.26 104.5 2508.1 5.00 + 0.00 | 2633.5
4 |Nucoxia 90 Tab 3004e069 [50  |Nos|2314 46.28 1110.72 |5.00+0.00{ 1166.26
5 |Ibset Tab 30049099 |10.0 |NOs|589.26 235.7 5656.9  |5.00+0.00| 5939.74
Taxble Amt 10642.89 Tot Inv. Amt 11175.00
"""


def _item(desc, qty, rate, total, hsn, disc, gross):
    return {
        "product_description": desc,
        "quantity": qty,
        "unit_price": rate,
        "total_amount": total,
        "hsn_code": hsn,
        "additional_fields": {
            "discount_percentage": disc,
            "gross_amount": gross,
        },
    }


def _payload(items, ocr=INVOICE_2848_OCR):
    return {
        "data": {
            "invoice_summary": {
                "vendor": "Pharmaceä Link",
                "customer": "Deenanath Medical Stores",
                "invoice_no": "2848/26-27",
                "total": "11175.00",
            },
            "line_items": {"items": items, "count": len(items)},
            "ocr_text": ocr,
        }
    }


class TestPharmaceaNucoxiaQtyRate(unittest.TestCase):
    def test_restores_50_and_2314_to_5_and_231_4(self):
        qty, rate = _restore_pharmacea_dropped_qty_rate_decimals(
            50, 2314, 1110.72, 46.28)
        self.assertAlmostEqual(qty, 5.0, places=2)
        self.assertAlmostEqual(rate, 231.4, places=2)

    def test_leaves_fitting_pair_unchanged(self):
        qty, rate = _restore_pharmacea_dropped_qty_rate_decimals(
            10.0, 261.26, 2508.1, 104.5)
        self.assertAlmostEqual(qty, 10.0, places=2)
        self.assertAlmostEqual(rate, 261.26, places=2)

    def test_fix18_corrects_only_nucoxia_qty_and_rate(self):
        items = [
            _item("Gerbisa L Syp 120ml", "1.0", "174.83", "176.24",
                  "300490", 6.99, 167.84),
            _item("Nasoclear Gel 15Gms", "13.0", "96.10", "1259.29",
                  "30049099", 49.97, 1199.33),
            _item("Atorva 80", "10.0", "261.26", "2633.5",
                  "30049079", 104.5, 2508.1),
            _item("Nucoxia 90 Tab", "1", "1157.00", "1166.26",
                  "30049069", 46.28, 1110.72),
            _item("Ibset Tab", "10.0", "589.26", "5939.74",
                  "30049099", 235.7, 5656.9),
        ]
        snapshot = [
            (i["product_description"], i["quantity"], i["unit_price"],
             i["total_amount"])
            for i in items
        ]
        out = _extract_line_items_for_validation(
            enforce_schema(_payload([dict(i) for i in items]))
        )
        by_name = {i["product_description"]: i for i in out}
        nuc = by_name["Nucoxia 90 Tab"]
        self.assertAlmostEqual(float(nuc["quantity"]), 5.0, places=2)
        self.assertAlmostEqual(float(nuc["unit_price"]), 231.4, places=2)
        self.assertEqual(str(nuc["total_amount"]), "1166.26")
        for desc, qty, rate, total in snapshot:
            if desc == "Nucoxia 90 Tab":
                continue
            other = by_name[desc]
            self.assertEqual(other["quantity"], qty)
            self.assertEqual(other["unit_price"], rate)
            self.assertEqual(str(other["total_amount"]), str(total))


if __name__ == "__main__":
    unittest.main()
