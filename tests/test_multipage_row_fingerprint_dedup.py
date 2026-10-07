"""Regression: multipage dedupe must use full row fingerprint, not product name."""

from __future__ import annotations

from services.stock_multipage import (
    classify_pair,
    is_ocr_near_duplicate,
    resolve_cross_page_product_rows,
    row_fingerprint,
    stamp_page_number,
)


def _item(
    name: str,
    *,
    page: int = 1,
    pack: str | None = None,
    opening=None,
    receipts=None,
    sales=None,
    closing=None,
    opening_value=None,
    sales_value=None,
    closing_value=None,
    receipts_value=None,
) -> dict:
    fs: dict = {}
    item = {
        "product_name": name,
        "packing": pack,
        "opening_qty": 0.0 if opening is None else float(opening),
        "receipts_qty": 0.0 if receipts is None else float(receipts),
        "sales_qty": 0.0 if sales is None else float(sales),
        "closing_qty": None if closing is None else float(closing),
        "opening_value": None if opening_value is None else float(opening_value),
        "sales_value": None if sales_value is None else float(sales_value),
        "closing_value": None if closing_value is None else float(closing_value),
        "extra": {
            "field_source": fs,
            "receipts_value": None
            if receipts_value is None
            else float(receipts_value),
        },
    }
    for field, val in (
        ("opening_qty", opening),
        ("receipts_qty", receipts),
        ("sales_qty", sales),
        ("closing_qty", closing),
        ("opening_value", opening_value),
        ("sales_value", sales_value),
        ("closing_value", closing_value),
        ("receipts_value", receipts_value),
    ):
        fs[field] = "missing" if val is None else "printed"
    return stamp_page_number(item, page)


def test_row_fingerprint_differs_when_values_differ_but_ocr_near_dup_collapses():
    """Same qtys + drifted money field → fingerprint differs, OCR near-dup collapses."""
    a = _item("PRODUCT A", page=1, pack="60", sales=10, closing=20, sales_value=100)
    b = _item("PRODUCT A", page=2, pack="60", sales=10, closing=20, sales_value=999)
    assert row_fingerprint(a) != row_fingerprint(b)
    assert is_ocr_near_duplicate(a, b) is True
    assert classify_pair(a, b) == "exact_duplicate"


def test_row_fingerprint_match_is_exact_duplicate():
    a = _item(
        "PRODUCT A",
        page=1,
        pack="60",
        sales=10,
        closing=20,
        sales_value=100,
        closing_value=200,
    )
    b = _item(
        "PRODUCT A",
        page=2,
        pack="60",
        sales=10,
        closing=20,
        sales_value=100,
        closing_value=200,
    )
    assert row_fingerprint(a) == row_fingerprint(b)
    assert classify_pair(a, b) == "exact_duplicate"


def test_35_plus_9_unique_products_remain_44(monkeypatch):
    """Page1 35 + page2 9 unique names → 44 after dedupe (no name-only collapse)."""
    monkeypatch.setenv("STOCK_MULTIPAGE_ROW_FINGERPRINT_DEDUP", "true")
    items = []
    for i in range(35):
        items.append(
            _item(
                f"P1 PRODUCT {i:02d}",
                page=1,
                pack="P",
                sales=float(i + 1),
                closing=float(100 + i),
                sales_value=float(10 * (i + 1)),
                closing_value=float(20 * (i + 1)),
            )
        )
    for i in range(9):
        items.append(
            _item(
                f"P2 PRODUCT {i:02d}",
                page=2,
                pack="P",
                sales=float(i + 50),
                closing=float(200 + i),
                sales_value=float(30 * (i + 1)),
                closing_value=float(40 * (i + 1)),
            )
        )
    assert len(items) == 44
    out, diag = resolve_cross_page_product_rows(items, page_count=2)
    assert diag["total_before_dedupe"] == 44
    assert len(out) == 44
    assert diag["total_after_dedupe"] == 44
    assert diag["exact_duplicates_collapsed"] == 0
    assert diag["fingerprint_dedup"] is True


def test_same_name_different_values_keeps_both(monkeypatch):
    monkeypatch.setenv("STOCK_MULTIPAGE_ROW_FINGERPRINT_DEDUP", "true")
    items = [
        _item(
            "PRODUCT A",
            page=1,
            pack="60",
            sales=10,
            closing=20,
            sales_value=100,
            closing_value=200,
        ),
        _item(
            "PRODUCT A",
            page=2,
            pack="60",
            sales=99,
            closing=5,
            sales_value=500,
            closing_value=50,
        ),
    ]
    out, diag = resolve_cross_page_product_rows(items, page_count=2)
    assert len(out) == 2
    assert diag["exact_duplicates_collapsed"] == 0
    assert diag["kept_separate"] >= 1 or diag["ambiguous_rows"] >= 1


