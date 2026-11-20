"""POST /extract-sales-statement-multi — ordered page images → one statement."""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from services.stock_geometry_table_v3 import PHYSICAL_SOURCE_COLUMNS
from services.stock_multipage import (
    extract_multipage_image_stock_statement,
    resolve_cross_page_product_rows,
    stamp_page_number,
)


def _tiny_png(color=(40, 80, 120)) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (32, 32), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _item(
    name: str,
    *,
    page: int = 1,
    code: str | None = None,
    pack: str | None = None,
    opening=None,
    receipts=None,
    sales=None,
    closing=None,
) -> dict:
    fs = {}
    item = {
        "product_code": code,
        "product_name": name,
        "packing": pack,
        "opening_qty": 0.0 if opening is None else float(opening),
        "receipts_qty": 0.0 if receipts is None else float(receipts),
        "sales_qty": 0.0 if sales is None else float(sales),
        "sales_value": 0.0,
        "closing_qty": 0.0 if closing is None else float(closing),
        "closing_value": 0.0,
        "extra": {"field_source": fs},
    }
    for field, val in (
        ("opening_qty", opening),
        ("receipts_qty", receipts),
        ("sales_qty", sales),
        ("closing_qty", closing),
    ):
        fs[field] = "missing" if val is None else "printed"
    return stamp_page_number(item, page)


def _page_result(items, *, stockist="TEST STOCKIST", method="geometry_cell_ocr"):
    return {
        "stockist_name": stockist,
        "line_items": items,
        "totals": {
            "extra": {
                "extraction_method": method,
                "geometry_cell_ocr_final": "geometry" in method,
                "gemini_calls": 0,
            }
        },
    }


@pytest.fixture
def app_client(monkeypatch):
    import asyncio

    monkeypatch.setenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", "false")
    import app as app_module
    import services.sales_extraction_runtime as runtime

    runtime.clear_sales_deadline()
    runtime._sales_extraction_async_sem = asyncio.Semaphore(
        runtime.MAX_CONCURRENT_EXTRACTIONS
    )
    with runtime._sales_extraction_waiters_lock:
        runtime._sales_extraction_active = 0
        runtime._sales_extraction_waiting = 0
    return TestClient(app_module.app), app_module, runtime


def test_physical_column_mapping_unchanged():
    assert PHYSICAL_SOURCE_COLUMNS[2] == "lms"
    assert PHYSICAL_SOURCE_COLUMNS[3] == "opening_qty"
    assert PHYSICAL_SOURCE_COLUMNS[4] == "receipts_qty"
    assert PHYSICAL_SOURCE_COLUMNS[6] == "sales_qty"
    assert PHYSICAL_SOURCE_COLUMNS[10] == "closing_qty"
    assert PHYSICAL_SOURCE_COLUMNS[11] == "closing_value"


def test_two_page_request_one_statement():
    png = _tiny_png()
    calls = []

    def fake_parse(file_bytes, filename, ext):
        calls.append(filename)
        n = len(calls)
        return _page_result(
            [_item(f"PROD {n}", page=n, pack="P", opening=n, closing=n)],
            stockist="S1" if n == 1 else None,
        )

    out = extract_multipage_image_stock_statement(
        [("p1.png", png), ("p2.png", png)],
        request_id="r2",
        stockist_id="99",
        month="2026-08",
        parse_image_fn=fake_parse,
    )
    assert out["success"] is True
    assert out["is_multi_page"] is True
    assert out["page_count"] == 2
    assert out["pages_processed"] == 2
    assert out["stockist_id"] == "99"
    assert out["month"] == "2026-08"
    assert out["stockist_name"] == "S1"
    assert len(out["page_results"]) == 2
    assert out["page_results"][0]["page_number"] == 1
    assert out["page_results"][1]["page_number"] == 2
    assert calls == ["p1.png", "p2.png"]
    extra = out["totals"]["extra"]
    assert extra["external_extraction_request_count"] == 1
    assert extra["final_statement_count"] == 1


def test_five_page_request_counts():
    png = _tiny_png()
    calls = []

    def fake_parse(file_bytes, filename, ext):
        calls.append(filename)
        n = len(calls)
        return _page_result([_item(f"P{n}", page=n, pack="X", opening=n, closing=n)])

    out = extract_multipage_image_stock_statement(
        [(f"page{i}.png", png) for i in range(1, 6)],
        request_id="r5",
        parse_image_fn=fake_parse,
    )
    assert out["page_count"] == 5
    assert out["pages_processed"] == 5
    assert len(calls) == 5
    assert out["totals"]["extra"]["external_extraction_request_count"] == 1
    assert out["totals"]["extra"]["final_statement_count"] == 1
    assert len(out["line_items"]) == 5


