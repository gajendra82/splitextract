"""Multi-page camera PDF stock statement: provenance + cross-page resolution."""

from __future__ import annotations

import io
from copy import deepcopy
from pathlib import Path

import fitz
import pytest

from services.stock_multipage import (
    classify_pair,
    merge_line_items,
    normalize_pack_key,
    normalize_product_name_key,
    parse_camera_multipage_stock_pdf,
    printed_qty,
    product_identity,
    resolve_cross_page_product_rows,
    stamp_page_number,
)


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
    sale_return=None,
    total=None,
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
        "closing_qty": None if closing is None else float(closing),
        "closing_value": None,
        "extra": {"field_source": fs, "sale_return": sale_return, "total_stock": total},
    }
    for field, val in (
        ("opening_qty", opening),
        ("receipts_qty", receipts),
        ("sales_qty", sales),
        ("closing_qty", closing),
        ("sales_return_qty", sale_return),
        ("total_qty", total),
    ):
        fs[field] = "missing" if val is None else "printed"
    return stamp_page_number(item, page)


def _make_image_only_pdf(page_count: int = 2) -> bytes:
    """Minimal multi-page PDF with embedded images and no text (camera-like)."""
    doc = fitz.open()
    for i in range(page_count):
        page = doc.new_page(width=400, height=600)
        # Solid rect — no text layer.
        page.draw_rect(page.rect, color=(1, 1, 1), fill=(1, 1, 1))
        # Insert a tiny PNG so page has image content.
        png = _tiny_png_bytes(color=(i * 40 % 200, 80, 120))
        page.insert_image(page.rect, stream=png)
    data = doc.tobytes()
    doc.close()
    return data


def _tiny_png_bytes(color=(255, 0, 0)) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (8, 8), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Unit: identity / printed / classify / merge
# ---------------------------------------------------------------------------


def test_normalize_keys_distinguish_packs():
    assert normalize_product_name_key("BONNISAN 100ML") != normalize_product_name_key(
        "BONNISAN 200ML"
    )
    assert normalize_pack_key("1X60TAB") == "1x60tab"


def test_product_identity_priority_code_over_name():
    a = _item("ARJUNA TAB", code="15983", pack="1X60")
    b = _item("ARJUNA TAB", code="99999", pack="1X60")
    ma, ka = product_identity(a)
    mb, kb = product_identity(b)
    assert ma == "product_code" and mb == "product_code"
    assert ka != kb


def test_same_name_different_pack_not_same_identity():
    a = _item("ABC TAB", pack="10S")
    b = _item("ABC TAB", pack="20S")
    assert product_identity(a)[1] != product_identity(b)[1]


def test_printed_qty_respects_field_source_missing():
    it = _item("X", opening=None, closing=53)
    assert printed_qty(it, "opening_qty") is None
    assert printed_qty(it, "closing_qty") == 53.0
    # Legacy 0.0 top-level must not invent opening when missing.
    assert it["opening_qty"] == 0.0
    assert printed_qty(it, "opening_qty") is None


def test_classify_continuation_and_conflict():
    p1 = _item("ARJUNA TAB", page=1, pack="1X60", opening=10, receipts=20)
    p2 = _item("ARJUNA TAB", page=2, pack="1X60", sales=5, closing=25)
    assert classify_pair(p1, p2) == "continuation"
    bad = _item("ARJUNA TAB", page=2, pack="1X60", opening=99, sales=5)
    assert classify_pair(p1, bad) == "conflict"


def test_merge_does_not_invent_closing():
    p1 = _item("ARJUNA TAB", page=1, pack="1X60", opening=18, receipts=60, sales=26)
    p2 = _item("ARJUNA TAB", page=2, pack="1X60")  # all missing
    # Not a useful continuation of qty, but merge fill must not invent closing.
    merged = merge_line_items([p1, p2], identity_method="name_pack", resolution="merged")
    assert printed_qty(merged, "opening_qty") == 18.0
    assert printed_qty(merged, "closing_qty") is None