def test_identical_normalized_rows_collapse_to_one(monkeypatch):
    monkeypatch.setenv("STOCK_MULTIPAGE_ROW_FINGERPRINT_DEDUP", "true")
    items = [
        _item(
            "PRODUCT A",
            page=1,
            pack="60",
            sales=10,
            closing=20,
            sales_value=100,
            closing_value=200,
        ),
        _item(
            "PRODUCT A",
            page=2,
            pack="60",
            sales=10,
            closing=20,
            sales_value=100,
            closing_value=200,
        ),
    ]
    out, diag = resolve_cross_page_product_rows(items, page_count=2)
    assert len(out) == 1
    assert diag["exact_duplicates_collapsed"] == 1
    assert diag["duplicates_removed"] == 1
    assert diag["total_before_dedupe"] == 2
    assert diag["total_after_dedupe"] == 1


def test_continuation_still_merges_complementary_halves(monkeypatch):
    """Fingerprint mode must not break sparse cross-page continuation."""
    monkeypatch.setenv("STOCK_MULTIPAGE_ROW_FINGERPRINT_DEDUP", "true")
    items = [
        _item("ARJUNA TAB", page=1, pack="1X60", opening=18, receipts=60),
        _item("ARJUNA TAB", page=2, pack="1X60", sales=26, closing=53),
    ]
    out, diag = resolve_cross_page_product_rows(items, page_count=2)
    assert len(out) == 1
    assert diag["continuations_merged"] >= 1
    assert out[0]["extra"]["source_pages"] == [1, 2]


def test_name_alone_never_collapses_complete_sale_closing_rows(monkeypatch):
    monkeypatch.setenv("STOCK_MULTIPAGE_ROW_FINGERPRINT_DEDUP", "true")
    # Same name, both complete SALE+CLOSING shapes, different figures.
    a = _item("LIV-52 TAB.", page=1, pack="60", sales=79, closing=379, sales_value=1000)
    b = _item("LIV-52 TAB.", page=2, pack="60", sales=12, closing=40, sales_value=200)
    assert classify_pair(a, b) in ("conflict", "independent")
    out, _ = resolve_cross_page_product_rows([a, b], page_count=2)
    assert len(out) == 2


def test_packless_ocr_fragment_collapses_to_packed_row(monkeypatch):
    """Same-name pack-less junk row must collapse into the packed product row."""
    monkeypatch.setenv("STOCK_MULTIPAGE_ROW_FINGERPRINT_DEDUP", "true")
    good = _item(
        "RENALKA SYP.",
        page=1,
        pack="100мг.",
        sales=6,
        closing=26,
        sales_value=454,
        closing_value=1967,
    )
    junk = _item(
        "RENALKA SYP.",
        page=1,
        pack=None,
        opening=142,
        opening_value=142,
        sales=0,
        closing=142,
        sales_value=0,
        closing_value=0,
    )
    page2 = _item(
        "RENALKA SYP",
        page=2,
        pack="100ML",
        sales=6,
        closing=26,
        sales_value=454,
        closing_value=1967,
    )
    out, diag = resolve_cross_page_product_rows([good, junk, page2], page_count=2)
    assert len(out) == 1
    assert diag["duplicates_removed"] >= 2
    assert out[0].get("packing")


def test_pack_ocr_drift_100_vs_100ml_collapses(monkeypatch):
    """Same product, pack OCR 100мг→100 vs 100ML must still collapse."""
    monkeypatch.setenv("STOCK_MULTIPAGE_ROW_FINGERPRINT_DEDUP", "true")
    a = _item(
        "LIV-52 SYP.100ML",
        page=1,
        pack="100мг.",
        sales=140,
        closing=64,
        sales_value=13237,
        closing_value=6051,
    )
    b = _item(
        "LIV-52 SYP.100ML",
        page=2,
        pack="100ML",
        sales=140,
        closing=64,
        sales_value=13237,
        closing_value=6051,
    )
    out, diag = resolve_cross_page_product_rows([a, b], page_count=2)
    assert len(out) == 1
    assert diag["duplicates_removed"] == 1


def test_cyrillic_tab_confusable_collapses(monkeypatch):
    monkeypatch.setenv("STOCK_MULTIPAGE_ROW_FINGERPRINT_DEDUP", "true")
    a = _item(
        "HERBOLAX TAB.",
        page=1,
        pack="100'S",
        sales=11,
        closing=36,
        sales_value=1520,
        closing_value=4975,
    )
    b = _item(
        "HERBOLAX ТАВ.",  # Cyrillic А/В
        page=2,
        pack="100'S",
        sales=11,
        closing=36,
        sales_value=1520,
        closing_value=4975,
    )
    out, _ = resolve_cross_page_product_rows([a, b], page_count=2)
    assert len(out) == 1


