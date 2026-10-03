"""Unit tests for isolated stock OCR benchmark path (no live API required)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from services.stock_header_resolver import resolve_columns
from services.stock_ocr_benchmark import (
    ARJUNA_EXPECTED,
    BONNISAN_DROPS_EXPECTED,
    compare_cells,
)
from services.stock_ocr_providers.base import OCRBlock, OCRDocumentResult, OCRPage, OCRTable
from services.stock_ocr_table_reconstructor import (
    detect_column_shift,
    fields_to_line_item,
    parse_html_table,
    reconstruct_from_html_tables,
    reconstruct_stock_table,
)


def test_parse_html_preserves_blank_cells():
    html = """
    <table>
      <tr><th>Opening</th><th>Purchase</th><th>Goods Ret</th><th>Total In</th>
          <th>Sale</th><th>Purc. Ret</th><th>Balance</th></tr>
      <tr><td>18</td><td>60</td><td>1</td><td>79</td><td>26</td><td></td><td>53</td></tr>
    </table>
    """
    grid = parse_html_table(html)
    assert grid[1] == ["18", "60", "1", "79", "26", None, "53"]


def test_header_purc_ret_never_purchase():
    cells = [
        {"text": "Opening Qty", "col_index": 0, "x_center": 0.1},
        {"text": "Purchase Qty", "col_index": 1, "x_center": 0.2},
        {"text": "Goods Ret. Qty", "col_index": 2, "x_center": 0.3},
        {"text": "Total In. Qty", "col_index": 3, "x_center": 0.4},
        {"text": "Sale Qty", "col_index": 4, "x_center": 0.5},
        {"text": "Purc. Ret. Qty", "col_index": 5, "x_center": 0.6},
        {"text": "Balance Qty", "col_index": 6, "x_center": 0.7},
    ]
    resolved = resolve_columns(cells)
    by = {c["col_index"]: c["canonical"] for c in resolved["columns"]}
    assert by[1] == "purchase_qty"
    assert by[5] == "purchase_return_qty"
    assert by[2] == "sales_return_qty"
    assert by[6] == "closing_qty"
    assert by[3] == "total_qty"


def test_goods_ret_and_balance_aliases():
    for text, canon in [
        ("Goods Ret", "sales_return_qty"),
        ("Goods Return", "sales_return_qty"),
        ("Sales Return", "sales_return_qty"),
        ("Balance", "closing_qty"),
        ("Closing Qty", "closing_qty"),
        ("Stock", "closing_qty"),
    ]:
        resolved = resolve_columns(
            [{"text": text, "col_index": 0, "x_center": 0.5}]
        )
        assert resolved["columns"][0]["canonical"] == canon, text


def test_printed_closing_preserved_not_overwritten():
    printed = [
        "opening_qty",
        "purchase_qty",
        "sales_return_qty",
        "total_qty",
        "sales_qty",
        "purchase_return_qty",
        "closing_qty",
        "product_name",
    ]
    item = fields_to_line_item(
        {
            "product_name": "ARJUNA TAB",
            "opening_qty": "18",
            "purchase_qty": "60",
            "sales_return_qty": "1",
            "total_qty": "79",
            "sales_qty": "26",
            "purchase_return_qty": None,
            "closing_qty": "53",
        },
        printed_fields=printed,
        row_index=0,
    )
    assert item["closing_qty"] == 53.0
    assert item["extra"]["field_source"]["closing_qty"] == "printed"
    # Wrong sale must not invent closing via derive when Balance column printed
    bad = fields_to_line_item(
        {
            "product_name": "ARJUNA TAB",
            "opening_qty": "18",
            "purchase_qty": "60",
            "sales_return_qty": "1",
            "total_qty": "79",
            "sales_qty": "29",
            "purchase_return_qty": None,
            "closing_qty": None,  # OCR missed Balance
        },
        printed_fields=printed,
        row_index=1,
    )
    assert bad["closing_qty"] is None
    assert bad["extra"]["field_source"]["closing_qty"] == "missing"
    # calculated may exist separately
    calc = bad["extra"].get("calculated_closing_qty")
    if calc is not None:
        assert bad["closing_qty"] != calc


def test_calculated_closing_stored_separately_when_balance_missing():
    printed = [
        "opening_qty",
        "purchase_qty",
        "sales_qty",
        "closing_qty",
        "product_name",
    ]
    item = fields_to_line_item(
        {
            "product_name": "X",
            "opening_qty": "18",
            "purchase_qty": "60",
            "sales_qty": "29",
            "closing_qty": None,
        },
        printed_fields=printed,
        row_index=0,
    )
    assert item["closing_qty"] is None
    assert item["extra"].get("calculated_closing_qty") == 49.0


def test_html_reconstruct_arjuna_exact():
    html = """
    <table>
      <tr>
        <th>Code</th><th>Product</th><th>Pack</th>
        <th>Opening Qty</th><th>Purchase Qty</th><th>Goods Ret. Qty</th>
        <th>Total In. Qty</th><th>Sale Qty</th><th>Purc. Ret. Qty</th><th>Balance Qty</th>
      </tr>
      <tr>
        <td>15983</td><td>ARJUNA TAB</td><td>1X60TAB</td>
        <td>18</td><td>60</td><td>1</td><td>79</td><td>26</td><td></td><td>53</td>
      </tr>
      <tr>
        <td>12598</td><td>BONNISAN DROPS</td><td>1X30ML</td>
        <td></td><td>500</td><td>3</td><td>503</td><td>318</td><td></td><td>185</td>
      </tr>
    </table>
    """
    page = OCRPage(
        page_number=1,
        tables=[OCRTable(table_id="t0", format="html", content=html, page=1)],
    )
    built = reconstruct_from_html_tables(page)
    assert built is not None
    items = {i["product_name"]: i for i in built["line_items"]}
    arjuna = items["ARJUNA TAB"]
    assert arjuna["opening_qty"] == 18.0
    assert arjuna["receipts_qty"] == 60.0
    assert arjuna["extra"]["sale_return"] == 1.0
    assert arjuna["extra"]["total_stock"] == 79.0
    assert arjuna["sales_qty"] == 26.0
    assert arjuna["closing_qty"] == 53.0
    # blank Purc Ret must not steal Balance
    assert arjuna["extra"]["field_source"]["purchase_return_qty"] == "missing"
    assert arjuna["extra"]["ocr_raw_cells"][8] is None
    assert arjuna["extra"]["ocr_raw_cells"][9] == "53"

    drops = items["BONNISAN DROPS"]
    assert drops["sales_qty"] == 318.0
    assert drops["closing_qty"] == 185.0


def test_bbox_column_assignment_preserves_blank():
    # Synthetic columns with x centres; blank Purc Ret between Sale and Balance
    headers = [
        OCRBlock("Opening Qty", bbox=(10, 10, 40, 25), page=1, block_type="text"),
        OCRBlock("Purchase Qty", bbox=(50, 10, 80, 25), page=1, block_type="text"),
        OCRBlock("Goods Ret Qty", bbox=(90, 10, 120, 25), page=1, block_type="text"),
        OCRBlock("Total In Qty", bbox=(130, 10, 160, 25), page=1, block_type="text"),
        OCRBlock("Sale Qty", bbox=(170, 10, 200, 25), page=1, block_type="text"),
        OCRBlock("Purc Ret Qty", bbox=(210, 10, 240, 25), page=1, block_type="text"),
        OCRBlock("Balance Qty", bbox=(250, 10, 280, 25), page=1, block_type="text"),
        OCRBlock("Product", bbox=(300, 10, 360, 25), page=1, block_type="text"),
    ]
    # Row y≈50: product at right, numbers at column centres; no Purc Ret token
    data = [
        OCRBlock("ARJUNA TAB", bbox=(300, 45, 380, 60), page=1, block_type="text"),
        OCRBlock("18", bbox=(15, 45, 35, 60), page=1, block_type="text"),
        OCRBlock("60", bbox=(55, 45, 75, 60), page=1, block_type="text"),
        OCRBlock("1", bbox=(95, 45, 115, 60), page=1, block_type="text"),
        OCRBlock("79", bbox=(135, 45, 155, 60), page=1, block_type="text"),
        OCRBlock("26", bbox=(175, 45, 195, 60), page=1, block_type="text"),
        OCRBlock("53", bbox=(255, 45, 275, 60), page=1, block_type="text"),
    ]
    page = OCRPage(page_number=1, blocks=headers + data, width=400, height=100)
    ocr = OCRDocumentResult(provider="mistral", pages=[page])
    result = reconstruct_stock_table(ocr)
    assert result["status"] == "ok"
    item = result["line_items"][0]
    assert item["product_name"] == "ARJUNA TAB"
    assert item["sales_qty"] == 26.0
    assert item["closing_qty"] == 53.0
    assert item["extra"]["field_source"]["purchase_return_qty"] == "missing"


def test_reconciliation_pass_and_fail_no_blind_correction():
    from services.stock_reconciliation import reconcile_row

    printed = [
        "opening_qty",
        "purchase_qty",
        "sales_return_qty",
        "total_qty",
        "sales_qty",
        "purchase_return_qty",
        "closing_qty",
        "product_name",
    ]
    good = fields_to_line_item(
        {
            "product_name": "ARJUNA TAB",
            "opening_qty": "18",
            "purchase_qty": "60",
            "sales_return_qty": "1",
            "total_qty": "79",
            "sales_qty": "26",
            "purchase_return_qty": None,
            "closing_qty": "53",
        },
        printed_fields=printed,
        row_index=0,
    )
    good["extra"]["total_in_layout"] = True
    good["extra"]["column_map"] = [
        {"canonical": "total_qty", "header_text": "Total In Qty"}
    ]
    ok = reconcile_row(good)
    assert ok["valid"] is True

    bad = fields_to_line_item(
        {
            "product_name": "ARJUNA TAB",
            "opening_qty": "18",
            "purchase_qty": "60",
            "sales_return_qty": "1",
            "total_qty": "79",
            "sales_qty": "29",
            "purchase_return_qty": None,
            "closing_qty": "49",
        },
        printed_fields=printed,
        row_index=1,
    )
    bad["extra"]["total_in_layout"] = True
    bad["extra"]["column_map"] = [
        {"canonical": "total_qty", "header_text": "Total In Qty"}
    ]
    fail = reconcile_row(bad)
    assert fail["valid"] is False
    # Must preserve printed wrong closing — never correct to 53
    assert bad["closing_qty"] == 49.0
    assert bad["sales_qty"] == 29.0


def test_compare_cells_arjuna_expectations():
    printed = list(ARJUNA_EXPECTED.keys()) + ["product_name"]
    item = fields_to_line_item(
        {
            "product_name": "ARJUNA TAB",
            "opening_qty": "18",
            "purchase_qty": "60",
            "sales_return_qty": "1",
            "total_qty": "79",
            "sales_qty": "26",
            "purchase_return_qty": None,
            "closing_qty": "53",
        },
        printed_fields=printed,
        row_index=0,
    )
    report = compare_cells(
        "mistral",
        [item],
        {"ARJUNA TAB": ARJUNA_EXPECTED},
    )
    assert report["cell_accuracy_pct"] == 100.0
    assert report["rows_passed"] == 1


def test_compare_cells_bonnisan_expectations():
    printed = list(BONNISAN_DROPS_EXPECTED.keys()) + ["product_name"]
    item = fields_to_line_item(
        {
            "product_name": "BONNISAN DROPS",
            "opening_qty": None,
            "purchase_qty": "500",
            "sales_return_qty": "3",
            "total_qty": "503",
            "sales_qty": "318",
            "purchase_return_qty": None,
            "closing_qty": "185",
        },
        printed_fields=printed,
        row_index=0,
    )
    report = compare_cells(
        "mistral",
        [item],
        {"BONNISAN DROPS": BONNISAN_DROPS_EXPECTED},
    )
    assert report["incorrect_cells"] == 0


def test_column_shift_detection():
    item = {
        "opening_qty": 18.0,
        "receipts_qty": 0.0,
        "sales_qty": 26.0,
        "closing_qty": None,
        "extra": {
            "field_source": {
                "total_qty": "printed",
                "sales_return_qty": "printed",
            },
            "total_stock": 79.0,
            "sale_return": 79.0,  # Total parked in goods ret
        },
    }
    assert detect_column_shift(item) is True


def test_mistral_provider_missing_key():
    from services.stock_ocr_providers.mistral_ocr import MistralOCRProvider

    prov = MistralOCRProvider(api_key="")
    result = prov.extract_document(b"\x89PNG\r\n\x1a\n", filename="x.png")
    assert result.error == "MISTRAL_API_KEY_MISSING"


def test_normalize_provider_response_shape():
    from services.stock_ocr_providers.mistral_ocr import MistralOCRProvider

    class FakeDims:
        width = 100
        height = 200
        dpi = 72

    class FakeBlock:
        type = "text"
        content = "18"
        top_left_x = 1
        top_left_y = 2
        bottom_right_x = 3
        bottom_right_y = 4
        confidence_scores = None
        table_id = None

    class FakePage:
        index = 0
        markdown = "hello"
        images = []
        dimensions = FakeDims()
        tables = []
        blocks = [FakeBlock()]
        confidence_scores = None

    class FakeResp:
        pages = [FakePage()]
        model = "mistral-ocr-latest"
        usage_info = None

    pages = MistralOCRProvider(api_key="x")._normalize_pages(FakeResp())
    assert pages[0].blocks[0].text == "18"
    assert pages[0].blocks[0].bbox == (1.0, 2.0, 3.0, 4.0)