def test_page_order_preservation():
    png = _tiny_png()
    seen = []

    def fake_parse(file_bytes, filename, ext):
        seen.append(filename)
        return _page_result(
            [_item(filename, page=len(seen), pack="P", opening=1, closing=1)]
        )

    pages = [("c.png", png), ("a.png", png), ("b.png", png)]
    out = extract_multipage_image_stock_statement(
        pages, parse_image_fn=fake_parse, request_id="ord"
    )
    assert seen == ["c.png", "a.png", "b.png"]
    assert [p["page_number"] for p in out["page_results"]] == [1, 2, 3]
    assert [p["filename"] for p in out["page_results"]] == ["c.png", "a.png", "b.png"]


def test_maximum_10_pages_rejected():
    png = _tiny_png()
    from services.stock_multipage import MultiPageImageExtractionError

    with pytest.raises(MultiPageImageExtractionError) as ei:
        extract_multipage_image_stock_statement(
            [(f"p{i}.png", png) for i in range(11)],
            parse_image_fn=lambda *a, **k: _page_result([]),
        )
    assert ei.value.error == "too_many_pages"


def test_empty_page_rejection():
    from services.stock_multipage import MultiPageImageExtractionError

    with pytest.raises(MultiPageImageExtractionError) as ei:
        extract_multipage_image_stock_statement(
            [("ok.png", _tiny_png()), ("empty.png", b"")],
            parse_image_fn=lambda *a, **k: _page_result([]),
        )
    assert ei.value.error == "empty_page"
    assert ei.value.page_number == 2


def test_invalid_image_rejection():
    from services.stock_multipage import MultiPageImageExtractionError

    with pytest.raises(MultiPageImageExtractionError) as ei:
        extract_multipage_image_stock_statement(
            [("bad.bin", b"not-an-image-at-all!!!!!!!!!!")],
            parse_image_fn=lambda *a, **k: _page_result([]),
        )
    assert ei.value.error == "invalid_image"
    assert ei.value.page_number == 1


def test_one_failed_page_aborts():
    from services.stock_multipage import MultiPageImageExtractionError

    png = _tiny_png()

    def fake_parse(file_bytes, filename, ext):
        if "fail" in filename:
            raise RuntimeError("boom")
        return _page_result([_item("OK", page=1, pack="P", opening=1, closing=1)])

    with pytest.raises(MultiPageImageExtractionError) as ei:
        extract_multipage_image_stock_statement(
            [("p1.png", png), ("fail.png", png)],
            parse_image_fn=fake_parse,
        )
    assert ei.value.error == "page_processing_failed"
    assert ei.value.page_number == 2


def test_stockist_month_metadata_preserved():
    png = _tiny_png()
    out = extract_multipage_image_stock_statement(
        [("p1.png", png)],
        stockist_id="SID-7",
        month="2026-09",
        parse_image_fn=lambda *a, **k: _page_result(
            [_item("A", pack="P", opening=1, closing=1)], stockist="FromPage"
        ),
    )
    assert out["stockist_id"] == "SID-7"
    assert out["month"] == "2026-09"
    assert out["stockist_name"] == "FromPage"
    assert out["totals"]["extra"]["stockist_id"] == "SID-7"
    assert out["totals"]["extra"]["month"] == "2026-09"


def test_lms_opening_not_shifted_across_pages():
    a = _item("BONNISAN", page=1, pack="100ML", opening=14, closing=14)
    a["extra"]["lms"] = None
    a["extra"]["field_source"]["lms"] = "missing"
    b = _item("BONNISAN", page=2, pack="100ML", sales=0)
    out, _ = resolve_cross_page_product_rows([a, b], page_count=2)
    assert len(out) == 1
    from services.stock_multipage import printed_qty

    assert printed_qty(out[0], "lms") is None
    assert printed_qty(out[0], "opening_qty") == 14.0


def test_opstk_receipt_not_compacted():
    a = _item("X", page=1, pack="P", opening=None, receipts=14)
    b = _item("X", page=2, pack="P", sales=1)
    out, _ = resolve_cross_page_product_rows([a, b], page_count=2)
    from services.stock_multipage import printed_qty

    assert printed_qty(out[0], "opening_qty") is None
    assert printed_qty(out[0], "receipts_qty") == 14.0


def test_duplicate_product_not_blindly_removed():
    items = [
        _item("ARJUNA TAB", page=1, pack="60", opening=18, receipts=60, sales=26, closing=53),
        _item("ARJUNA TAB", page=2, pack="60", opening=99, receipts=1, sales=2, closing=3),
    ]
    out, diag = resolve_cross_page_product_rows(items, page_count=2)
    assert len(out) == 2
    assert diag["kept_separate"] >= 1 or diag["ambiguous_rows"] >= 1