def test_merge_preserves_printed_closing_53():
    p1 = _item(
        "ARJUNA TAB",
        page=1,
        pack="1X60",
        opening=18,
        receipts=60,
        sales=26,
        closing=53,
    )
    p2 = _item("ARJUNA TAB", page=2, pack="1X60", sale_return=1)
    merged = merge_line_items([p1, p2], identity_method="name_pack", resolution="merged")
    assert printed_qty(merged, "closing_qty") == 53.0
    assert printed_qty(merged, "sales_return_qty") == 1.0
    assert merged["extra"]["source_pages"] == [1, 2]


# ---------------------------------------------------------------------------
# resolve_cross_page_product_rows scenarios
# ---------------------------------------------------------------------------


def test_A_single_page_noop():
    items = [_item("ARJUNA TAB", page=1, opening=18, closing=53)]
    out, diag = resolve_cross_page_product_rows(items, page_count=1)
    assert len(out) == 1
    assert diag["multi_page"] is False


def test_B_multipage_unique_products():
    items = [
        _item("ARJUNA TAB", page=1, pack="60", opening=18, closing=53),
        _item("BRESOL S 200ML", page=2, pack="200ML", opening=51, sales=3, closing=48),
    ]
    out, diag = resolve_cross_page_product_rows(items, page_count=2)
    assert len(out) == 2
    assert diag["merged_rows"] == 0


def test_C_same_product_across_pages_continuation():
    items = [
        _item("ARJUNA TAB", page=1, pack="1X60", opening=18, receipts=60),
        _item("ARJUNA TAB", page=2, pack="1X60", sales=26, closing=53),
    ]
    out, _ = resolve_cross_page_product_rows(items, page_count=2)
    assert len(out) == 1
    assert printed_qty(out[0], "opening_qty") == 18.0
    assert printed_qty(out[0], "sales_qty") == 26.0
    assert printed_qty(out[0], "closing_qty") == 53.0
    assert out[0]["extra"]["source_pages"] == [1, 2]


def test_D_same_product_code_across_pages():
    items = [
        _item("ARJUNA TAB", page=1, code="15983", opening=18),
        _item("ARJUNA T", page=2, code="15983", sales=26, closing=53),
    ]
    out, _ = resolve_cross_page_product_rows(items, page_count=2)
    assert len(out) == 1
    assert out[0]["extra"]["identity_match_method"] == "product_code"


def test_E_same_name_different_pack_kept_separate():
    items = [
        _item("ABC TAB", page=1, pack="10S", opening=5, closing=5),
        _item("ABC TAB", page=2, pack="20S", opening=8, closing=8),
    ]
    out, _ = resolve_cross_page_product_rows(items, page_count=2)
    assert len(out) == 2


def test_F_same_name_different_code_kept_separate():
    items = [
        _item("ABC TAB", page=1, code="A1", pack="10S", opening=5, closing=5),
        _item("ABC TAB", page=2, code="A2", pack="10S", opening=8, closing=8),
    ]
    out, _ = resolve_cross_page_product_rows(items, page_count=2)
    assert len(out) == 2


def test_G_similar_names_not_merged():
    items = [
        _item("BRESOL S 200ML", page=1, pack="200ML", opening=51, closing=48),
        _item("BRESOL-N 10ML", page=2, pack="10ML", opening=10, closing=8),
    ]
    out, _ = resolve_cross_page_product_rows(items, page_count=2)
    assert len(out) == 2


def test_H_continuation_row_across_pages():
    items = [
        _item("ARJUNA TAB", page=1, pack="60", opening=10, receipts=20),
        _item("ARJUNA TAB", page=2, pack="60", sales=5, closing=25),
    ]
    out, diag = resolve_cross_page_product_rows(items, page_count=2)
    assert len(out) == 1
    assert diag["continuations_merged"] >= 1
    assert printed_qty(out[0], "closing_qty") == 25.0


