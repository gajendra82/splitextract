"""Multipage statement grouping by MONTH+YEAR (YYYY-MM), not exact day."""

from __future__ import annotations

import io
import unittest

from services.stock_multipage import (
    build_statement_from_page_group,
    extract_multipage_image_stock_statement,
    group_pages_by_stockist_period,
    normalize_page_period_yyyy_mm,
    stamp_page_number,
)


def _tiny_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (24, 24), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _item(name: str, *, page: int, pack: str = "P") -> dict:
    item = {
        "product_code": None,
        "product_name": name,
        "packing": pack,
        "opening_qty": float(page),
        "receipts_qty": 0.0,
        "sales_qty": 0.0,
        "sales_value": 0.0,
        "closing_qty": float(page),
        "closing_value": 0.0,
        "extra": {
            "field_source": {
                "opening_qty": "printed",
                "receipts_qty": "printed",
                "sales_qty": "printed",
                "closing_qty": "printed",
            }
        },
    }
    return stamp_page_number(item, page)


def _page_entry(
    page_no: int,
    *,
    period_from=None,
    period_to=None,
    stockist="MEENA PHARMA",
    product=None,
    company_name=None,
    sales_value=None,
    closing_value=None,
    opening_value=None,
):
    name = product or f"PROD{page_no}"
    totals_extra = {}
    totals = {
        "opening_value": opening_value,
        "sales_value": sales_value,
        "closing_value": closing_value,
        "extra": totals_extra,
    }
    return {
        "page_number": page_no,
        "filename": f"page_{page_no}.jpg",
        "rows": [_item(name, page=page_no)],
        "line_item_count": 1,
        "stockist_name": stockist,
        "period_from": period_from,
        "period_to": period_to or period_from,
        "company_name": company_name,
        "report_title": None,
        "stockist_address": None,
        "totals": totals,
    }


def _parse_factory(specs):
    """specs: list of dicts with period_from/period_to/stockist/name/company."""
    png = _tiny_png()
    calls = {"n": 0}

    def fake_parse(file_bytes, filename, ext):
        calls["n"] += 1
        i = calls["n"] - 1
        spec = specs[i] if i < len(specs) else specs[-1]
        n = calls["n"]
        extra = {
            "extraction_method": "geometry_cell_ocr",
            "geometry_cell_ocr_final": True,
            "gemini_calls": 0,
        }
        return {
            "stockist_name": spec.get("stockist", "MEENA PHARMA"),
            "company_name": spec.get("company_name"),
            "period_from": spec.get("period_from"),
            "period_to": spec.get("period_to", spec.get("period_from")),
            "line_items": [
                _item(spec.get("name") or f"P{n}", page=n, pack=spec.get("pack", "X"))
            ],
            "totals": {
                "opening_value": spec.get("opening_value"),
                "sales_value": spec.get("sales_value"),
                "closing_value": spec.get("closing_value"),
                "extra": extra,
            },
        }

    return png, fake_parse, calls


class TestNormalizePeriodYYYYMM(unittest.TestCase):
    def test_day_ignored_same_month(self):
        self.assertEqual(
            normalize_page_period_yyyy_mm({"period_from": "2026-09-01"}),
            "2026-09",
        )
        self.assertEqual(
            normalize_page_period_yyyy_mm({"period_from": "2026-09-15"}),
            "2026-09",
        )
        self.assertEqual(
            normalize_page_period_yyyy_mm({"period_from": "2026-09-30"}),
            "2026-09",
        )

    def test_different_month(self):
        self.assertEqual(
            normalize_page_period_yyyy_mm({"period_from": "2026-10-01"}),
            "2026-10",
        )

    def test_missing_period_is_none(self):
        self.assertIsNone(normalize_page_period_yyyy_mm({}))
        self.assertIsNone(normalize_page_period_yyyy_mm({"period_from": None}))


