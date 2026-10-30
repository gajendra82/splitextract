"""Group Wise multi-page multi-division safeguards (flag-gated)."""

from __future__ import annotations

import os
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from services.sales_statement_extractor import (
    _apply_swil_receipt_value_fields,
    _build_division_summaries,
    _dedupe_swil_receipt_overlap_items,
    _detect_statement_division,
    _drop_trailing_statement_total_item,
    _group_wise_division_pages_enabled,
    _is_non_product_line_name,
    _is_statement_division_banner,
    _parse_image_only_swil_receipt_pur_pdf,
    _resolve_page_statement_division,
    _stamp_swil_page_division_items,
    _swil_receipt_items_overlap,
    empty_result,
)
from services.stock_multipage import _page_stockist_group_key


def _item(
    name: str,
    *,
    packing: str = "60'S",
    receipts_qty: float = 10.0,
    sales_qty: float = 2.0,
    closing_qty: float = 8.0,
    closing_value: float = 100.0,
    receipts_value: float = 90.0,
    opening_qty: float = 0.0,
    division_code: Optional[str] = None,
    division_name: Optional[str] = None,
    page_number: Optional[int] = None,
) -> Dict[str, Any]:
    extra: Dict[str, Any] = {"receipts_value": receipts_value}
    if division_code:
        extra["division_code"] = division_code
    if division_name:
        extra["division_name"] = division_name
    if page_number is not None:
        extra["page_number"] = page_number
    row: Dict[str, Any] = {
        "product_name": name,
        "packing": packing,
        "opening_qty": opening_qty,
        "receipts_qty": receipts_qty,
        "sales_qty": sales_qty,
        "closing_qty": closing_qty,
        "closing_value": closing_value,
        "receipts_value": receipts_value,
        "extra": extra,
    }
    if page_number is not None:
        row["page_number"] = page_number
    return row


def _page_ok(
    items: List[Dict[str, Any]],
    *,
    company_name: Optional[str] = None,
    stockist_name: str = "MODERN PHARMA",
) -> Dict[str, Any]:
    result = empty_result("page.png", "png")
    result["stockist_name"] = stockist_name
    result["company_name"] = company_name
    result["period_from"] = "2026-08-01"
    result["period_to"] = "2026-08-31"
    result["report_title"] = "Sales & Stock Statement"
    result["line_items"] = items
    result["totals"]["closing_value"] = sum(
        float(i.get("closing_value") or 0) for i in items
    )
    result["totals"]["receipts_value"] = sum(
        float(i.get("receipts_value") or 0) for i in items
    )
    result["totals"]["extra"]["receipts_value"] = result["totals"]["receipts_value"]
    return result


class TestDivisionDetector(unittest.TestCase):
    def test_detects_him_codes_and_aliases(self):
        cases = [
            ("Himalaya OTX (HIM8)", "HIM8"),
            ("HIMALAYA PURE HANDS DIV (HIM7)", "HIM7"),
            ("Himalaya Zandra (HIM3)", "HIM3"),
            ("HIMALAYA ZANDRA", "HIM3"),
            ("HIMALAYA ZEAL", "HIM1"),
            ("HIMALAYA-ZENITH", "HIM2"),
            ("Himalaya Zenith (HIM2)", "HIM2"),
        ]
        for text, code in cases:
            with self.subTest(text=text):
                detected = _detect_statement_division(text)
                self.assertIsNotNone(detected)
                assert detected is not None
                self.assertEqual(detected["division_code"], code)

    def test_ignores_plain_company_and_product_skus(self):
        self.assertIsNone(_detect_statement_division("HIMALAYA WELLNESS COMPANY"))
        self.assertIsNone(_detect_statement_division("LIV.52 SYRUP 200ML"))
        self.assertIsNone(_detect_statement_division("ZEAL DROPS 30ML"))
        self.assertIsNone(_detect_statement_division("MODERN PHARMA"))

    def test_banner_is_non_product(self):
        self.assertTrue(_is_statement_division_banner("HIMALAYA ZANDRA (HIM3)"))
        self.assertTrue(_is_non_product_line_name("HIMALAYA PURE HANDS DIV"))
        self.assertTrue(_is_non_product_line_name("Total (Value In Rs.)"))
        self.assertTrue(
            _is_non_product_line_name("MARG ERP NANO for Chemist Rs.5550")
        )
        self.assertFalse(_is_non_product_line_name("CONFIDO TABLETS"))


