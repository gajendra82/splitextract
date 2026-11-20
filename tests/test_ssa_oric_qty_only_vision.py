"""Qty-only SSA ORIC Vision escalation when Tesseract drops/shifts rows.

Layout family: STOCK & SALES ANALYSIS OPENING|RECEIPT|ISSUE|CLOSING (qty-only).
No stockist-named route — Vision is the layout-family fallback for weak OCR.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.sales_statement_extractor import (
    _apply_ssa_oric_qty_only_vision_fields,
    _extract_ssa_oric_qty_only_photo,
    _extract_ssa_oric_qty_only_vision,
    _is_ssa_oric_qty_only_text,
    _is_ssa_oric_qty_only_text_fuzzy,
    _parse_ps_pharma_statement,
    _ssa_oric_clear_consecutive_qty_clones,
    _ssa_oric_geometry_header_centres,
    _ssa_oric_ocr_needs_vision,
    _ssa_oric_overlay_ocr_movement,
    _ssa_oric_repair_row_qtys,
    empty_line_item,
    empty_result,
)
from services.stock_image_vision import assess_stock_structured_quality


PAVAN_FUZZY_OCR = """
PAVAN DISTRIBUTORS
A AYURVEDIC SHOPEE CTS NO 3294/1A, GROUND FLOOR
STOCK & SAL ES ANALYSIS (HIMALAYA ZANDRA) 01-09-2026 - 30-09-2026
ITEM DESCRIPTION OPENING RREEIPT IS8UR CLOSING
ARJUNA CAP 60'S 5 0 1 4 0
BRESOL NS 10 ML 34 0 4 30 3
TOTAL 147991 26840 37348 140027 21K
"""

WEAK_ORIC_OCR = """
STOCK & SALES ANALYSIS (HIMALAYA ZANDRA) 01-09-2026 - 30-09-2026
ITEM DESCRIPTION OPENING RECEIPT ISSUE CLOSING
ARJUNA CAP 60'S 3 1 4 0
BRESOL NS 10 ML 29 4 30 3
BRESOL SYP 200 ML 17 0 11 6
CYSTONE SYRUP 200ML 31 0 24 7
ii. 52 DS SYRUP. 100 ML 50 6 5 58
MENTAT DS SYRUP 100 ML 9 0 3 0
REOSTO TAB 30 TAB 22 0 824 14
ST MAN 60 TAB 9 50 32 5
sae a TAB 2. 30 TAB 5 0 3 2
V-GEL 150ML 5 4 9 0
BONNISAN SYP 120 ML 1 6 0 7
BRESOL TAB. 60 TAB 14 0 2 12
CYSTONE TAB 60 TAB 49 100 82 67
EVECARE CAP 30 CAP 61 0 2 59
FLORASANTE CAP 20 CAP 3 0 0 3
TOTAL 1 2 3 4
"""

VISION_PAYLOAD = {
    "stockist_name": "PAVAN DISTRIBUTORS",
    "stockist_address": (
        "A AYURVEDIC SHOPEE CTS NO 3294/1A,GROUND FLOOR,"
        "AZIZ MANSION KOPPIKAR ROAD HUBLI"
    ),
    "company_name": "HIMALAYA ZANDRA",
    "period_from": "2026-09-01",
    "period_to": "2026-09-30",
    "report_title": "STOCK & SALES ANALYSIS",
    "line_items": [
        {
            "product_name": "ARJUNA CAP",
            "packing": "60'S",
            "opening_qty": 5,
            "receipts_qty": 0,
            "sales_qty": 1,
            "closing_qty": 4,
        },
        {
            "product_name": "BRESOL NS",
            "packing": "10 ML",
            "opening_qty": 34,
            "receipts_qty": 0,
            "sales_qty": 4,
            "closing_qty": 30,
        },
        {
            "product_name": "BRESOL SYP",
            "packing": "200 ML",
            "opening_qty": 11,
            "receipts_qty": 0,
            "sales_qty": 0,
            "closing_qty": 11,
        },
        {
            "product_name": "CYSTONE FORTE TAB",
            "packing": "30 TAB",
            "opening_qty": 64,
            "receipts_qty": 0,
            "sales_qty": 12,
            "closing_qty": 52,
        },
        {
            "product_name": "CYSTONE SYRUP",
            "packing": "200ML",
            "opening_qty": 24,
            "receipts_qty": 0,
            "sales_qty": 0,
            "closing_qty": 24,
        },
        {
            "product_name": "LIV-52 DS SYRUP.",
            "packing": "100 ML",
            "opening_qty": 59,
            "receipts_qty": 0,
            "sales_qty": 0,
            "closing_qty": 59,
        },
        {
            "product_name": "REOSTO TAB",
            "packing": "30 TAB",
            "opening_qty": 22,
            "receipts_qty": 0,
            "sales_qty": 8,
            "closing_qty": 14,
        },
        {
            "product_name": "SPEMAN",
            "packing": "60 TAB",
            "opening_qty": 44,
            "receipts_qty": 50,
            "sales_qty": 32,
            "closing_qty": 62,
        },
        {
            "product_name": "STYPLON",
            "packing": "30 TAB",
            "opening_qty": 10,
            "receipts_qty": 0,
            "sales_qty": 0,
            "closing_qty": 10,
        },
        {
            "product_name": "V-GEL",
            "packing": "30 GM",
            "opening_qty": 10,
            "receipts_qty": 0,
            "sales_qty": 1,
            "closing_qty": 9,
        },
        {
            "product_name": "XRENALKA",
            "packing": "100 ML",
            "opening_qty": 0,
            "receipts_qty": 0,
            "sales_qty": 0,
            "closing_qty": 0,
        },
        {
            "product_name": "QUISTA DN",
            "packing": "200G",
            "opening_qty": 6,
            "receipts_qty": 0,
            "sales_qty": 0,
            "closing_qty": 6,
        },
        {
            "product_name": "SEPTILIN SYP",
            "packing": "200ML",
            "opening_qty": 22,
            "receipts_qty": 0,
            "sales_qty": 6,
            "closing_qty": 16,
        },
        {
            "product_name": "MENOSAN",
            "packing": "30 TAB",
            "opening_qty": 68,
            "receipts_qty": 0,
            "sales_qty": 1,
            "closing_qty": 67,
        },
        {
            "product_name": "EVECARE SYP",
            "packing": "400 ML",
            "opening_qty": 0,
            "receipts_qty": 0,
            "sales_qty": 0,
            "closing_qty": 0,
        },
    ],
    "totals": {
        "opening_qty": 147991,
        "receipts_qty": 26840,
        "sales_qty": 37348,
        "closing_qty": 140027,
        "sales_value": 0,
        "closing_value": 0,
        "extra": {},
    },
}


def _gemini_response(payload: dict):
    import json

    return SimpleNamespace(
        json=lambda: {
            "candidates": [
                {"content": {"parts": [{"text": json.dumps(payload)}]}}
            ]
        }
    )


class TestSsaOricQtyOnlyFuzzy(unittest.TestCase):
    def test_fuzzy_matches_mangled_header(self):
        self.assertFalse(_is_ssa_oric_qty_only_text(PAVAN_FUZZY_OCR))
        self.assertTrue(_is_ssa_oric_qty_only_text_fuzzy(PAVAN_FUZZY_OCR))

    def test_fuzzy_rejects_qty_value(self):
        qty_value = (
            "STOCK & SALES ANALYSIS\n"
            "ITEM DESCRIPTION OPENING QTY VALUE RECEIPT QTY VALUE\n"
        )
        self.assertFalse(_is_ssa_oric_qty_only_text_fuzzy(qty_value))

    def test_fuzzy_rejects_money_heavy_without_value_header(self):
        money_heavy = (
            "STOCK & SALES ANALYSIS OPENING RECEIPT ISSUE CLOSING\n"
            "PROD A 10 12.50 0 0.00 1 10.00 9 90.00\n"
            "PROD B 5 55.00 2 20.00 0 0.00 7 70.00\n"
            "PROD C 1 11.00 0 0.00 0 0.00 1 11.00\n"
        )
        self.assertFalse(_is_ssa_oric_qty_only_text_fuzzy(money_heavy))


class TestSsaOricOcrNeedsVision(unittest.TestCase):
    def test_weak_shifted_ocr_needs_vision(self):
        result = _parse_ps_pharma_statement(WEAK_ORIC_OCR, "weak.jpg")
        self.assertIsNotNone(result)
        self.assertTrue(_ssa_oric_ocr_needs_vision(result))

    def test_quality_gate_flags_oric_ocr_weak(self):
        result = _parse_ps_pharma_statement(WEAK_ORIC_OCR, "weak.jpg")
        self.assertIsNotNone(result)
        result["source_format"] = "jpg"
        result["totals"]["extra"]["extraction_method"] = "ssa_oric_qty_only_ocr"
        result["totals"]["extra"]["layout"] = "opening_receipt_issue_closing_qty"
        decision = assess_stock_structured_quality(result)
        self.assertTrue(decision["needs_direct_gemini"])
        self.assertIn("oric_ocr_weak", decision["reasons"])


class TestSsaOricGeometryHeaders(unittest.TestCase):
    def test_interpolates_missing_issue_and_picks_opening_near_receipt(self):
        words = [
            {"text": "OPENING", "cx": 100.0, "cy": 100.0},
            {"text": "OPENING", "cx": 400.0, "cy": 105.0},
            {"text": "RECEIPT", "cx": 550.0, "cy": 108.0},
            {"text": "CLOSING", "cx": 900.0, "cy": 112.0},
        ]
        hdr = _ssa_oric_geometry_header_centres(words)
        self.assertIsNotNone(hdr)
        assert hdr is not None
        self.assertEqual(hdr["OPENING"], 400.0)
        self.assertEqual(hdr["RECEIPT"], 550.0)
        self.assertEqual(hdr["CLOSING"], 900.0)
        self.assertAlmostEqual(hdr["ISSUE"], 725.0)


class TestSsaOricQtyCloneClear(unittest.TestCase):
    def test_clears_x_prefix_ghost_and_evecare_bleed(self):
        def row(name, pack, o, r, s, c):
            it = empty_line_item()
            it.update(
                product_name=name,
                packing=pack,
                opening_qty=float(o),
                receipts_qty=float(r),
                sales_qty=float(s),
                closing_qty=float(c),
            )
            return it

        items = [
            row("EVECARE FORTE SYP", "200 ML", 3, 0, 0, 3),
            row("EVECARE SYP", "400 ML", 3, 0, 0, 3),
            row("HIORA D TOOTH PASTE", "100G", 10, 0, 0, 10),
            row("V-GEL", "30 GM", 10, 0, 1, 9),
            row("XHIORA K MOUTH WASH", "150ML", 10, 0, 0, 10),
        ]
        _ssa_oric_clear_consecutive_qty_clones(items)
        self.assertEqual(
            (
                items[1]["opening_qty"],
                items[1]["sales_qty"],
                items[1]["closing_qty"],
            ),
            (0.0, 0.0, 0.0),
        )
        self.assertEqual(items[3]["sales_qty"], 1.0)
        self.assertEqual(
            (
                items[4]["opening_qty"],
                items[4]["closing_qty"],
            ),
            (0.0, 0.0),
        )


class TestSsaOricOverlayAndRepair(unittest.TestCase):
    def test_overlay_prefers_balancing_ocr_movement(self):
        vis = empty_line_item()
        vis.update(
            product_name="GERIFORTE TAB",
            packing="100 TA",
            opening_qty=6.0,
            receipts_qty=0.0,
            sales_qty=0.0,
            closing_qty=6.0,
        )
        ocr = empty_line_item()
        ocr.update(
            product_name="GERIFORTE TAB",
            packing="100 TA",
            opening_qty=6.0,
            receipts_qty=10.0,
            sales_qty=4.0,
            closing_qty=12.0,
        )
        _ssa_oric_overlay_ocr_movement([vis], [ocr])
        self.assertEqual(vis["receipts_qty"], 10.0)
        self.assertEqual(vis["sales_qty"], 4.0)
        self.assertEqual(vis["closing_qty"], 12.0)

    def test_issue_from_open_close_requires_receipt(self):
        with_receipt = empty_line_item()
        with_receipt.update(
            product_name="KARELA TAB",
            packing="60S",
            opening_qty=1.0,
            receipts_qty=5.0,
            sales_qty=0.0,
            closing_qty=2.0,
        )
        _ssa_oric_repair_row_qtys(with_receipt)
        self.assertEqual(with_receipt["sales_qty"], 4.0)

        no_receipt = empty_line_item()
        no_receipt.update(
            product_name="HIORA GA GEL",
            packing="15ML",
            opening_qty=10.0,
            receipts_qty=0.0,
            sales_qty=0.0,
            closing_qty=1.0,
        )
        _ssa_oric_repair_row_qtys(no_receipt)
        self.assertEqual(no_receipt["sales_qty"], 0.0)


class TestSsaOricQtyOnlyVision(unittest.TestCase):
    def test_vision_fields_preserve_printed_qtys_and_address(self):
        result = empty_result("pavan.jpg", "jpg")
        out = _apply_ssa_oric_qty_only_vision_fields(result, VISION_PAYLOAD)
        by_name = {it["product_name"]: it for it in out["line_items"]}
        self.assertEqual(out["stockist_name"], "PAVAN DISTRIBUTORS")
        self.assertIn("3294/1A", out["stockist_address"])
        self.assertIn("HUBLI", out["stockist_address"].upper())
        self.assertEqual(by_name["ARJUNA CAP"]["opening_qty"], 5.0)
        self.assertEqual(by_name["ARJUNA CAP"]["sales_qty"], 1.0)
        self.assertEqual(by_name["ARJUNA CAP"]["closing_qty"], 4.0)
        self.assertEqual(by_name["BRESOL NS"]["opening_qty"], 34.0)
        self.assertEqual(by_name["BRESOL NS"]["packing"], "10 ML")
        self.assertEqual(by_name["REOSTO TAB"]["sales_qty"], 8.0)
        self.assertEqual(by_name["V-GEL"]["packing"], "30 GM")
        self.assertEqual(by_name["CYSTONE FORTE TAB"]["closing_qty"], 52.0)
        # Qty-only: never invent money.
        self.assertEqual(by_name["ARJUNA CAP"]["sales_value"], 0.0)

    def test_extract_escalates_weak_ocr_to_vision(self):
        with patch(
            "services.sales_statement_extractor._ssa_oric_ocr_variant_texts",
            return_value=[("gray", WEAK_ORIC_OCR)],
        ), patch(
            "services.sales_statement_extractor._ssa_oric_geometry_parse",
            return_value=None,
        ), patch(
            "services.sales_extraction_runtime.sales_generate_content_via_vertex",
            return_value=_gemini_response(VISION_PAYLOAD),
        ):
            result = _extract_ssa_oric_qty_only_photo(
                b"fake-bytes", "pavan.jpg", ".jpg", ocr_hint=WEAK_ORIC_OCR
            )
        self.assertIsNotNone(result)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "ssa_oric_qty_only_vision")
        self.assertEqual(result.get("source_format"), "jpg")
        by_name = {it["product_name"]: it for it in result["line_items"]}
        self.assertEqual(by_name["REOSTO TAB"]["sales_qty"], 8.0)
        self.assertIn("CYSTONE FORTE TAB", by_name)
        self.assertIn("XRENALKA", by_name)

    def test_vision_flag_off_keeps_ocr(self):
        with patch.dict(os.environ, {"STOCK_SSA_ORIC_QTY_ONLY_VISION": "false"}):
            with patch(
                "services.sales_statement_extractor._ssa_oric_ocr_variant_texts",
                return_value=[("gray", WEAK_ORIC_OCR)],
            ), patch(
                "services.sales_statement_extractor._ssa_oric_geometry_parse",
                return_value=None,
            ):
                result = _extract_ssa_oric_qty_only_photo(
                    b"fake", "pavan.jpg", ".jpg", ocr_hint=WEAK_ORIC_OCR
                )
        self.assertIsNotNone(result)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "ssa_oric_qty_only_ocr")
        self.assertEqual(result.get("source_format"), "jpg")


LIVE_JPG = Path(
    r"C:\Users\Prerana Bhalerao\Downloads"
    r"\dea3b7f4-e5f0-44f6-acf7-90f316ccaa6f66892476386547601.jpg"
)


@unittest.skipUnless(LIVE_JPG.is_file(), "Pavan ORIC audit JPG not present")
class TestSsaOricQtyOnlyVisionLive(unittest.TestCase):
    def test_live_photo_recovers_missing_rows(self):
        # Optional live check — uses Gemini budget (max 2). Skip in CI without key.
        if os.getenv("STOCK_LIVE_ORIC_VISION", "").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            self.skipTest("STOCK_LIVE_ORIC_VISION not enabled")
        result = _extract_ssa_oric_qty_only_photo(
            LIVE_JPG.read_bytes(), LIVE_JPG.name, ".jpg"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.get("source_format"), "jpg")
        names = {it["product_name"].upper() for it in result["line_items"]}
        self.assertGreaterEqual(len(result["line_items"]), 35)
        for must in (
            "CYSTONE FORTE TAB",
            "SPEMAN",
            "STYPLON",
            "V-GEL",
            "XRENALKA",
            "REOSTO TAB",
        ):
            self.assertTrue(
                any(must in n for n in names),
                f"missing {must} in {sorted(names)[:20]}...",
            )


if __name__ == "__main__":
    unittest.main()