class TestGroupPagesByStockistPeriod(unittest.TestCase):
    def test_same_month_different_dates_one_group(self):
        pages = [
            _page_entry(1, period_from="2026-09-01"),
            _page_entry(2, period_from="2026-09-15"),
            _page_entry(3, period_from="2026-09-30"),
        ]
        groups = group_pages_by_stockist_period(pages, request_id="t1")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["period"], "2026-09")
        self.assertEqual(groups[0]["page_numbers"], [1, 2, 3])

    def test_same_month_two_dates_one_group(self):
        pages = [
            _page_entry(1, period_from="2026-09-05"),
            _page_entry(2, period_from="2026-09-20"),
        ]
        groups = group_pages_by_stockist_period(pages, request_id="t2")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["period"], "2026-09")

    def test_different_month_two_groups(self):
        pages = [
            _page_entry(1, period_from="2026-09-30"),
            _page_entry(2, period_from="2026-10-01"),
        ]
        groups = group_pages_by_stockist_period(pages, request_id="t3")
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["period"], "2026-09")
        self.assertEqual(groups[0]["page_numbers"], [1])
        self.assertEqual(groups[1]["period"], "2026-10")
        self.assertEqual(groups[1]["page_numbers"], [2])

    def test_different_month_after_same_month_pages(self):
        pages = [
            _page_entry(1, period_from="2026-09-01"),
            _page_entry(2, period_from="2026-09-15"),
            _page_entry(3, period_from="2026-09-30"),
            _page_entry(4, period_from="2026-10-01"),
            _page_entry(5, period_from="2026-10-10"),
        ]
        groups = group_pages_by_stockist_period(pages, request_id="t4")
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["page_numbers"], [1, 2, 3])
        self.assertEqual(groups[1]["page_numbers"], [4, 5])
        self.assertEqual(groups[0]["period"], "2026-09")
        self.assertEqual(groups[1]["period"], "2026-10")

    def test_same_yyyy_mm_strings_one_group(self):
        pages = [
            _page_entry(1, period_from="2026-09-01", period_to="2026-09-30"),
            _page_entry(2, period_from="2026-09-01", period_to="2026-09-30"),
        ]
        groups = group_pages_by_stockist_period(pages, request_id="t5")
        self.assertEqual(len(groups), 1)

    def test_different_year_two_groups(self):
        pages = [
            _page_entry(1, period_from="2026-09-01"),
            _page_entry(2, period_from="2027-09-01"),
        ]
        groups = group_pages_by_stockist_period(pages, request_id="t6")
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["period"], "2026-09")
        self.assertEqual(groups[1]["period"], "2027-09")

    def test_continuation_without_date_stays_in_group(self):
        pages = [
            _page_entry(1, period_from="2026-09-01"),
            _page_entry(2, period_from=None, period_to=None, stockist=None),
            _page_entry(3, period_from="2026-09-30"),
        ]
        groups = group_pages_by_stockist_period(pages, request_id="t7")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["period"], "2026-09")
        self.assertEqual(groups[0]["page_numbers"], [1, 2, 3])

    def test_single_page_one_group(self):
        pages = [_page_entry(1, period_from="2026-09-01")]
        groups = group_pages_by_stockist_period(pages, request_id="t8")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["page_numbers"], [1])

    def test_same_stockist_different_months_two_groups(self):
        pages = [
            _page_entry(1, period_from="2026-09-01", stockist="Stockist A"),
            _page_entry(2, period_from="2026-10-01", stockist="Stockist A"),
        ]
        groups = group_pages_by_stockist_period(pages, request_id="t9")
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["period"], "2026-09")
        self.assertEqual(groups[1]["period"], "2026-10")

    def test_different_stockists_same_month_two_groups(self):
        pages = [
            _page_entry(1, period_from="2026-09-01", stockist="Stockist A"),
            _page_entry(2, period_from="2026-09-15", stockist="Stockist B"),
        ]
        groups = group_pages_by_stockist_period(pages, request_id="t10")
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["stockist_key"] != groups[1]["stockist_key"], True)

    def test_column_header_stockist_does_not_split_group(self):
        page1 = _page_entry(1, period_from="2026-09-01", stockist="DROGARIA ANANTA")
        page2 = _page_entry(2, period_from="2026-09-01", stockist="Pharmacy")
        page2["rows"][0]["extra"]["vision_column_headers"] = [
            "Product Name",
            "Pack",
            "Opening",
            "Prev Sale",
            "Pharmacy",
            "Tot Sales",
            "Tot Closing",
            "Stock Value",
        ]
        groups = group_pages_by_stockist_period([page1, page2], request_id="t11")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["page_numbers"], [1, 2])

    def test_different_company_self_contained_two_groups(self):
        """Same stockist+month, different division → two statements."""
        pages = [
            _page_entry(
                1,
                period_from="2026-10-01",
                period_to="2026-10-31",
                stockist="NEW MODERN PHARMA",
                company_name="HIMALAYA ZANDRA DIVI",
                sales_value=19571.52,
                closing_value=50949.38,
                opening_value=63612.11,
                product="ARJUNA TABS",
            ),
            _page_entry(
                2,
                period_from="2026-10-01",
                period_to="2026-10-31",
                stockist="NEW MODERN PHARMA",
                company_name="HIMALAYA ZENITH DIVI",
                sales_value=43516.44,
                closing_value=59633.64,
                opening_value=75010.10,
                product="BONNISAN DROPS",
            ),
        ]
        groups = group_pages_by_stockist_period(pages, request_id="zandra-zenith")
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["page_numbers"], [1])
        self.assertEqual(groups[1]["page_numbers"], [2])
        self.assertEqual(groups[0]["period"], "2026-10")
        self.assertEqual(groups[1]["period"], "2026-10")

    def test_same_company_multipage_stays_one_group(self):
        """True continuation of one division must not split."""
        pages = [
            _page_entry(
                1,
                period_from="2026-10-01",
                stockist="NEW MODERN PHARMA",
                company_name="HIMALAYA ZANDRA",
                product="ROW_A",
            ),
            _page_entry(
                2,
                period_from="2026-10-01",
                stockist="NEW MODERN PHARMA",
                company_name="HIMALAYA ZANDRA DIVI",
                sales_value=100.0,
                closing_value=200.0,
                product="ROW_B",
            ),
        ]
        groups = group_pages_by_stockist_period(pages, request_id="same-div")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["page_numbers"], [1, 2])

    def test_continuation_without_company_does_not_split(self):
        """Headerless page 2 (no company/period) stays with page 1."""
        pages = [
            _page_entry(
                1,
                period_from="2026-10-01",
                stockist="NEW MODERN PHARMA",
                company_name="HIMALAYA ZANDRA",
                sales_value=10.0,
                product="A",
            ),
            _page_entry(
                2,
                period_from=None,
                stockist=None,
                company_name=None,
                product="B",
            ),
        ]
        groups = group_pages_by_stockist_period(pages, request_id="cont-no-co")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["page_numbers"], [1, 2])


