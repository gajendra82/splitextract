"""Palepu Pharma: keep coherent Vision qty/rate; skip Tot Qty footer override."""
import unittest

from app import (
    fix_marg_erp_qty_rate_from_ocr,
    fix_single_item_qty_rate_from_ocr,
    fix_mrp_as_unit_price,
    _apply_palepu_multipage_vision_line_items,
    _extract_line_items_for_validation,
)


PALEPU_OCR = """
PALEPU PHARMA DISTRIBUTORS PVT LTD
TAX INVOICE CUM BILL OF SUPPLY
Tax Inv. No. :CBPI-26-69910
Code KMC001 Page:2 of 3
Mfac PRODUCT PACK Pack QTY BATCH EXP New MRP Trade Price Product Value GST HSN
ZYDUS ATORVA 10MG TAB 15S 15 300 IA01432B 08/28 79.63 51.56 15468.00 5 30049079
ZYDUS LOSACAR H TAB 10S 10 20 SB00011A 12/27 235.69 145.82 2916.40 5 30049073
ZYDUS TRAMAZAC 50MG 1ML INJ 1 350 NB00086A 01/28 12.57 8.31 2908.50 5 30049069
ZYDUS DERIPHYLLIN TAB 30S 30 30 IB00770A 03/30 25.63 17.37 521.10 5 30049094
"""

# Garbled OCR that misreads Product Value as 530 for ATORVA/LOSACAR
PALEPU_GARBLED_OCR = """
PALEPU PHARMA DISTRIBUTORS PVT LTD
Tax Inv. No. :CBPI-26-69910
ZYDUS ATORVA 10MG TAB 15S 15 300 IA01432B 08/28 79.63 51.56 530.00 5 30049079
ZYDUS LOSACAR H TAB 10S 10 20 SB00011A 12/27 235.69 145.82 530.00 5 30049073
ZYDUS TRAMAZAC 50MG 1ML INJ 1 350 NB00086A 01/28 12.57 8.31 3908.50 5 30049069
"""

PALEPU_PAGE3_OCR = """
PALEPU PHARMA DISTRIBUTORS PVT LTD
Tax Inv. No. : CBPI-26-69910
Page:3 of 3
ZYDUS ENTEHEP 0.5MG TAB 30S 1 10 SB00195A 02/28 2723.27 508.80 5088.00 5 300490
Total Items: 37
Total Qty : 2576
Sale Value : 305707.80
Grand Total : 320993.00
"""