def test_ocr_column_swap_near_duplicate_collapses(monkeypatch):
    """Page-overlap Vision swap of sale/closing must collapse to one product."""
    monkeypatch.setenv("STOCK_MULTIPAGE_ROW_FINGERPRINT_DEDUP", "true")
    a = _item(
        "LIV-52 SYP.100ML",
        page=1,
        pack="100ML",
        sales=64,
        closing=140,
        sales_value=6051,
        closing_value=13237,
    )
    b = _item(
        "LIV-52 SYP.100ML",
        page=2,
        pack="100ML",
        sales=140,  # swapped qty
        closing=64,
        sales_value=13237,  # swapped value
        closing_value=6051,
    )
    assert row_fingerprint(a) != row_fingerprint(b)
    assert is_ocr_near_duplicate(a, b) is True
    assert classify_pair(a, b) == "exact_duplicate"
    out, diag = resolve_cross_page_product_rows([a, b], page_count=2)
    assert len(out) == 1
    assert diag["exact_duplicates_collapsed"] == 1


def test_ssa_page_overlap_48_collapses_to_44_unique(monkeypatch):
    """Camera overlap: 44 unique products + 4 OCR re-reads → 44 after dedupe."""
    monkeypatch.setenv("STOCK_MULTIPAGE_ROW_FINGERPRINT_DEDUP", "true")
    names = [
        "AACTARIL SOAP",
        "ABANA TAB.",
        "ALTHEA LOTION",
        "BLEMINOR ANTIBLEMISH CREA",
        "CLARINA ANTI ACNE CREAM",
        "CLARINA FACE WASH",
        "CONFIDO TAB.",
        "DIABECON DS TAB",
        "DIABECON TAB",
        "DIAREX SYP.",
        "DIAREX TAB.",
        "HADJOD TAB.",
        "HAIR ZONE SOLUTION",
        "HERBOLAX CAP.",
        "HERBOLAX TAB.",
        "HIMCOLIN GEL",
        "KAPIKACHHU",
        "LIV-52 HB CAP.",
        "LIV-52 SF SYP",
        "LIV-52 SYP.100ML",
        "LIV-52 SYP.200ML",
        "LIV-52 TAB.",
        "MANJISTHA CAP.",
        "OPHTHACARE DROPS",
        "ORO-T ORAL RINSE",
        "OXITRAD CAP.",
        "PILEX FORT OINT.",
        "PILEX FORTE TAB",
        "PILEX TAB.",
        "PURIM TAB.",
        "QUISTA ACTIVE 200G (CF)",
        "QUISTA ACTIVE 200MG.(MMF)",
        "RENALKA SYP.",
        "RENALKA SYRUP 200ML",
        "SHALLAKI CAP.",
        "SHIGRU TAB",
        "TAGARA CAP.",
        "TALEKT TAB.",
        "TENTAX FORTE TAB.",
        "TRIKATU TAB.",
        "TRIPHALA TAB",
        "VASAKA TAB.",
        "VRIKSHAMLA CAP",
        "YASTHIMADHU CAP",
    ]
    assert len(names) == 44
    items = []
    for i, n in enumerate(names):
        items.append(
            _item(
                n,
                page=1 if i < 35 else 2,
                pack="P",
                sales=float(10 + i),
                closing=float(100 + i),
                sales_value=float(1000 + i),
                closing_value=float(2000 + i),
            )
        )
    # 4 OCR re-reads that previously inflated 44 → 48
    items.append(
        _item(
            "LIV-52 SYP.100ML",
            page=2,
            pack="P",
            sales=100 + 19,  # swapped-ish vs index 19
            closing=10 + 19,
            sales_value=2000 + 19,
            closing_value=1000 + 19,
        )
    )
    items.append(
        _item(
            "OPHTHACARE DROPS",
            page=2,
            pack="P",
            sales=100 + 23,
            closing=10 + 23,
            sales_value=2000 + 23,
            closing_value=1000 + 23,
        )
    )
    items.append(
        _item(
            "RENALKA SYP.",
            page=1,
            pack="P",
            sales=10 + 32,
            closing=100 + 32,
            sales_value=1000 + 32,
            closing_value=2000 + 32,
        )
    )
    items.append(
        _item(
            "RENALKA SYP",
            page=2,
            pack="P",
            sales=100 + 32,
            closing=10 + 32,
            sales_value=2000 + 32,
            closing_value=1000 + 32,
        )
    )
    assert len(items) == 48
    out, diag = resolve_cross_page_product_rows(items, page_count=2)
    assert diag["total_before_dedupe"] == 48
    assert len(out) == 44
    assert diag["total_after_dedupe"] == 44
    assert diag["duplicates_removed"] >= 4