class TestExtractMultipagePeriodGrouping(unittest.TestCase):
    def test_same_month_pages_remain_one_statement(self):
        png, fake_parse, _ = _parse_factory(
            [
                {"period_from": "2026-09-01", "name": "A"},
                {"period_from": "2026-09-15", "name": "B"},
                {"period_from": "2026-09-30", "name": "C"},
            ]
        )
        out = extract_multipage_image_stock_statement(
            [("p1.png", png), ("p2.png", png), ("p3.png", png)],
            request_id="same-month",
            parse_image_fn=fake_parse,
        )
        self.assertFalse(out.get("multi_statement"))
        self.assertEqual(out["totals"]["extra"]["final_statement_count"], 1)
        self.assertEqual(out["period_from"], "2026-09-01")
        self.assertEqual(out["period_to"], "2026-09-30")
        names = [i["product_name"] for i in out["line_items"]]
        self.assertEqual(names, ["A", "B", "C"])

    def test_sep_then_oct_returns_two_statements(self):
        png, fake_parse, _ = _parse_factory(
            [
                {"period_from": "2026-09-30", "name": "SEP"},
                {"period_from": "2026-10-01", "name": "OCT"},
            ]
        )
        out = extract_multipage_image_stock_statement(
            [("p1.png", png), ("p2.png", png)],
            request_id="sep-oct",
            parse_image_fn=fake_parse,
        )
        self.assertTrue(out.get("multi_statement"))
        self.assertEqual(out["statement_count"], 2)
        self.assertEqual(out["totals"]["extra"]["final_statement_count"], 2)
        stmts = out["statements"]
        self.assertEqual(stmts[0]["period_from"][:7], "2026-09")
        self.assertEqual(stmts[1]["period_from"][:7], "2026-10")
        self.assertEqual(stmts[0]["line_items"][0]["product_name"], "SEP")
        self.assertEqual(stmts[1]["line_items"][0]["product_name"], "OCT")

    def test_continuation_without_date_stays_one_statement(self):
        png, fake_parse, _ = _parse_factory(
            [
                {"period_from": "2026-09-01", "name": "A", "stockist": "S1"},
                {
                    "period_from": None,
                    "period_to": None,
                    "name": "B",
                    "stockist": None,
                },
                {"period_from": "2026-09-30", "name": "C", "stockist": "S1"},
            ]
        )
        out = extract_multipage_image_stock_statement(
            [("p1.png", png), ("p2.png", png), ("p3.png", png)],
            request_id="cont",
            parse_image_fn=fake_parse,
        )
        self.assertFalse(out.get("multi_statement"))
        self.assertEqual(len(out["line_items"]), 3)
        self.assertEqual(out["period_from"], "2026-09-01")
        self.assertEqual(out["period_to"], "2026-09-30")

    def test_single_page_unchanged_shape(self):
        png, fake_parse, _ = _parse_factory(
            [{"period_from": "2026-09-01", "name": "ONLY"}]
        )
        out = extract_multipage_image_stock_statement(
            [("p1.png", png)],
            request_id="single",
            parse_image_fn=fake_parse,
        )
        self.assertFalse(out.get("multi_statement"))
        self.assertEqual(out["page_count"], 1)
        self.assertEqual(len(out["line_items"]), 1)
        self.assertEqual(out["line_items"][0]["product_name"], "ONLY")

    def test_build_statement_period_span(self):
        group = {
            "period": "2026-09",
            "stockist_key": "meenapharma",
            "page_numbers": [1, 2],
            "pages": [
                _page_entry(1, period_from="2026-09-01", period_to="2026-09-01"),
                _page_entry(2, period_from="2026-09-30", period_to="2026-09-30"),
            ],
        }
        stmt = build_statement_from_page_group(
            group, source_file="x.jpg", source_format="image_multipage"
        )
        self.assertEqual(stmt["period_from"], "2026-09-01")
        self.assertEqual(stmt["period_to"], "2026-09-30")
        self.assertEqual(len(stmt["line_items"]), 2)

    def test_zandra_then_zenith_returns_two_statements(self):
        png, fake_parse, _ = _parse_factory(
            [
                {
                    "period_from": "2026-10-01",
                    "period_to": "2026-10-31",
                    "stockist": "NEW MODERN PHARMA",
                    "company_name": "HIMALAYA ZANDRA DIVI",
                    "name": "ARJUNA TABS",
                    "sales_value": 19571.52,
                    "closing_value": 50949.38,
                    "opening_value": 63612.11,
                },
                {
                    "period_from": "2026-10-01",
                    "period_to": "2026-10-31",
                    "stockist": "NEW MODERN PHARMA",
                    "company_name": "HIMALAYA ZENITH DIVI",
                    "name": "BONNISAN DROPS",
                    "sales_value": 43516.44,
                    "closing_value": 59633.64,
                    "opening_value": 75010.10,
                },
            ]
        )
        out = extract_multipage_image_stock_statement(
            [("195.jpg", png), ("197.jpg", png)],
            request_id="new-modern-div",
            parse_image_fn=fake_parse,
        )
        self.assertTrue(out.get("multi_statement"))
        self.assertEqual(out["statement_count"], 2)
        stmts = out["statements"]
        self.assertEqual(stmts[0]["line_items"][0]["product_name"], "ARJUNA TABS")
        self.assertEqual(stmts[1]["line_items"][0]["product_name"], "BONNISAN DROPS")
        self.assertIn("ZANDRA", str(stmts[0].get("company_name") or "").upper())
        self.assertIn("ZENITH", str(stmts[1].get("company_name") or "").upper())

    def test_same_company_pages_remain_one_statement(self):
        png, fake_parse, _ = _parse_factory(
            [
                {
                    "period_from": "2026-10-01",
                    "stockist": "NEW MODERN PHARMA",
                    "company_name": "HIMALAYA ZANDRA",
                    "name": "A",
                },
                {
                    "period_from": "2026-10-15",
                    "stockist": "NEW MODERN PHARMA",
                    "company_name": "HIMALAYA ZANDRA DIVI",
                    "name": "B",
                    "sales_value": 50.0,
                    "closing_value": 80.0,
                },
            ]
        )
        out = extract_multipage_image_stock_statement(
            [("p1.png", png), ("p2.png", png)],
            request_id="same-zandra",
            parse_image_fn=fake_parse,
        )
        self.assertFalse(out.get("multi_statement"))
        self.assertEqual(out["totals"]["extra"]["final_statement_count"], 1)
        names = [i["product_name"] for i in out["line_items"]]
        self.assertEqual(names, ["A", "B"])


if __name__ == "__main__":
    unittest.main()
