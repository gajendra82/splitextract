"""Product-wise photo reader: do not fold Free/Goods when qty columns close."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("PHASE2_RECOVERY_ENABLED", "false")

from services.sales_statement_extractor import (
    _parse_product_wise_stock_statement,
    _parse_product_wise_stock_statement_pdf,
    _parse_product_wise_stock_statement_photo_panel,
    empty_result,
    extract_sales_statement,
)
from services.stock_identity_profiles import issues_expected_closing

DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
DOC_0259 = DATASET / "0000700259_2026_08_ZL_18_379_03092026174312.jpg"
DOC_0325 = DATASET / "0000700325_2026_08_ZL_18_379_03092026175634.jpg"
DOC_0238 = DATASET / "0000700238_2026_08_ZL_18_377_03092026154026.pdf"
PASS_PHOTOS = [
    DATASET / "0000700206_2026_08_ZL_18_377_04092026042611.png",
    DATASET / "0000700221_2026_08_ZL_18_377_04092026042846.png",
    DATASET / "0000700345_2026_08_ZL_18_377_04092026043609.png",
]


def _word(x, y, text, w=36):
    return (x, y, x + w, y + 12, text)


_SAVED_GEMINI = {}


def _live_opt_in() -> bool:
    return os.environ.get("RUN_LIVE_EXTRACTION", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _block_gemini():
    import services.sales_extraction_runtime as runtime
    import services.vertex_gemini_client as vertex

    if "runtime" not in _SAVED_GEMINI:
        _SAVED_GEMINI["runtime"] = runtime.sales_generate_content_via_vertex
        _SAVED_GEMINI["vertex"] = vertex.generate_content_via_vertex

    def _blocked(*_a, **_k):
        raise RuntimeError("gemini blocked for phase 3.8 tests")

    runtime.sales_generate_content_via_vertex = _blocked
    vertex.generate_content_via_vertex = _blocked


def _restore_gemini():
    if "runtime" not in _SAVED_GEMINI:
        return
    import services.sales_extraction_runtime as runtime
    import services.vertex_gemini_client as vertex

    runtime.sales_generate_content_via_vertex = _SAVED_GEMINI.pop("runtime")
    vertex.generate_content_via_vertex = _SAVED_GEMINI.pop("vertex")


class TestPhotoFoldDecision(unittest.TestCase):
    def test_canonical_balance_keeps_free_and_goods_out_of_qty(self):
        header = [
            _word(80, 10, "Packing"),
            _word(160, 10, "Opening"),
            _word(230, 10, "Receipt"),
            _word(270, 10, "Qty"),
            _word(310, 10, "Free"),
            _word(350, 10, "Jul"),
            _word(390, 10, "Jun"),
            _word(450, 10, "Issues"),
            _word(490, 10, "Qty"),
            _word(530, 10, "Free"),
            _word(580, 10, "Goods"),
            _word(650, 10, "Closing"),
            _word(720, 10, "Liquidation"),
        ]
        # 10 + 110 - 20 = 100. Free 20 / Jul 26 / Issues Free 20 / Goods 6 stay aside.
        row = [
            _word(10, 40, "PILEX"),
            _word(50, 40, "FORTE"),
            _word(90, 40, "OINT"),
            _word(120, 40, "30GM"),
            _word(160, 40, "10"),
            _word(270, 40, "110"),
            _word(310, 40, "20"),
            _word(350, 40, "26"),
            _word(390, 40, "4"),
            _word(490, 40, "20"),
            _word(530, 40, "20"),
            _word(580, 40, "6"),
            _word(650, 40, "100"),
            _word(720, 40, "162"),
            _word(760, 40, "Days"),
        ]
        result = empty_result("fold.png", "png")
        items = _parse_product_wise_stock_statement_photo_panel(
            header + row, "", result, set()
        )
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["opening_qty"], 10.0)
        self.assertEqual(item["receipts_qty"], 110.0)
        self.assertEqual(item["sales_qty"], 20.0)
        self.assertEqual(item["closing_qty"], 100.0)
        self.assertNotEqual(item["receipts_qty"], 130.0)
        self.assertNotEqual(item["sales_qty"], 46.0)
        extra = item["extra"]
        self.assertEqual(extra.get("receipts_free"), 20.0)
        self.assertEqual(extra.get("sales_free"), 20.0)
        self.assertEqual(extra.get("goods_issue"), 6.0)
        self.assertEqual(extra.get("jul_qty"), 26.0)
        self.assertEqual(issues_expected_closing(item), 100.0)

    def test_fold_preserved_when_qty_columns_do_not_close(self):
        header = [
            _word(80, 10, "Packing"),
            _word(160, 10, "Opening"),
            _word(230, 10, "Receipt"),
            _word(270, 10, "Qty"),
            _word(310, 10, "Free"),
            _word(450, 10, "Issues"),
            _word(490, 10, "Qty"),
            _word(650, 10, "Closing"),
        ]
        # 5 + 0 - 0 = 5, but closing is 8. Free 3 is required by legacy fold: 5+3-0=8.
        row = [
            _word(10, 40, "SAMPLE"),
            _word(60, 40, "PROD"),
            _word(120, 40, "10S"),
            _word(160, 40, "5"),
            _word(310, 40, "3"),
            _word(650, 40, "8"),
        ]
        result = empty_result("fold2.png", "png")
        items = _parse_product_wise_stock_statement_photo_panel(
            header + row, "", result, set()
        )
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["receipts_qty"], 3.0)
        self.assertEqual(item["extra"].get("receipts_free"), 3.0)
        self.assertEqual(item["closing_qty"], 8.0)
        self.assertEqual(issues_expected_closing(item), 8.0)


@unittest.skipUnless(
    DOC_0259.is_file() and _live_opt_in(),
    "live 0259 photo is opt-in via RUN_LIVE_EXTRACTION=1",
)
class TestLive0259(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _block_gemini()
        cls.result = extract_sales_statement(DOC_0259.read_bytes(), DOC_0259.name)

    @classmethod
    def tearDownClass(cls):
        _restore_gemini()

    def test_method_and_pilex_forte(self):
        extra = (self.result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "product_wise_stock_statement_image")
        pilex = next(
            i
            for i in self.result["line_items"]
            if i["product_name"].startswith("PILEX FORTE")
        )
        self.assertEqual(pilex["opening_qty"], 10.0)
        self.assertEqual(pilex["receipts_qty"], 110.0)
        self.assertEqual(pilex["sales_qty"], 20.0)
        self.assertEqual(pilex["closing_qty"], 100.0)
        self.assertTrue(pilex["extra"].get("stock_identity_ok"))

    def test_clarina_canonical_issues(self):
        clarina = next(
            i
            for i in self.result["line_items"]
            if "CLARINA ANTIACNE" in (i["product_name"] or "").upper()
        )
        self.assertEqual(clarina["opening_qty"], 18.0)
        self.assertEqual(clarina["sales_qty"], 8.0)
        self.assertEqual(clarina["closing_qty"], 10.0)
        self.assertNotEqual(clarina["receipts_qty"], 2.0)
        self.assertTrue(clarina["extra"].get("stock_identity_ok"))


@unittest.skipUnless(
    DOC_0325.is_file() and _live_opt_in(),
    "live 0325 photo is opt-in via RUN_LIVE_EXTRACTION=1",
)
class TestLive0325(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _block_gemini()
        cls.result = extract_sales_statement(DOC_0325.read_bytes(), DOC_0325.name)

    @classmethod
    def tearDownClass(cls):
        _restore_gemini()

    def test_document_valid(self):
        extra = (self.result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "product_wise_stock_statement_image")
        self.assertTrue((extra.get("stock_validation") or {}).get("is_valid"))
        self.assertEqual(extra.get("stock_identity_fail_count"), 0)

    def test_aactaril_and_liv(self):
        aact = next(
            i for i in self.result["line_items"] if i["product_name"].startswith("AACTARIL")
        )
        self.assertEqual(aact["opening_qty"], 72.0)
        self.assertEqual(aact["sales_qty"], 21.0)
        self.assertEqual(aact["closing_qty"], 51.0)
        self.assertNotEqual(aact["receipts_qty"], 21.0)
        liv = next(
            i
            for i in self.result["line_items"]
            if i["product_name"].startswith("LIV SYP") and i["opening_qty"] == 403.0
        )
        self.assertEqual(liv["receipts_qty"], 420.0)
        self.assertEqual(liv["sales_qty"], 380.0)
        self.assertEqual(liv["closing_qty"], 443.0)


@unittest.skipUnless(
    all(p.is_file() for p in PASS_PHOTOS) and _live_opt_in(),
    "live pass photos are opt-in via RUN_LIVE_EXTRACTION=1",
)
class TestPassPhotoRegression(unittest.TestCase):
    def test_pass_photos_stay_valid(self):
        _block_gemini()
        try:
            self._assert_pass_photos()
        finally:
            _restore_gemini()

    def _assert_pass_photos(self):
        for path in PASS_PHOTOS:
            with self.subTest(path.name):
                result = extract_sales_statement(path.read_bytes(), path.name)
                extra = (result.get("totals") or {}).get("extra") or {}
                self.assertEqual(
                    extra.get("extraction_method"), "product_wise_stock_statement_image"
                )
                self.assertTrue((extra.get("stock_validation") or {}).get("is_valid"))
                self.assertEqual(len(result.get("line_items") or []), 35)


@unittest.skipUnless(DOC_0238.is_file(), "0238 fixture missing")
class Test0238Unchanged(unittest.TestCase):
    def test_free_pdf_not_claimed_by_photo_fix(self):
        import fitz

        doc = fitz.open(DOC_0238)
        self.assertIsNone(_parse_product_wise_stock_statement(doc, DOC_0238.name))
        parsed = _parse_product_wise_stock_statement_pdf(doc, DOC_0238.name)
        doc.close()
        diarex = next(
            i for i in parsed["line_items"] if i["product_name"].startswith("DIAREX TABLETS")
        )
        self.assertEqual(diarex["receipts_qty"], 48.0)
        self.assertEqual(diarex["extra"]["receipts_free"], 2.0)
        self.assertEqual(diarex["sales_qty"], 50.0)