class TestDivisionAwareDedupe(unittest.TestCase):
    def test_cross_division_same_name_kept(self):
        items = [
            _item("CONFIDO TABLETS", division_code="HIM8", receipts_qty=10),
            _item("CONFIDO TABLETS", division_code="HIM3", receipts_qty=10),
        ]
        self.assertFalse(_swil_receipt_items_overlap(items[0], items[1]))
        self.assertEqual(len(_dedupe_swil_receipt_overlap_items(items)), 2)

    def test_within_division_ocr_duplicate_removed(self):
        items = [
            _item("CONFIDO TABLETS", division_code="HIM1", closing_value=111.0),
            _item("CONFIDO TABLETS", division_code="HIM1", closing_value=111.0),
        ]
        self.assertTrue(_swil_receipt_items_overlap(items[0], items[1]))
        self.assertEqual(len(_dedupe_swil_receipt_overlap_items(items)), 1)

    def test_no_division_metadata_preserves_legacy_dedupe(self):
        items = [
            _item("HIORA GA GEL", packing="15ML", closing_value=3652.42),
            _item("HIORA GA GEL", packing=None, closing_value=3652.42, receipts_qty=0),
        ]
        # Same as legacy: matching closing_value collapses OCR near-dup.
        self.assertEqual(len(_dedupe_swil_receipt_overlap_items(items)), 1)


class TestTotalRowPreservation(unittest.TestCase):
    def test_total_name_dropped_product_kept(self):
        items = [
            _item(f"ALPHA SKU {i}", receipts_qty=10.0 + i, opening_qty=0.0)
            for i in range(6)
        ]
        last_sku = "LAST BEFORE TOTAL SKU"
        items.append(
            _item(last_sku, receipts_qty=5.0, opening_qty=0.0, closing_qty=5.0)
        )
        items.append(
            {
                "product_name": "Total (Value In Rs.)",
                "packing": None,
                "opening_qty": 0.0,
                "receipts_qty": 500.0,
                "sales_qty": 0.0,
                "closing_qty": 500.0,
                "closing_value": 0.0,
                "extra": {},
            }
        )
        cleaned = _drop_trailing_statement_total_item(items)
        names = [i["product_name"] for i in cleaned]
        self.assertIn(last_sku, names)
        self.assertFalse(any(n.upper().startswith("TOTAL") for n in names))

    def test_aggregate_heuristic_skipped_when_flag_on(self):
        items = [
            _item(f"ALPHA SKU {i}", receipts_qty=50.0, opening_qty=0.0)
            for i in range(6)
        ]
        # Large last SKU that would trip the legacy aggregate heuristic.
        items.append(
            _item(
                "REAL LAST PRODUCT",
                receipts_qty=400.0,
                opening_qty=0.0,
                closing_qty=400.0,
            )
        )
        with mock.patch.dict(os.environ, {"STOCK_GROUP_WISE_DIVISION_PAGES": "true"}):
            self.assertTrue(_group_wise_division_pages_enabled())
            cleaned = _drop_trailing_statement_total_item(items)
        names = [i["product_name"] for i in cleaned]
        self.assertIn("REAL LAST PRODUCT", names)

    def test_aggregate_heuristic_still_runs_when_flag_off(self):
        items = [
            _item(f"ALPHA SKU {i}", receipts_qty=50.0, opening_qty=0.0)
            for i in range(6)
        ]
        items.append(
            _item(
                "LOOKS LIKE TOTAL",
                receipts_qty=400.0,
                opening_qty=0.0,
                closing_qty=400.0,
            )
        )
        with mock.patch.dict(os.environ, {"STOCK_GROUP_WISE_DIVISION_PAGES": "false"}):
            self.assertFalse(_group_wise_division_pages_enabled())
            cleaned = _drop_trailing_statement_total_item(items)
        names = [i["product_name"] for i in cleaned]
        self.assertNotIn("LOOKS LIKE TOTAL", names)


