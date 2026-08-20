"""GST e-invoice Details of Goods / Services (UNT + qty.000): Unit Price, not taxable/qty."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _ocr_suggests_gst_einvoice_goods_services_unt_table,
    _ocr_suggests_gst_portal_goods_details,
    _ocr_suggests_nic_irp_einvoice_table,
    _parse_gst_einvoice_goods_services_unt_rows,
    enforce_schema,
    fix_gst_einvoice_goods_services_unt_qty_rate_from_ocr,
    _extract_line_items_for_validation,
)

SMA7535_OCR = """
1.e-Invoice Details
Document No. : SMA7535
4.Details of Goods / Services
SINo|Item Description HSN Quantity} Unit | Unit Discount(Rs)| Taxable Tax Rate(GST + Cess | Other Total
Code Price(Rs) Amount(Rs)|State Cess + Cess charges
1 DERIPHYLLIN RET 30049094 |30.000 |UNT}21.600 32.40 615.60 5.00 + 0.00 | 0 646.38
150MG 0.00 + 0
2 THROMBOPHOB OINT —_|30049099 |200.000 | UNT| 129.000 1290.00 24510.00 {5.00 + 0.00 | 0 25735.50
0.00 + 0
3 CINTAPRO TAB 30049099 |40.000 |UNT/105.210 210.42 3997.98 5.00 + 0.00 | 0 4197.88
0.00 +0
4 CINTAPRO TAB 30049099 |20.000 )UNT/105.210 105.21 1998.99 5.00 + 0.00 | 0 2098.94
0.00 + 0
5 MEVA C 15T 30049099 |10.000 )UNT/241.720 120.86 2296.34 5.00 + 0.00 | 0 2411.16
0.00 + 0
6 MEVA C 15T 30049099 |1.000 /UNT/241.720 12.09 229.63 5.00 + 0.00 | 0 241.11
0.00 + 0
7 ATEN 25 30049074 |27.000 |UNT|24.080 32.51 617.65 5.00 + 0.00 | 0 648.53
0.00 + 0
8 ATEN 50 30049074 |30.000 |UNT/29.500 44.25 840.75 5.00 + 0.00 | 0 882.79
0.00 +0
9 BILOVAS TAB 300390 {10.000 |UNT/222.120 111.06 2110.14 5.00 + 0.00 | 0 2215.65
0.00 + 0
10 |FORGLYN PLUS INH 30049099 |10.000 |UNT|712.050 356.03 6764.48 5.00 + 0.00 | e) 7102.70
0.00 +0
11. |NUPATCH 200 30049099 |5.000 /UNT|148.610 37.15 705.90 5.00 + 0.00 | e) 741.20
0.00 + 0
12 |NUPATCH 200 30049099 |4.000 /UNT|148.610 29.72 564.72 5.00 + 0.00 | e) 592.96
0.00 + 0
13 |LIPAGLYN 30049099 |1.000 /UNT|381.810 19.09 362.72 5.00 + 0.00 | e) 380.86
0.00 + 0
Tax'ble Amt |CGSTAmt |SGSTAmt Tot Inv. Amt
45614.90 1140.38 1140.38 47896.00
"""

NIC_IRP_OCR = """
Sr Item HSN Quantity Unit Unit Price Discount Taxable Tax Rate Other Total
4 DOLONEX- 300490 50 UNT 204.79 307.19 10239.5 5+0|0+0 0 10428.93
Digitally Signed by NIC-IRP
1.e-Invoice Details
4A.Details of Goods / Services
"""

JACKSON_OCR = """
JACKSON MEDICALS Inv.No. D4151 ST.THOMAS HOSPITAL
AMLOPIN 5MG TAB 10 20.32 203.20
"""


class TestGstEinvoiceGoodsServicesUnt(unittest.TestCase):
    def test_detects_sma_table_not_nic_or_portal(self):
        self.assertTrue(_ocr_suggests_gst_einvoice_goods_services_unt_table(SMA7535_OCR))
        self.assertFalse(_ocr_suggests_nic_irp_einvoice_table(SMA7535_OCR))
        self.assertFalse(_ocr_suggests_gst_portal_goods_details(SMA7535_OCR))
        self.assertFalse(_ocr_suggests_gst_einvoice_goods_services_unt_table(NIC_IRP_OCR))
        self.assertFalse(_ocr_suggests_gst_einvoice_goods_services_unt_table(JACKSON_OCR))

    def test_parses_wrapped_name_qty_unit_price(self):
        rows = _parse_gst_einvoice_goods_services_unt_rows(SMA7535_OCR)
        self.assertEqual(len(rows), 13)
        deri = next(r for r in rows if "DERIPHYLLIN" in r["product_description"].upper())
        throm = next(r for r in rows if "THROMBOPHOB" in r["product_description"].upper())
        self.assertIn("150MG", deri["product_description"].upper())
        self.assertEqual(deri["quantity"], 30.0)
        self.assertAlmostEqual(deri["unit_price"], 21.6, places=2)
        self.assertEqual(throm["quantity"], 200.0)
        self.assertAlmostEqual(throm["unit_price"], 129.0, places=2)

    def test_restores_net_rate_and_swapped_qty_without_touching_totals_or_names(self):
        items = [
            {
                "product_description": "DERIPHYLLIN RET 150MG",
                "hsn_code": "30049094",
                "quantity": "30.000",
                "unit_price": "20.52",
                "total_amount": "615.60",
            },
            {
                "product_description": "THROMBOPHOB OINT",
                "hsn_code": "30049099",
                "quantity": "129",
                "unit_price": "190.00",
                "total_amount": "24510.00",
            },
            {
                "product_description": "CINTAPRO TAB",
                "hsn_code": "30049099",
                "quantity": "40.000",
                "unit_price": "99.95",
                "total_amount": "3997.98",
            },
        ]
        fixed = fix_gst_einvoice_goods_services_unt_qty_rate_from_ocr(items, SMA7535_OCR)
        deri = next(i for i in fixed if "DERIPHYLLIN" in i["product_description"].upper())
        throm = next(i for i in fixed if "THROMBOPHOB" in i["product_description"].upper())
        cin = next(i for i in fixed if i["quantity"] in ("40.000", "40") or float(i["quantity"]) == 40)
        self.assertEqual(deri["product_description"], "DERIPHYLLIN RET 150MG")
        self.assertEqual(deri["quantity"], "30.000")
        self.assertEqual(deri["unit_price"], "21.60")
        self.assertEqual(deri["total_amount"], "615.60")
        self.assertEqual(throm["product_description"], "THROMBOPHOB OINT")
        self.assertEqual(str(throm["quantity"]), "200")
        self.assertEqual(throm["unit_price"], "129.00")
        self.assertEqual(throm["total_amount"], "24510.00")
        self.assertEqual(cin["unit_price"], "105.21")
        self.assertEqual(cin["total_amount"], "3997.98")

    def test_enforce_schema_undoes_mrp_net_rate(self):
        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "SURYA MEDICAL AGENCIES",
                    "customer": "Narayana pharmacy unit III",
                    "invoice_no": "SMA7535",
                    "total": "47896.00",
                },
                "line_items": {
                    "items": [
                        {
                            "product_description": "THROMBOPHOB OINT",
                            "hsn_code": "30049099",
                            "quantity": "200.000",
                            "unit_price": "129.000",
                            "total_amount": "24510.00",
                        },
                        {
                            "product_description": "DERIPHYLLIN RET 150MG",
                            "hsn_code": "30049094",
                            "quantity": "30.000",
                            "unit_price": "21.600",
                            "total_amount": "615.60",
                        },
                    ],
                    "count": 2,
                },
                "ocr_text": SMA7535_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        throm = next(i for i in out if "THROMBOPHOB" in i["product_description"].upper())
        deri = next(i for i in out if "DERIPHYLLIN" in i["product_description"].upper())
        self.assertEqual(float(throm["quantity"]), 200.0)
        self.assertAlmostEqual(float(throm["unit_price"]), 129.0, places=2)
        self.assertEqual(throm["total_amount"], "24510.00")
        self.assertEqual(float(deri["quantity"]), 30.0)
        self.assertAlmostEqual(float(deri["unit_price"]), 21.6, places=2)
        self.assertEqual(deri["total_amount"], "615.60")

    def test_single_line_restores_unit_price_only(self):
        """SMA7513: one row, Discounted Unit Price must not become taxable/qty."""
        ocr = """
