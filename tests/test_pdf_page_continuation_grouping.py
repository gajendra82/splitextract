"""Multi-page image PDFs must stay one statement when pages are continuations."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _detect_stockist_from_page_text,
    _group_pdf_pages_by_stockist,
    _page_text_looks_like_continuation,
    _stockist_keys_compatible,
)


def _page(index: int, text: str, stockist=None, *, words=None):
    return {
        "page_index": index,
        "text": text,
        "image_bytes": b"img",
        "stockist": stockist
        if stockist is not None
        else _detect_stockist_from_page_text(text),
        "words": words if words is not None else [],
    }


PAGE1 = """
SHRI RAM MEDICAL AGENCY
Sales & Stock Statement
From 01/08/2026 To 31/08/2026
PRODUCT NAME  PACKING  Op.Bal  Receipt  Issue  Closing
BONNISAN DROPS  30ML  10  50  20  40
"""

PAGE2_CONTINUED = """
Page No.2
Continued Page
PRODUCT NAME  PACKING  Op.Bal  Receipt  Issue  Closing
CONFIDO TABLETS  60'S  0  200  0  200
"""

PAGE2_HEADER_REPRINT = """
SHRI RAM MEDICAL AGENCY
PRODUCT NAME  PACKING  Op.Bal  Receipt  Issue  Closing
CONFIDO TABLETS  60'S  0  200  0  200
CLARINA CREAM  30G  5  10  2  13
"""

PAGE2_NOISY_STOCKIST = """
SHRI RAM MEDICAL AGENCV
HIMALAYA WELLNESS COMPANY
PRODUCT NAME  PACKING  Op.Bal  Receipt  Issue  Closing
CONFIDO TABLETS  60'S  0  200  0  200
"""

PAGE2_NEW_STOCKIST = """
NEW ERA PHARMACEUTICALS
Sales & Stock Statement
From 01/08/2026 To 31/08/2026
PRODUCT NAME  PACKING  Op.Bal  Receipt  Issue  Closing
LIV 52 SYRUP  100ML  1  2  1  2
"""


class TestPdfPageContinuationGrouping(unittest.TestCase):
    def test_continuation_hint_detected(self):
        self.assertTrue(_page_text_looks_like_continuation(PAGE2_CONTINUED))
        self.assertFalse(_page_text_looks_like_continuation(PAGE1))

    def test_page_no_hyphen_footer_continuation(self):
        """CONSOLIDATED PDFs print 'Page No.- 2' only in the footer band."""
        footer_page = (
            "Item\nPack\nOp.Qty\nOp.Val\nP.Qty\n"
            "PILEX KIT\nKIT\n0\n100\n10\n60\n"
            "VARDHMAN MEDISALES PRIVATE LIMITED\n"
            "Page No.- 2\n"
        )
        self.assertTrue(_page_text_looks_like_continuation(footer_page))

    def test_detect_stockist_skips_continuation_page(self):
        self.assertIsNotNone(_detect_stockist_from_page_text(PAGE1))
        self.assertIsNone(_detect_stockist_from_page_text(PAGE2_CONTINUED))

    def test_fuzzy_stockist_keys_compatible(self):
        self.assertTrue(
            _stockist_keys_compatible(
                "SHRI RAM MEDICAL AGENCY", "SHRI RAM MEDICAL AGENCV"
            )
        )
        self.assertFalse(
            _stockist_keys_compatible(
                "SHRI RAM MEDICAL AGENCY", "NEW ERA PHARMACEUTICALS"
            )
        )

    def test_image_only_pages_merge_as_one_statement(self):
        pages = [
            _page(0, PAGE1),
            _page(1, PAGE2_HEADER_REPRINT),
            _page(2, PAGE2_CONTINUED),
        ]
        groups = _group_pdf_pages_by_stockist(pages, image_only=True)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["pages"]), 3)
        self.assertIn("RAM", groups[0]["stockist_name"].upper())

    def test_image_only_noisy_header_stays_continuation(self):
        pages = [
            _page(0, PAGE1),
            _page(1, PAGE2_NOISY_STOCKIST),
        ]
        groups = _group_pdf_pages_by_stockist(pages, image_only=True)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["pages"]), 2)

    def test_image_only_real_new_stockist_still_splits(self):
        pages = [
            _page(0, PAGE1),
            _page(1, PAGE2_NEW_STOCKIST),
        ]
        groups = _group_pdf_pages_by_stockist(pages, image_only=True)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[0]["pages"]), 1)
        self.assertEqual(len(groups[1]["pages"]), 1)

    def test_text_pdf_exact_same_stockist_merges(self):
        pages = [
            _page(0, PAGE1, words=[(0, 0, 1, 1, "x")]),
            _page(1, PAGE2_HEADER_REPRINT, words=[(0, 0, 1, 1, "y")]),
        ]
        groups = _group_pdf_pages_by_stockist(pages, image_only=False)
        self.assertEqual(len(groups), 1)

    def test_orphan_placeholder_upgraded_by_page2_stockist(self):
        orphan = _page(0, "PRODUCT NAME\nBONNISAN 10 20 30", stockist=None)
        orphan["stockist"] = None
        page2 = _page(1, PAGE1)
        groups = _group_pdf_pages_by_stockist([orphan, page2], image_only=True)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["pages"]), 2)
        self.assertFalse(groups[0]["stockist_name"].startswith("Statement_"))


if __name__ == "__main__":
    unittest.main()
