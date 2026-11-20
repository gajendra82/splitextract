"""Regression: blue-tick stock statement — Geometry V3 OCR preprocessing.

Fixture: 0000730099_2026_08_ZA_06_333_04092026081833 (2).png
(BELIEVE INTERNATIONAL — Monthly Stock & Sales Statement with blue pen ticks)

Key regression: ARJUNA TAB sales_qty MUST be 26 (blue tick overlaps the cell).
No filename-specific or product-specific OCR rules — generic blue suppression.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

FIXTURE = Path(
    "/var/www/html/splitextract/"
    "0000730099_2026_08_ZA_06_333_04092026081833 (2).png"
)
FIXTURE_ALT = Path(
    "/var/www/html/splitextract/tests/fixtures/"
    "0000730099_2026_08_ZA_06_333_04092026081833.png"
)


def _fixture() -> Path:
    if FIXTURE.is_file():
        return FIXTURE
    return FIXTURE_ALT


@pytest.mark.skipif(not _fixture().is_file(), reason="ARJUNA blue-tick fixture missing")
def test_blue_mask_detects_annotation_not_black_ink():
    import cv2
    from services.stock_geometry_hybrid_v3 import (
        build_blue_annotation_mask,
        remove_blue_preserve_ink,
    )

    # White bg, black digit, bright blue tick
    img = np.full((24, 48, 3), 255, dtype=np.uint8)
    img[8:18, 8:14] = (15, 15, 15)
    img[6:16, 28:40] = (255, 180, 40)  # BGR cyan/blue pen
    mask = build_blue_annotation_mask(img)
    assert int(mask.sum()) > 0
    cleaned, n, stats = remove_blue_preserve_ink(img)
    assert n > 0
    assert int(cleaned[12, 10].mean()) < 80  # black ink preserved
    assert int(cleaned[10, 34].mean()) > 200  # blue tick removed


@pytest.mark.skipif(not _fixture().is_file(), reason="ARJUNA blue-tick fixture missing")
def test_max_channel_is_ocr_only_view():
    import cv2
    from services.stock_geometry_hybrid_v3 import max_channel_blue_suppress

    img = np.zeros((10, 10, 3), dtype=np.uint8)
    img[:, :] = (200, 40, 40)  # strong B
    mx = max_channel_blue_suppress(img)
    assert mx.shape == (10, 10)
    assert int(mx.mean()) >= 200


@pytest.mark.skipif(not _fixture().is_file(), reason="ARJUNA blue-tick fixture missing")
def test_arjuna_sales_qty_26_via_blue_suppression(monkeypatch):
    monkeypatch.setenv("STOCK_BLUE_SUPPRESSION_ENABLED", "true")
    monkeypatch.setenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", "true")
    monkeypatch.delenv("STOCK_BLUE_MORPH_OPEN", raising=False)
    monkeypatch.delenv("STOCK_BLUE_DILATE_PX", raising=False)

    from services.stock_geometry_hybrid_v3 import run_hybrid_v3

    report = run_hybrid_v3(_fixture(), enable_gemini=True)
    assert not report.get("error"), report.get("error")

    blue = report.get("blue_suppression") or {}
    assert blue.get("enabled") is True
    assert int(blue.get("blue_pixels_detected") or 0) > 0
    assert int(blue.get("blue_cells_detected") or 0) > 0

    arjuna = None
    for f in report.get("focus") or []:
        if f.get("product") == "ARJUNA TAB" and f.get("found"):
            arjuna = f
            break
    assert arjuna is not None, "ARJUNA TAB row not detected"
    sel = arjuna.get("selected") or {}
    cells = arjuna.get("cells") or {}

    def _val(field: str):
        if sel.get(field) is not None:
            return sel.get(field)
        c = cells.get(field) or {}
        if c.get("gemini_value") is not None:
            return c.get("gemini_value")
        return c.get("normalized")

    # Tesseract blue-suppression recovers these without inventing neighbors.
    assert _val("opening_qty") == 18.0
    assert _val("sales_qty") == 26.0  # KEY REGRESSION — blue tick cell
    assert _val("closing_qty") == 53.0
    # Purchase/total/goods-ret often need the existing Gemini low-conf fallback.
    gem_applied = int((report.get("gemini") or {}).get("cells_applied") or 0)
    if gem_applied > 0 and _val("purchase_qty") is not None:
        assert _val("purchase_qty") == 60.0
        # Goods Ret → sales_return_qty (crop-verified 1; 18+60+1=79).
        assert _val("sales_return_qty") == 1.0
        assert _val("total_qty") == 79.0
    else:
        # Must not invent a false purchase from max-channel noise (60→5).
        assert _val("purchase_qty") in (None, 60.0)
        purch_cell = cells.get("purchase_qty") or {}
        assert purch_cell.get("ocr_uncertain") or _val("purchase_qty") == 60.0


@pytest.mark.skipif(not _fixture().is_file(), reason="ARJUNA blue-tick fixture missing")
def test_arjuna_sales_26_without_gemini_via_blue_variants(monkeypatch):
    """Tesseract blue/max variants alone must recover sale=26 (no invention)."""
    monkeypatch.setenv("STOCK_BLUE_SUPPRESSION_ENABLED", "true")
    monkeypatch.delenv("STOCK_BLUE_MORPH_OPEN", raising=False)

    import cv2
    from services.stock_geometry_hybrid_v3 import (
        POSITIONAL_10,
        detect_grid,
        ocr_numeric_cell,
        reset_blue_suppression_stats,
    )
    from services.stock_header_resolver import resolve_columns

    bgr = cv2.imread(str(_fixture()))
    assert bgr is not None
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    geom = detect_grid(bgr)
    rows_y, col_xs = geom["row_ys"], geom["col_xs"]
    header_end = 2 if len(rows_y) >= 3 and rows_y[1] - rows_y[0] <= 25 else 1
    patched = [
        {
            "text": POSITIONAL_10[i],
            "col_index": i,
            "x_center": (col_xs[i] + col_xs[i + 1]) / 2.0,
        }
        for i in range(10)
    ]
    columns = list(resolve_columns(patched).get("columns") or [])
    for c in columns:
        idx = int(c["col_index"])
        c["x0"] = int(col_xs[idx])
        c["x1"] = int(col_xs[idx + 1])
    sales = next(c for c in columns if c.get("canonical") == "sales_qty")
    bbox = {
        "x0": int(sales["x0"]),
        "y0": int(rows_y[header_end]),
        "x1": int(sales["x1"]),
        "y1": int(rows_y[header_end + 1]),
    }
    reset_blue_suppression_stats()
    cell = ocr_numeric_cell(bgr, gray, bbox, "sales_qty", quick=False)
    assert cell.normalized == 26.0
    assert cell.ocr_uncertain is False
    assert cell.blue_pixels_removed > 0


@pytest.mark.skipif(not _fixture().is_file(), reason="ARJUNA blue-tick fixture missing")
def test_geometry_table_v3_routes_10col_blue_tick_layout(monkeypatch):
    monkeypatch.setenv("STOCK_BLUE_SUPPRESSION_ENABLED", "true")
    monkeypatch.setenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", "true")

    from services.stock_geometry_table_v3 import run_geometry_table_v3

    report = run_geometry_table_v3(
        _fixture(), enable_gemini=False, max_gemini_cells=0
    )
    assert not report.get("error"), report.get("error")
    assert (report.get("geometry") or {}).get("detection_mode") == "hybrid_10col"
    # Without Gemini, sales must still be 26 from blue-suppressed OCR.
    for r in report.get("rows") or []:
        name = str(r.get("matched_product") or r.get("product_name_ocr") or "")
        if "ARJUNA" in name.upper():
            sel = r.get("selected") or {}
            assert sel.get("sales_qty") == 26.0
            assert sel.get("opening_qty") == 18.0
            assert sel.get("closing_qty") == 53.0
            break
    else:
        pytest.fail("ARJUNA TAB not found in geometry v3 rows")


@pytest.mark.skipif(not _fixture().is_file(), reason="ARJUNA blue-tick fixture missing")
def test_production_path_accepts_hybrid_10col_blue_tick(monkeypatch):
    """Production must NOT fall back to Vision for blue-tick 10-col statements."""
    monkeypatch.setenv("STOCK_BLUE_SUPPRESSION_ENABLED", "true")
    monkeypatch.setenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", "true")
    monkeypatch.setenv("STOCK_VISION_TABLE", "true")
    monkeypatch.setenv("STOCK_VISION_TABLE_TYPES", "image")

    from services.sales_statement_extractor import extract_sales_statement

    result = extract_sales_statement(_fixture().read_bytes(), _fixture().name)
    extra = (result.get("totals") or {}).get("extra") or {}
    assert extra.get("extraction_method") == "geometry_cell_ocr"
    assert extra.get("vision_called") is False or extra.get("geometry_cell_ocr_final")
    assert "hybrid_10col" in str(extra.get("extraction_engine") or extra.get("detection_mode") or "")

    items = result.get("line_items") or []
    assert len(items) >= 3
    arjuna = next(
        (i for i in items if "ARJUNA" in str(i.get("product_name") or "").upper()),
        None,
    )
    assert arjuna is not None
    assert float(arjuna.get("opening_qty") or 0) == 18.0
    assert float(arjuna.get("sales_qty") or 0) == 26.0  # blue-tick key regression
    assert float(arjuna.get("closing_qty") or 0) == 53.0
    # Purchase/total usually filled by Gemini cell fallback when tess is uncertain.
    recv = arjuna.get("receipts_qty")
    total = (arjuna.get("extra") or {}).get("total_stock")
    if recv not in (None, 0, 0.0):
        assert float(recv) == 60.0
    if total not in (None, 0, 0.0):
        assert float(total) == 79.0