1.e-Invoice Details
Document No. : SMA7513
4.Details of Goods / Services
SINo |Item Description |HSN Code | Quantity |Unit |Unit Price(Rs) |Discount(Rs) | Taxable
1 NASOCLEAR DR |30049069 |150.000 |UNT /43.440 325.80 6190.20 5.00 + 0.00 | 0
0.00 +0
Tax'bleAmt Tot Inv. Amt
6190.20 6500.00
"""
        items = [{
            "product_description": "NASOCLEAR DR",
            "hsn_code": "30049069",
            "quantity": "150.000",
            "unit_price": "41.27",
            "total_amount": "6190.20",
        }]
        self.assertTrue(_ocr_suggests_gst_einvoice_goods_services_unt_table(ocr))
        rows = _parse_gst_einvoice_goods_services_unt_rows(ocr)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["unit_price"], 43.44, places=2)
        fixed = fix_gst_einvoice_goods_services_unt_qty_rate_from_ocr(
            [dict(items[0])], ocr)
        self.assertEqual(fixed[0]["product_description"], "NASOCLEAR DR")
        self.assertEqual(fixed[0]["quantity"], "150.000")
        self.assertEqual(fixed[0]["unit_price"], "43.44")
        self.assertEqual(fixed[0]["total_amount"], "6190.20")

        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "SURYA MEDICAL AGENCIES",
                    "customer": "Narayana pharmacy unit III",
                    "invoice_no": "SMA7513",
                    "total": "6500.00",
                },
                "line_items": {"items": [dict(items[0])], "count": 1},
                "ocr_text": ocr,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["product_description"], "NASOCLEAR DR")
        self.assertEqual(float(out[0]["quantity"]), 150.0)
        self.assertAlmostEqual(float(out[0]["unit_price"]), 43.44, places=2)
        self.assertEqual(out[0]["total_amount"], "6190.20")

    def test_does_not_touch_non_matching_invoices(self):
        items = [{
            "product_description": "AMLOPIN 5MG TAB",
            "quantity": "10",
            "unit_price": "20.32",
            "total_amount": "203.20",
        }]
        fixed = fix_gst_einvoice_goods_services_unt_qty_rate_from_ocr(items, JACKSON_OCR)
        self.assertEqual(fixed[0]["quantity"], "10")
        self.assertEqual(fixed[0]["unit_price"], "20.32")


if __name__ == "__main__":
    unittest.main()
