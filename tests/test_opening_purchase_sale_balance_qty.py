"""Qty-only SALES & STOCK OF COMPANY: SALE qty must not become purchase value."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _finalize_opening_purchase_sale_balance_qty,
    _is_opening_purchase_sale_balance_qty_text,
    _opening_purchase_sale_balance_header_text,
    _opsb_product_names_readable,
    empty_line_item,
    empty_result,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000732482_2026_08_ZL_12_262A_01092026051510.jpg"
)

HEADER = (
    "PATEL MEDICAL AGENCY\n"
    "SALES & STOCK OF COMPANY HIMALAYA Normal ON P.Rate FROM 01/08/26 TO 31/08/26\n"
    "PRODUCT NAME UNIT OPENING PURCHASE SALE BALANCE\n"
)

SWIL = (
    "Sales & Stock Statement\n"
    "PRODUCT NAME PACKING Opening Qty Opening Value Receipt Qty Receipt/Pur Value "
    "Issue/Sales Qty Issue/Sales Value Closing Qty Closing Value\n"
)


class TestOpeningPurchaseSaleBalanceDetector(unittest.TestCase):
    def test_matches_qty_columns_and_rejects_swil_values(self):
        self.assertTrue(_is_opening_purchase_sale_balance_qty_text(HEADER))
        self.assertFalse(_is_opening_purchase_sale_balance_qty_text(SWIL))
        self.assertFalse(_is_opening_purchase_sale_balance_qty_text(""))

    def test_sale_qty_is_not_kept_as_purchase_value(self):
        result = empty_result("sample.jpg", "jpg")
        rows = []
        for name, opening, purchase, sale, closing in (
            ("LIV 52 DS SYP", 59, 0, 3, 56),
            ("LIV 52 DS SYP", 70, 0, 12, 58),
            ("KOFLET- LONG. TAB (JAR)", 7, 0, 0, 7),
            ("NOFLET-EX SYP", 0, 0, 0, 0),
            ("LIV 52 HB", 56, 0, 0, 56),
            ("LIV 52 SYP", 0, 0, 0, 0),
            ("LIV-52 DROP", 112, 0, 15, 97),
            ("LIV-52 DS TAB", 20, 100, 48, 72),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = opening
            item["receipts_qty"] = purchase
            item["sales_qty"] = sale
            item["closing_qty"] = closing
            item["sales_value"] = float(sale)
            item["extra"] = {"purchase_value": float(sale), "receipts_value": float(sale)}
            rows.append(item)
        result["line_items"] = rows
        finished = _finalize_opening_purchase_sale_balance_qty(result)
        self.assertIsNotNone(finished)
        liv = finished["line_items"][0]
        self.assertEqual(liv["sales_qty"], 3)
        self.assertEqual(liv["receipts_qty"], 0)
        self.assertEqual(liv["opening_qty"], 59)
        self.assertEqual(liv["closing_qty"], 56)
        self.assertIsNone(liv["sales_value"])
        self.assertNotIn("purchase_value", liv.get("extra") or {})
        self.assertNotIn("receipts_value", liv)
        self.assertTrue(finished["totals"]["extra"]["qty_only"])
        self.assertEqual(
            finished["totals"]["extra"]["extraction_method"],
            "opening_purchase_sale_balance_qty",
        )

    def test_readable_names_reject_symbol_noise(self):
        good = []
        for name in (
            "TENTEX FORTE TAB",
            "LIV 52 DS SYP",
            "KOFLET- LONG. TAB (JAR)",
            "NOFLET-EX SYP",
            "LIV-52 DS TAB",
            "LUKOL TAB",
            "MENTAT SYP",
            "TRIKATU SYP",
        ):
            item = empty_line_item()
            item["product_name"] = name
            good.append(item)
        self.assertTrue(_opsb_product_names_readable(good))
        noise = []
        for name in (
            "et. al ' QoeEo OD OOO",
            "on om. ste",
            "8 Sines of nl PEE",
            "ee & ay Po",
            "at Ye",
            "permet",
            "$$$ oo",
            "QoeEo",
        ):
            item = empty_line_item()
            item["product_name"] = name
            noise.append(item)
        self.assertFalse(_opsb_product_names_readable(noise))

    def test_tentex_forte_keeps_opening_184_and_sales_16(self):
        result = empty_result("sample.jpg", "jpg")
        rows = []
        for name, opening, purchase, sale, closing in (
            ("LIV 52 DS SYP", 59, 0, 3, 56),
            ("KOFLET- LONG. TAB (JAR)", 7, 0, 0, 7),
            ("NOFLET-EX SYP", 0, 0, 0, 0),
            ("LIV-52 DS TAB", 20, 100, 48, 72),
            ("LUKOL TAB", 75, 0, 9, 66),
            ("MENTAT SYP", 1, 10, 0, 11),
            ("TENTEX FORTE TAB", 0, 0, 0, 0),
            ("TENTEX ROYAL CAP", 184, 0, 16, 168),
            ("TRIKATU SYP", 0, 0, 0, 0),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = opening
            item["receipts_qty"] = purchase
            item["sales_qty"] = sale
            item["closing_qty"] = closing
            rows.append(item)
        result["line_items"] = rows
        finished = _finalize_opening_purchase_sale_balance_qty(result)
        by_name = {
            str(item.get("product_name") or ""): item for item in finished["line_items"]
        }
        forte = by_name["TENTEX FORTE TAB"]
        self.assertEqual(forte["opening_qty"], 184)
        self.assertEqual(forte["receipts_qty"], 0)
        self.assertEqual(forte["sales_qty"], 16)
        self.assertEqual(forte["closing_qty"], 168)
        self.assertIsNone(forte["sales_value"])
        self.assertNotIn("purchase_value", forte.get("extra") or {})
        royal = by_name["TENTEX ROYAL CAP"]
        self.assertEqual(royal["opening_qty"], 0)
        self.assertEqual(royal["sales_qty"], 0)


@unittest.skipUnless(FIXTURE.is_file(), "missing Patel photo")
class TestOpeningPurchaseSaleBalancePhoto(unittest.TestCase):
    def test_header_ocr_matches_this_photo_only(self):
        text = _opening_purchase_sale_balance_header_text(FIXTURE.read_bytes())
        self.assertTrue(_is_opening_purchase_sale_balance_qty_text(text))