def test_repeated_page_headers_ignored():
    png = _tiny_png()

    def fake_parse(file_bytes, filename, ext):
        n = 1 if "p1" in filename else 2
        rows = [
            _item("Product", page=n),
            _item("ARJUNA", page=n, pack="60", opening=10 if n == 1 else 11, closing=10 if n == 1 else 11),
        ]
        return _page_result(rows)

    out = extract_multipage_image_stock_statement(
        [("p1.png", png), ("p2.png", png)],
        parse_image_fn=fake_parse,
    )
    names = [i["product_name"] for i in out["line_items"]]
    assert "Product" not in names
    for pr in out["page_results"]:
        assert all(r["product_name"] != "Product" for r in pr["rows"])


def test_one_request_one_final_statement():
    png = _tiny_png()
    out = extract_multipage_image_stock_statement(
        [("a.png", png), ("b.png", png)],
        parse_image_fn=lambda *a, **k: _page_result(
            [_item("Z", pack="P", opening=1, closing=1)]
        ),
    )
    assert out["totals"]["extra"]["statement_count"] == 1
    assert out["totals"]["extra"]["final_statement_count"] == 1
    assert isinstance(out["line_items"], list)


def test_endpoint_multipart_files_bracket(app_client):
    client, app_module, runtime = app_client
    png = _tiny_png()

    def fake_extract(pages, **kwargs):
        assert len(pages) == 2
        assert pages[0][0] == "page1.png"
        assert pages[1][0] == "page2.png"
        return {
            "success": True,
            "is_multi_page": True,
            "page_count": 2,
            "pages_processed": 2,
            "extraction_engine": "geometry_v3",
            "line_items": [_item("A", pack="P", opening=1, closing=1)],
            "page_results": [
                {"page_number": 1, "filename": "page1.png", "rows": []},
                {"page_number": 2, "filename": "page2.png", "rows": []},
            ],
            "totals": {
                "extra": {
                    "external_extraction_request_count": 1,
                    "final_statement_count": 1,
                }
            },
        }

    with patch(
        "services.stock_multipage.extract_multipage_image_stock_statement",
        side_effect=fake_extract,
    ):
        resp = client.post(
            "/extract-sales-statement-multi",
            data={
                "stockist_id": "42",
                "month": "2026-08",
                "is_multi_page": "true",
            },
            files=[
                ("files[]", ("page1.png", png, "image/png")),
                ("files[]", ("page2.png", png, "image/png")),
            ],
            headers={"X-Request-ID": "multi-ep-1"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["page_count"] == 2
    with runtime._sales_extraction_waiters_lock:
        assert runtime._sales_extraction_active == 0


def test_endpoint_failed_page_returns_error_shape(app_client):
    client, _, runtime = app_client
    from services.stock_multipage import MultiPageImageExtractionError

    png = _tiny_png()

    def boom(*a, **k):
        raise MultiPageImageExtractionError(
            page_number=3,
            message="Unable to process page 3",
            error="page_processing_failed",
        )

    with patch(
        "services.stock_multipage.extract_multipage_image_stock_statement",
        side_effect=boom,
    ):
        resp = client.post(
            "/extract-sales-statement-multi",
            data={"stockist_id": "1", "month": "2026-08", "is_multi_page": "true"},
            files=[
                ("files[]", ("p1.png", png, "image/png")),
                ("files[]", ("p2.png", png, "image/png")),
                ("files[]", ("p3.png", png, "image/png")),
            ],
        )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"] == "page_processing_failed"
    assert body["page_number"] == 3
    assert "Unable to process page 3" in body["message"]
    with runtime._sales_extraction_waiters_lock:
        assert runtime._sales_extraction_active == 0


def test_existing_single_file_endpoint_unchanged(app_client):
    client, app_module, runtime = app_client

    def ok_result(file_bytes, filename):
        return {
            "source_file": filename,
            "stockist_name": "Single",
            "line_items": [_item("ONLY", pack="P", opening=1, closing=1)],
            "totals": {"extra": {"extraction_method": "mock"}},
        }

    with patch.object(app_module, "extract_sales_statement", side_effect=ok_result):
        resp = client.post(
            "/extract-sales-statement",
            files={"file": ("ok.png", _tiny_png(), "image/png")},
            headers={"X-Request-ID": "single-reg-1"},
        )
    assert resp.status_code == 200
    assert resp.json().get("stockist_name") == "Single"
    assert "is_multi_page" not in resp.json() or resp.json().get("is_multi_page") is not True
    with runtime._sales_extraction_waiters_lock:
        assert runtime._sales_extraction_active == 0


def test_endpoint_max_pages_http(app_client):
    client, _, _ = app_client
    png = _tiny_png()
    files = [("files[]", (f"p{i}.png", png, "image/png")) for i in range(11)]
    resp = client.post(
        "/extract-sales-statement-multi",
        data={"stockist_id": "1", "month": "2026-08", "is_multi_page": "true"},
        files=files,
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"] == "too_many_pages"