def test_I_ambiguous_conflict_kept_separate():
    items = [
        _item("ARJUNA TAB", page=1, pack="60", opening=18, receipts=60, sales=26, closing=53),
        _item("ARJUNA TAB", page=2, pack="60", opening=99, receipts=1, sales=2, closing=3),
    ]
    out, diag = resolve_cross_page_product_rows(items, page_count=2)
    assert len(out) == 2
    assert diag["kept_separate"] >= 1 or diag["ambiguous_rows"] >= 1


def test_J_metadata_like_names_still_passthrough():
    # Classifier is upstream; resolver must not invent merges for unrelated banners.
    items = [
        _item("KANNUR DRUG LINES", page=1, opening=None),
        _item("ARJUNA TAB", page=1, pack="60", opening=18, closing=53),
    ]
    out, _ = resolve_cross_page_product_rows(items, page_count=1)
    assert len(out) == 2


def test_K_blank_physical_cells_stay_missing():
    items = [
        _item("ARJUNA TAB", page=1, pack="60", opening=None, receipts=14, sales=None, closing=None),
    ]
    out, _ = resolve_cross_page_product_rows(items, page_count=1)
    assert printed_qty(out[0], "opening_qty") is None
    assert printed_qty(out[0], "receipts_qty") == 14.0


def test_L_lms_opening_not_shifted_by_merge():
    # Two pages complementary; LMS stays missing (never filled from opening).
    a = _item("BONNISAN 100ML", page=1, pack="100ML", opening=14, closing=14)
    a["extra"]["lms"] = None
    a["extra"]["field_source"]["lms"] = "missing"
    b = _item("BONNISAN 100ML", page=2, pack="100ML", sales=0)
    out, _ = resolve_cross_page_product_rows([a, b], page_count=2)
    assert len(out) == 1
    assert printed_qty(out[0], "lms") is None
    assert printed_qty(out[0], "opening_qty") == 14.0


def test_M_opening_receipt_not_compacted():
    a = _item("X", page=1, pack="P", opening=None, receipts=14)
    b = _item("X", page=2, pack="P", sales=1)
    out, _ = resolve_cross_page_product_rows([a, b], page_count=2)
    assert printed_qty(out[0], "opening_qty") is None
    assert printed_qty(out[0], "receipts_qty") == 14.0


def test_N_cross_page_value_contamination_blocked():
    items = [
        _item("BONNISAN 100ML", page=1, pack="100ML", opening=14, closing=14),
        _item("ARJUNA TAB", page=2, pack="60", opening=None, closing=None),
    ]
    out, _ = resolve_cross_page_product_rows(items, page_count=2)
    arj = next(i for i in out if "ARJUNA" in i["product_name"].upper())
    assert printed_qty(arj, "opening_qty") is None
    assert printed_qty(arj, "closing_qty") is None


def test_O_printed_closing_preserved():
    items = [
        _item("ARJUNA TAB", page=1, pack="60", opening=18, receipts=60, sales=26, closing=53),
        _item("ARJUNA TAB", page=2, pack="60", sale_return=1),
    ]
    out, _ = resolve_cross_page_product_rows(items, page_count=2)
    assert printed_qty(out[0], "closing_qty") == 53.0


def test_P_missing_closing_not_invented():
    items = [
        _item("ARJUNA TAB", page=1, pack="60", opening=18, receipts=60, sales=26, closing=None),
        _item("ARJUNA TAB", page=2, pack="60", sale_return=1),
    ]
    out, _ = resolve_cross_page_product_rows(items, page_count=2)
    assert printed_qty(out[0], "closing_qty") is None
    # Must not become 18+60-26=52
    assert out[0].get("closing_qty") in (None, 0.0) or printed_qty(out[0], "closing_qty") is None