class TestPalepuQtyRateGuard(unittest.TestCase):
    def test_keeps_coherent_vision_when_ocr_amount_wrong(self):
        items = [
            {
                "product_description": "ATORVA 10MG TAB 15S",
                "quantity": "300",
                "unit_price": "51.56",
                "total_amount": "15468.00",
                "lot_batch_number": "IA01432B",
            },
            {
                "product_description": "LOSACAR H TAB 10S",
                "quantity": "20",
                "unit_price": "145.82",
                "total_amount": "2916.40",
                "lot_batch_number": "SB00011A",
            },
            {
                "product_description": "TRAMAZAC 50MG 1ML INJ",
                "quantity": "350",
                "unit_price": "8.31",
                "total_amount": "2908.50",
                "lot_batch_number": "NB00086A",
            },
        ]
        out = fix_marg_erp_qty_rate_from_ocr(items, PALEPU_GARBLED_OCR)
        by_name = {it["product_description"]: it for it in out}
        self.assertEqual(by_name["ATORVA 10MG TAB 15S"]["unit_price"], "51.56")
        self.assertEqual(by_name["ATORVA 10MG TAB 15S"]["total_amount"], "15468.00")
        self.assertEqual(by_name["LOSACAR H TAB 10S"]["unit_price"], "145.82")
        self.assertEqual(by_name["LOSACAR H TAB 10S"]["total_amount"], "2916.40")
        self.assertEqual(by_name["TRAMAZAC 50MG 1ML INJ"]["unit_price"], "8.31")
        self.assertEqual(by_name["TRAMAZAC 50MG 1ML INJ"]["total_amount"], "2908.50")

    def test_still_fixes_incoherent_vision_amount_as_rate(self):
        # Classic Palepu bug: Product Value parked in unit_price, qty=1 (pack)
        items = [
            {
                "product_description": "ATORVA 10MG TAB 15S",
                "quantity": "1",
                "unit_price": "15468.00",
                "total_amount": "15468.00",
                "lot_batch_number": "IA01432B",
            }
        ]
        out = fix_marg_erp_qty_rate_from_ocr(items, PALEPU_OCR)
        self.assertEqual(out[0]["quantity"], "300")
        self.assertAlmostEqual(float(out[0]["unit_price"]), 51.56, places=2)
        self.assertAlmostEqual(float(out[0]["total_amount"]), 15468.00, places=2)

    def test_skips_invoice_footer_tot_qty_on_single_item_page(self):
        items = [
            {
                "product_description": "ENTEHEP 0.5MG TAB",
                "quantity": "10",
                "unit_price": "508.80",
                "total_amount": "5088.00",
                "lot_batch_number": "SB00195A",
            }
        ]
        out = fix_single_item_qty_rate_from_ocr(items, PALEPU_PAGE3_OCR)
        self.assertEqual(out[0]["quantity"], "10")
        self.assertEqual(out[0]["unit_price"], "508.80")

    def test_skips_mrp_fix_when_qty_is_gst_pct_and_gross_absurd(self):
        # Combined-OCR path: GST%→qty, digit-smashed Product Value→gross
        item = {
            "product_description": "FLUTICONE FT NASAL SPRAY",
            "quantity": "5",
            "unit_price": "355.07",
            "total_amount": "53260.50",
            "additional_fields": {"gross_amount": "5326050"},
        }
        out = fix_mrp_as_unit_price(
            item, vendor="PALEPU PHARMA", ocr_text=PALEPU_OCR
        )
        self.assertEqual(out["unit_price"], "355.07")
        self.assertEqual(out["quantity"], "5")

    def test_multipage_keeps_vision_trade_price_rows(self):
        group = {
            "pages": [0, 1],
            "ocr_text": PALEPU_OCR,
            "extracted_data": {
                "data": {"line_items": {"items": [], "count": 0}}
            },
        }
        page_results = [
            {
                "ocr_text": PALEPU_OCR + "\npage1",
                "full_data": {
                    "data": {
                        "line_items": {
                            "items": [
                                {
                                    "product_description": (
                                        "FLUTICONE FT NASAL SPRAY"
                                    ),
                                    "quantity": "150",
                                    "unit_price": "355.07",
                                    "total_amount": "53260.50",
                                    "lot_batch_number": "FC001",
                                }
                            ],
                            "count": 1,
                        }
                    }
                },
            },
            {
                "ocr_text": PALEPU_OCR + "\npage2",
                "full_data": {
                    "data": {
                        "line_items": {
                            "items": [
                                {
                                    "product_description": (
                                        "TRAMAZAC 50MG CAP 10S"
                                    ),
                                    "quantity": "60",
                                    "unit_price": "37.54",
                                    "total_amount": "2252.40",
                                    "lot_batch_number": "TR001",
                                }
                            ],
                            "count": 1,
                        }
                    }
                },
            },
        ]
        self.assertTrue(
            _apply_palepu_multipage_vision_line_items(group, page_results)
        )
        items = _extract_line_items_for_validation(group["extracted_data"])
        by_name = {it["product_description"]: it for it in items}
        self.assertEqual(by_name["FLUTICONE FT NASAL SPRAY"]["quantity"], "150")
        self.assertEqual(
            by_name["FLUTICONE FT NASAL SPRAY"]["unit_price"], "355.07"
        )
        self.assertEqual(by_name["TRAMAZAC 50MG CAP 10S"]["quantity"], "60")
        self.assertEqual(
            by_name["TRAMAZAC 50MG CAP 10S"]["unit_price"], "37.54"
        )


if __name__ == "__main__":
    unittest.main()
