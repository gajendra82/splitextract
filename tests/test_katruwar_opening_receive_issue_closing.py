"""Katruwar / Marg PRODUCT DESCRIPTION opening-receive-issue-closing photo."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from services.extraction_quality import evaluate_extraction_quality
from services.sales_statement_extractor import (
    _extract_opening_receive_issue_closing_ocr,
    _is_opening_receive_issue_closing_stock_text,
    _main_stock_parse_ocr_line,
    _main_stock_product_key,
    _main_stock_receive_value_missing,
    _main_stock_sales_strips,
    _apply_stock_identity_validation,
    extract_sales_statement,
)

FIXTURE = Path(__file__).resolve().parents[1] / "test_pdfs" / "katruwar_stock_sales.jpg"

SAMPLE_LINES = {
    "BONNISAN DROP 30ML 77 5096.63 0 0 32 2389.12 45 2978.55 01-May-29": {
        "opening_qty": 77.0,
        "opening_value": 5096.63,
        "receipts_qty": 0.0,
        "receipts_value": 0.0,
        "sales_qty": 32.0,
        "sales_value": 2389.12,
        "closing_qty": 45.0,
        "closing_value": 2978.55,
    },
    "BONNISAN LIQ 200ML 1 60.1 0 0 0 0 1 60.1 01-Sep-28": {
        "opening_qty": 1.0,
        "opening_value": 60.1,
        "receipts_qty": 0.0,
        "receipts_value": 0.0,
        "sales_qty": 0.0,
        "sales_value": 0.0,
        "closing_qty": 1.0,
        "closing_value": 60.1,
    },
    "BONNISPAZ DROP 15ML 40 2269.6 72 4085.28 40 2560 72 4085.28 01-May-29": {
        "opening_qty": 40.0,
        "opening_value": 2269.6,
        "receipts_qty": 72.0,
        "receipts_value": 4085.28,
        "sales_qty": 40.0,
        "sales_value": 2560.0,
        "closing_qty": 72.0,
        "closing_value": 4085.28,
    },
    "BRESOL NS NASAL SOLUTION 10ML 47 2380.55 50 2532.5 50 2857 47 2380.55 01-Apr-28": {
        "opening_qty": 47.0,
        "opening_value": 2380.55,
        "receipts_qty": 50.0,
        "receipts_value": 2532.5,
        "sales_qty": 50.0,
        "sales_value": 2857.0,
        "closing_qty": 47.0,
        "closing_value": 2380.55,
    },
}


class TestKatruwarOpeningReceiveIssueClosing(unittest.TestCase):
    def test_line_parser_maps_eight_columns(self):
        for line, expected in SAMPLE_LINES.items():
            with self.subTest(line=line.split()[0]):
                item = _main_stock_parse_ocr_line(line)
                self.assertIsNotNone(item)
                self.assertEqual(item["opening_qty"], expected["opening_qty"])
                self.assertEqual(item["extra"]["opening_value"], expected["opening_value"])
                self.assertEqual(item["receipts_qty"], expected["receipts_qty"])
                self.assertEqual(item["extra"]["receipts_value"], expected["receipts_value"])
                self.assertEqual(item["sales_qty"], expected["sales_qty"])
                self.assertEqual(item["sales_value"], expected["sales_value"])
                self.assertEqual(item["closing_qty"], expected["closing_qty"])
                self.assertEqual(item["closing_value"], expected["closing_value"])

    def test_all_zero_and_issue_rows_kept(self):
        zero = _main_stock_parse_ocr_line(
            "BABY LOTION 100ML 0 ) re) ) 0 0 0 0 --"
        )
        self.assertIsNotNone(zero)
        self.assertEqual(zero["opening_qty"], 0.0)
        self.assertEqual(zero["sales_qty"], 0.0)
        self.assertEqual(zero["closing_qty"], 0.0)
        self.assertIn("BABY LOTION 100", str(zero["product_name"]).upper())

        issue = _main_stock_parse_ocr_line(
            "BONNISAN DROP 30ML 77 5096.63 0 0 32 2389.12 45 2978.55 01-May-29"
        )
        self.assertIsNotNone(issue)
        self.assertEqual(issue["sales_qty"], 32.0)

        # ISSUE dropped by OCR but recoverable from opening/closing identity.
        from services.sales_statement_extractor import (
            _main_stock_normalize_product_name,
            _main_stock_ocr_number,
            _main_stock_repair_issue_qty,
        )

        broken = {
            "opening_qty": 68.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": 24.0,
        }
        _main_stock_repair_issue_qty(broken)
        self.assertEqual(broken["sales_qty"], 44.0)

        # ISSUE qty blank but sales_value present (OCR "a 1566.4").
        forte = {
            "opening_qty": 76.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "sales_value": 1566.4,
            "closing_qty": 68.0,
        }
        _main_stock_repair_issue_qty(forte)
        self.assertEqual(forte["sales_qty"], 8.0)

        # Digit-drop ISSUE / OPENING (34→4, 52→2).
        tooth = {
            "opening_qty": 49.0,
            "receipts_qty": 0.0,
            "sales_qty": 4.0,
            "closing_qty": 15.0,
        }
        _main_stock_repair_issue_qty(tooth)
        self.assertEqual(tooth["sales_qty"], 34.0)
        geri = {
            "opening_qty": 2.0,
            "receipts_qty": 0.0,
            "sales_qty": 15.0,
            "closing_qty": 37.0,
        }
        _main_stock_repair_issue_qty(geri)
        self.assertEqual(geri["opening_qty"], 52.0)

        self.assertEqual(_main_stock_ocr_number("$2"), 52.0)
        self.assertEqual(_main_stock_ocr_number("$649.08"), 5649.08)
        self.assertEqual(
            _main_stock_normalize_product_name("GEL BOgrr"),
            "V GEL 30GM",
        )
        gel = _main_stock_parse_ocr_line(
            "GEL BOgrr : 3917.6 0 0 22 2430.56 18 1762.92 01-Nov-28"
        )
        self.assertIsNotNone(gel)
        gel["product_name"] = _main_stock_normalize_product_name(gel["product_name"])
        _main_stock_repair_issue_qty(gel)
        self.assertEqual(gel["opening_qty"], 40.0)
        self.assertEqual(gel["sales_qty"], 22.0)
        self.assertEqual(gel["closing_qty"], 18.0)

        vanila = _main_stock_parse_ocr_line(
            "QUISTA KIDZ VANILA 200GM 19 5649.08 ) 0 +) 0 19 $649.08 01-Jan-27"
        )
        self.assertIsNotNone(vanila)
        self.assertEqual(vanila["opening_qty"], 19.0)
        self.assertEqual(vanila["sales_qty"], 0.0)
        self.assertEqual(vanila["closing_qty"], 19.0)
        self.assertIn("VANILA", vanila["product_name"].upper())
        self.assertNotIn("5649", vanila["product_name"])

    def test_packing_not_consumed_as_qty(self):
        item = _main_stock_parse_ocr_line(
            "GERI FORTE SYP 200ml 32 4646.72 0 0 3 450 29 4211.09 01-Oct-27"
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["opening_qty"], 32.0)
        self.assertIn("200", item["product_name"])

    def test_product_key_collapses_ocr_variants(self):
        self.assertEqual(
            _main_stock_product_key("BONNISAN OROP 30ML"),
            _main_stock_product_key("BONNISAN DROP 30ML"),
        )
        self.assertEqual(
            _main_stock_product_key("UV 52 DROP 100ML"),
            _main_stock_product_key("LIV 52 DROP 100ML"),
        )
        self.assertNotEqual(
            _main_stock_product_key("BABY LOTION 100ML"),
            _main_stock_product_key("BABY LOTION 50ML"),
        )

    def test_detector_accepts_title_when_header_garbled(self):
        garbled = (
            "KATRUWAR DISTRIBUTORS\n"
            "HIMALAYA DRUG CO STOCK & SALES STATEMENT 01-08-2026 - 31-08-2026\n"
            "OP{ VALU rec QUANTIT ISSUE CLOSING STOCK\n"
            "BONNISAN DROP 30ML 77 5096.63 0 0 32 2389.12 45 2978.55\n"
        )
        self.assertTrue(_is_opening_receive_issue_closing_stock_text(garbled))
        ssa = (
            "ITEM DESCRIPTION Opening Receipt Issue Dump Closing\n"
            "ARJUNA 1 2 3 4 5\n"
        )
        self.assertFalse(_is_opening_receive_issue_closing_stock_text(ssa))

    def test_early_gemini_misread_triggers_main_stock_reread(self):
        """Generic early Vision zeros ISSUE qty / shifts rows on this layout."""
        bad = {
            "report_title": "STOCK & SALES STATEMENT",
            "line_items": [
                {
                    "product_name": "BONNISAN DROP 30ML",
                    "opening_qty": 155.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 0.0,
                    "closing_qty": 155.0,
                },
                {
                    "product_name": "BONNISAN LIQ 200ML",
                    "opening_qty": 77.0,
                    "receipts_qty": 32.0,
                    "sales_qty": 0.0,
                    "closing_qty": 45.0,
                },
                {
                    "product_name": "BONNISPAZ DROP 15ML",
                    "opening_qty": 65.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 0.0,
                    "closing_qty": 59.0,
                },
                {
                    "product_name": "BRESOL NS",
                    "opening_qty": 40.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 0.0,
                    "closing_qty": 72.0,
                },
                {
                    "product_name": "CYSTONE FORTE",
                    "opening_qty": 130.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 0.0,
                    "closing_qty": 123.0,
                },
                {
                    "product_name": "EVECARE CAP",
                    "opening_qty": 35.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 0.0,
                    "closing_qty": 51.0,
                },
                {
                    "product_name": "HIORA DIABETICS",
                    "opening_qty": 0.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 0.0,
                    "closing_qty": 68.0,
                },
            ],
            "totals": {
                "extra": {
                    "extraction_method": "gemini_extraction_fallback",
                    "stock_identity_fail_count": 21,
                }
            },
        }
        self.assertTrue(_main_stock_receive_value_missing(bad))
        good = {
            "report_title": "STOCK & SALES STATEMENT",
            "line_items": bad["line_items"],
            "totals": {
                "extra": {
                    "extraction_method": "main_stock_sales_statement",
                    "layout": "opening_receive_issue_closing",
                }
            },
        }
        self.assertFalse(_main_stock_receive_value_missing(good))

    @unittest.skipUnless(FIXTURE.exists(), "fixture image missing")
    def test_strips_cover_full_page(self):
        strips = _main_stock_sales_strips(FIXTURE.read_bytes())
        self.assertGreaterEqual(len(strips), 5)

    @unittest.skipUnless(FIXTURE.exists(), "fixture image missing")
    def test_ocr_extract_sample_rows_and_receipt_totals(self):
        result = _extract_opening_receive_issue_closing_ocr(
            FIXTURE.read_bytes(), FIXTURE.name, ".jpg"
        )
        self.assertIsNotNone(result)
        result = _apply_stock_identity_validation(result)
        items = result["line_items"]
        self.assertGreaterEqual(len(items), 30)
        by_name = {str(i.get("product_name") or "").upper(): i for i in items}

        drop = next(i for k, i in by_name.items() if "BONNISAN DROP" in k)
        self.assertEqual(drop["opening_qty"], 77.0)
        self.assertEqual(drop["extra"]["opening_value"], 5096.63)
        self.assertEqual(drop["receipts_qty"], 0.0)
        self.assertEqual(drop["sales_qty"], 32.0)
        self.assertEqual(drop["sales_value"], 2389.12)
        self.assertEqual(drop["closing_qty"], 45.0)
        self.assertEqual(drop["closing_value"], 2978.55)

        liq = next(i for k, i in by_name.items() if "BONNISAN LIQ" in k)
        self.assertEqual(liq["opening_qty"], 1.0)
        self.assertEqual(liq["closing_qty"], 1.0)
        self.assertEqual(liq["sales_qty"], 0.0)

        receipts_qty = sum(float(i.get("receipts_qty") or 0) for i in items)
        receipts_value = sum(
            float((i.get("extra") or {}).get("receipts_value") or 0) for i in items
        )
        self.assertEqual(receipts_qty, 394.0)
        self.assertAlmostEqual(receipts_value, 42410.18, delta=20.0)

        zeros = [
            i
            for i in items
            if all(
                float(i.get(k) or 0) == 0
                for k in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
            )
        ]
        issues = [i for i in items if float(i.get("sales_qty") or 0) > 0]
        self.assertGreaterEqual(len(zeros), 20)
        self.assertGreaterEqual(len(issues), 30)
        self.assertTrue(
            any("BABY LOTION 100" in str(i.get("product_name") or "").upper() for i in zeros)
            or any(
                "BABY LOTION 100" in str(i.get("product_name") or "").upper() for i in items
            )
        )
        names = " | ".join(str(i.get("product_name") or "").upper() for i in items)
        self.assertIn("ARJUNA", names)
        self.assertIn("BABY LOTION 50", names)
        receipts_qty = sum(float(i.get("receipts_qty") or 0) for i in items)
        self.assertEqual(receipts_qty, 394.0)

        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "main_stock_sales_statement")
        self.assertEqual(extra.get("layout"), "opening_receive_issue_closing")

    def test_quality_not_good_when_identity_fails(self):
        result = {
            "line_items": [
                {
                    "product_name": f"PROD {index}",
                    "opening_qty": 10.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 1.0,
                    "closing_qty": 99.0,
                    "sales_value": 1.0,
                    "closing_value": 1.0,
                    "extra": {"layout": "opening_receive_issue_closing"},
                }
                for index in range(10)
            ],
            "totals": {
                "extra": {
                    "extraction_method": "main_stock_sales_statement",
                    "layout": "opening_receive_issue_closing",
                    "stock_identity_fail_count": 10,
                    "stock_identity_kind": "opening_receipts_sales_closing",
                }
            },
        }
        quality = evaluate_extraction_quality(
            result, {"page_count": 1, "source_has_visible_data": True}
        )
        self.assertNotEqual(quality.get("quality"), "good")
        self.assertIn("stock_identity_failure", quality.get("reasons") or [])

    @unittest.skipUnless(FIXTURE.exists(), "fixture image missing")
    def test_extract_routes_to_main_stock_parser(self):
        data = FIXTURE.read_bytes()

        def _fake_photo(file_bytes, filename, ext, force=False):
            return _extract_opening_receive_issue_closing_ocr(
                file_bytes, filename, ext
            )

        with mock.patch(
            "services.sales_statement_extractor._extract_opening_receive_issue_closing_photo",
            side_effect=_fake_photo,
        ):
            with mock.patch(
                "services.sales_statement_extractor._maybe_early_vision_for_image",
                return_value=(None, False),
            ):
                # Avoid Gemini fallback overwriting a good OCR extract in CI.
                with mock.patch(
                    "services.gemini_extraction_fallback.maybe_apply_gemini_fallback",
                    side_effect=lambda result, *args, **kwargs: result,
                ):
                    result = extract_sales_statement(data, FIXTURE.name)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "main_stock_sales_statement")
        items = result.get("line_items") or []
        drop = next(
            i
            for i in items
            if "BONNISAN DROP" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(drop["opening_qty"], 77.0)
        self.assertEqual(drop["closing_qty"], 45.0)


if __name__ == "__main__":
    unittest.main()