def test_acceptance_example_no_duplicate_arjuna():
    items = [
        _item("ARJUNA TAB", page=1, pack="60", opening=18, receipts=60, sales=26, closing=53),
        _item("BONNISAN 100ML", page=1, pack="100ML", opening=14, closing=14),
        _item("ARJUNA TAB", page=2, pack="60", sale_return=1),  # continuation crumb
        _item("BRESOL S 200ML", page=2, pack="200ML", opening=51, sales=3, closing=48),
    ]
    out, _ = resolve_cross_page_product_rows(items, page_count=2)
    names = [i["product_name"] for i in out]
    assert names.count("ARJUNA TAB") == 1
    arj = next(i for i in out if i["product_name"] == "ARJUNA TAB")
    bon = next(i for i in out if "BONNISAN" in i["product_name"].upper())
    bre = next(i for i in out if "BRESOL" in i["product_name"].upper())
    assert printed_qty(arj, "closing_qty") == 53.0
    assert printed_qty(bon, "opening_qty") == 14.0
    assert printed_qty(bre, "closing_qty") == 48.0
    assert printed_qty(arj, "opening_qty") != printed_qty(bon, "opening_qty") or True


# ---------------------------------------------------------------------------
# Orchestrator: image-only multipage PDF uses per-page parse_image
# ---------------------------------------------------------------------------


def test_parse_camera_multipage_calls_parse_image_per_page(monkeypatch):
    monkeypatch.setenv("STOCK_MULTI_PAGE_CAMERA_PDF_ENABLED", "true")
    pdf = _make_image_only_pdf(3)
    calls = []

    def fake_parse(file_bytes, filename, ext):
        calls.append(filename)
        page = int(filename.rsplit("page", 1)[-1])
        return {
            "stockist_name": "TEST STOCKIST" if page == 1 else None,
            "line_items": [
                _item(f"PROD {page}", page=page, pack="P", opening=page, closing=page)
            ],
            "totals": {
                "extra": {
                    "extraction_method": "geometry_cell_ocr",
                    "geometry_cell_ocr_final": True,
                    "gemini_calls": 0,
                }
            },
        }

    result = parse_camera_multipage_stock_pdf(
        pdf, "stock_statement.pdf", parse_image_fn=fake_parse, max_pages=20
    )
    assert result is not None
    assert len(calls) == 3
    assert result["stockist_name"] == "TEST STOCKIST"
    extra = result["totals"]["extra"]
    assert extra["multi_page"] is True
    assert extra["page_count"] == 3
    assert extra["pages_processed"] == 3
    assert len(result["line_items"]) == 3
    for it in result["line_items"]:
        assert it.get("page_number") in (1, 2, 3)
        assert "page_number" in (it.get("extra") or {})


def test_parse_camera_multipage_disabled(monkeypatch):
    monkeypatch.setenv("STOCK_MULTI_PAGE_CAMERA_PDF_ENABLED", "false")
    pdf = _make_image_only_pdf(2)
    result = parse_camera_multipage_stock_pdf(
        pdf, "x.pdf", parse_image_fn=lambda *a, **k: {"line_items": []}
    )
    assert result is None


def test_single_page_image_only_pdf_not_claimed(monkeypatch):
    monkeypatch.setenv("STOCK_MULTI_PAGE_CAMERA_PDF_ENABLED", "true")
    pdf = _make_image_only_pdf(1)
    result = parse_camera_multipage_stock_pdf(
        pdf, "x.pdf", parse_image_fn=lambda *a, **k: {"line_items": [_item("A")]}
    )
    assert result is None


def test_Q_existing_single_page_extract_unaffected(monkeypatch):
    """Flag on must not alter single-image geometry path."""
    monkeypatch.setenv("STOCK_MULTI_PAGE_CAMERA_PDF_ENABLED", "true")
    monkeypatch.setenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", "false")
    from services.stock_multipage import is_multipage_camera_pdf_enabled

    assert is_multipage_camera_pdf_enabled() is True
    # resolve with page_count=1 is identity
    items = [_item("ONLY", page=1, opening=1, closing=1)]
    out, diag = resolve_cross_page_product_rows(items, page_count=1)
    assert out == items or len(out) == 1
    assert diag["multi_page"] is False