class TestStampAndSummaries(unittest.TestCase):
    def test_stamp_and_summaries(self):
        raw = [
            _item("A"),
            {"product_name": "HIMALAYA ZANDRA (HIM3)", "extra": {}},
            _item("B"),
        ]
        stamped = _stamp_swil_page_division_items(
            raw,
            page_number=3,
            division={"division_code": "HIM3", "division_name": "Himalaya Zandra"},
        )
        self.assertEqual(len(stamped), 2)
        for row in stamped:
            self.assertEqual(row["extra"]["division_code"], "HIM3")
            self.assertEqual(row["extra"]["page_number"], 3)
        summary = _build_division_summaries(stamped)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["product_count"], 2)
        self.assertEqual(summary[0]["page_numbers"], [3])

    def test_apply_captures_banner_before_filter(self):
        result = empty_result("x.png", "png")
        parsed = {
            "company_name": "MODERN PHARMA COMPANY",
            "line_items": [
                {
                    "product_name": "Himalaya OTX (HIM8)",
                    "packing": None,
                    "opening_qty": 0,
                    "receipts_qty": 0,
                    "sales_qty": 0,
                    "closing_qty": 0,
                },
                {
                    "product_name": "ASHVAGANDHA CAP",
                    "packing": "60'S",
                    "opening_qty": 1,
                    "receipts_qty": 2,
                    "sales_qty": 1,
                    "closing_qty": 2,
                    "closing_value": 10,
                    "receipts_value": 5,
                    "sales_value": 3,
                },
                {
                    "product_name": "BRAHMI CAP",
                    "packing": "60'S",
                    "opening_qty": 1,
                    "receipts_qty": 2,
                    "sales_qty": 1,
                    "closing_qty": 2,
                    "closing_value": 10,
                    "receipts_value": 5,
                    "sales_value": 3,
                },
                {
                    "product_name": "CONFIDO TAB",
                    "packing": "60'S",
                    "opening_qty": 1,
                    "receipts_qty": 2,
                    "sales_qty": 1,
                    "closing_qty": 2,
                    "closing_value": 10,
                    "receipts_value": 5,
                    "sales_value": 3,
                },
            ],
            "totals": {},
        }
        applied = _apply_swil_receipt_value_fields(result, parsed)
        detected = ((applied.get("totals") or {}).get("extra") or {}).get(
            "detected_division"
        )
        self.assertEqual(detected["division_code"], "HIM8")
        names = [i["product_name"] for i in applied["line_items"]]
        self.assertNotIn("Himalaya OTX (HIM8)", names)
        resolved = _resolve_page_statement_division(applied)
        self.assertEqual(resolved["division_code"], "HIM8")


class TestMultipageStockistGuard(unittest.TestCase):
    def test_division_banner_not_stockist_key(self):
        page = {
            "stockist_name": "HIMALAYA ZANDRA (HIM3)",
            "company_name": "HIMALAYA ZANDRA",
            "period_from": "2026-08-01",
            "period_to": "2026-08-31",
        }
        self.assertEqual(_page_stockist_group_key(page), "")
        ok = {
            "stockist_name": "MODERN PHARMA",
            "period_from": "2026-08-01",
            "period_to": "2026-08-31",
        }
        self.assertIn("MODERN", _page_stockist_group_key(ok))


class TestParseImageOnlyDivisionPages(unittest.TestCase):
    def _fake_doc(self, page_count: int = 7):
        doc = mock.MagicMock()
        doc.page_count = page_count

        def _page_get(index):
            page = mock.MagicMock()
            page.get_text.return_value = ""
            page.get_pixmap.return_value.tobytes.return_value = b"PNG"
            return page

        doc.__getitem__.side_effect = _page_get
        return doc

    def _run_parse(self, pages, page_count, flag: str):
        fake_fitz = mock.MagicMock()
        with mock.patch.dict(os.environ, {"STOCK_GROUP_WISE_DIVISION_PAGES": flag}):
            with mock.patch.dict("sys.modules", {"fitz": fake_fitz}):
                with mock.patch(
                    "services.sales_statement_extractor._extract_swil_receipt_value_vision"
                ) as vision:
                    seq = iter(list(pages) + [None] * 16)
                    vision.side_effect = lambda *a, **k: next(seq)
                    return _parse_image_only_swil_receipt_pur_pdf(
                        self._fake_doc(page_count), "HIMALAYA (1).pdf"
                    )

    def test_stamps_divisions_inherits_and_reports_failed_page(self):
        pages = [
            _page_ok(
                [_item("OTX A"), _item("OTX B"), _item("OTX C")],
                company_name="Himalaya OTX (HIM8)",
            ),
            _page_ok(
                [_item("PH A"), _item("PH B"), _item("PH C")],
                company_name="Himalaya Pure Hands Div (HIM7)",
            ),
            _page_ok(
                [_item("ZANDRA A"), _item("CONFIDO TABLETS"), _item("ZANDRA C")],
                company_name="Himalaya Zandra (HIM3)",
            ),
            # Continuation: no heading — must inherit HIM3
            _page_ok(
                [_item("ZANDRA D"), _item("ZANDRA E"), _item("ZANDRA F")],
                company_name=None,
            ),
            _page_ok(
                [_item("ZEAL A"), _item("CONFIDO TABLETS"), _item("ZEAL C")],
                company_name="HIMALAYA ZEAL",
            ),
            None,  # failed page 6
            _page_ok(
                [_item("ZENITH A"), _item("ZENITH B"), _item("ZENITH C")],
                company_name="Himalaya Zenith (HIM2)",
            ),
        ]
        result = self._run_parse(pages, 7, "true")

        self.assertIsNotNone(result)
        assert result is not None
        extra = result["totals"]["extra"]
        self.assertTrue(extra.get("pages_incomplete"))
        self.assertEqual(extra.get("pages_total"), 7)
        self.assertIn(6, extra.get("pages_failed") or [])
        self.assertEqual(extra.get("document_type"), "group_wise_sales")

        codes = {
            (i.get("extra") or {}).get("division_code")
            for i in result["line_items"]
        }
        self.assertIn("HIM8", codes)
        self.assertIn("HIM7", codes)
        self.assertIn("HIM3", codes)
        self.assertIn("HIM1", codes)
        self.assertIn("HIM2", codes)

        # Continuation page 4 stamped HIM3.
        p4 = [
            i
            for i in result["line_items"]
            if (i.get("extra") or {}).get("page_number") == 4
        ]
        self.assertTrue(p4)
        self.assertTrue(all((i.get("extra") or {}).get("division_code") == "HIM3" for i in p4))

        # Cross-division CONFIDO kept twice.
        confido = [
            i
            for i in result["line_items"]
            if "CONFIDO" in str(i.get("product_name") or "").upper()
        ]
        self.assertEqual(len(confido), 2)
        confido_divs = {(i.get("extra") or {}).get("division_code") for i in confido}
        self.assertEqual(confido_divs, {"HIM3", "HIM1"})

        divisions = {d["division_code"] for d in (extra.get("divisions") or [])}
        self.assertTrue({"HIM8", "HIM7", "HIM3", "HIM1", "HIM2"}.issubset(divisions))

    def test_flag_off_does_not_stamp_division(self):
        pages = [
            _page_ok(
                [_item("A"), _item("B"), _item("C")],
                company_name="Himalaya OTX (HIM8)",
            ),
            _page_ok(
                [_item("D"), _item("E"), _item("F")],
                company_name="HIMALAYA ZEAL",
            ),
        ]
        result = self._run_parse(pages, 2, "false")
        self.assertIsNotNone(result)
        assert result is not None
        for item in result["line_items"]:
            self.assertNotIn("division_code", (item.get("extra") or {}))
        self.assertNotIn("divisions", result["totals"]["extra"])


class TestParsePdfPrefersSwilWhenFlagOn(unittest.TestCase):
    def test_flag_on_returns_swil_before_camera_multipage(self):
        from services.sales_statement_extractor import _parse_pdf

        swil_result = empty_result("HIMALAYA (1).pdf", "pdf")
        swil_result["line_items"] = [
            _item("A", division_code="HIM8"),
            _item("B", division_code="HIM8"),
            _item("C", division_code="HIM7"),
        ]
        swil_result["totals"]["extra"] = {
            "extraction_method": "swil_receipt_pur_value_vision",
            "document_type": "group_wise_sales",
            "pages_parsed": 7,
            "pages_incomplete": False,
        }
        fake_doc = mock.MagicMock()
        fake_doc.page_count = 7
        fake_fitz = mock.MagicMock()
        fake_fitz.open.return_value = fake_doc

        with mock.patch.dict(os.environ, {"STOCK_GROUP_WISE_DIVISION_PAGES": "true"}):
            with mock.patch.dict("sys.modules", {"fitz": fake_fitz}):
                with mock.patch(
                    "services.sales_statement_extractor._parse_image_only_swil_receipt_pur_pdf",
                    return_value=swil_result,
                ) as swil:
                    with mock.patch(
                        "services.stock_multipage.parse_camera_multipage_stock_pdf"
                    ) as cam:
                        out = _parse_pdf(b"%PDF-1.4 fake", "HIMALAYA (1).pdf")
        self.assertIs(out, swil_result)
        swil.assert_called_once()
        cam.assert_not_called()

    def test_flag_off_does_not_force_swil_first(self):
        from services.sales_statement_extractor import _parse_pdf

        cam_result = empty_result("x.pdf", "pdf")
        cam_result["line_items"] = [_item("A"), _item("B"), _item("C")]
        cam_result["totals"]["extra"] = {"extraction_method": "vision_table"}
        fake_doc = mock.MagicMock()
        fake_doc.page_count = 2
        fake_fitz = mock.MagicMock()
        fake_fitz.open.return_value = fake_doc

        with mock.patch.dict(os.environ, {"STOCK_GROUP_WISE_DIVISION_PAGES": "false"}):
            with mock.patch.dict("sys.modules", {"fitz": fake_fitz}):
                with mock.patch(
                    "services.sales_statement_extractor._parse_image_only_swil_receipt_pur_pdf"
                ) as swil:
                    with mock.patch(
                        "services.stock_multipage.parse_camera_multipage_stock_pdf",
                        return_value=cam_result,
                    ):
                        out = _parse_pdf(b"%PDF-1.4 fake", "x.pdf")
        self.assertIs(out, cam_result)
        swil.assert_not_called()


if __name__ == "__main__":
    unittest.main()
