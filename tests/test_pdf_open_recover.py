"""PDF uploads that PyMuPDF rejects with 'no objects found'."""

from __future__ import annotations

import base64
import os
import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _PDF_RECOVERED_BYTES,
    _open_sales_pdf,
    _rebuild_pdf_xref,
    extract_sales_statement,
)

XFRX = Path(
    "/var/www/html/splitextract/debug_uploads/9e19d7d4cb072e1f_REPORT_7KN15KQLB.PDF"
)


def _fitz():
    import fitz

    return fitz


class TestPdfOpenRecover(unittest.TestCase):
    def setUp(self):
        if not XFRX.is_file():
            self.skipTest("sample XFRX report is not on disk")
        self.raw = XFRX.read_bytes()

    def test_valid_pdf_opens_without_recovery(self):
        doc = _open_sales_pdf(_fitz(), self.raw, XFRX.name)
        try:
            self.assertGreaterEqual(doc.page_count, 1)
            self.assertIn("SURANA", doc[0].get_text())
        finally:
            doc.close()
        self.assertIsNone(_PDF_RECOVERED_BYTES.get())

    def test_base64_pdf_opens_and_extracts(self):
        encoded = base64.b64encode(self.raw)
        doc = _open_sales_pdf(_fitz(), encoded, "REPORT_7KE0RITTZ.PDF")
        try:
            self.assertIn("SURANA", doc[0].get_text())
        finally:
            doc.close()
        self.assertTrue(_PDF_RECOVERED_BYTES.get().startswith(b"%PDF"))

        previous = os.environ.get("ENABLE_GEMINI_EXTRACTION_FALLBACK")
        os.environ["ENABLE_GEMINI_EXTRACTION_FALLBACK"] = "false"
        try:
            direct = extract_sales_statement(self.raw, XFRX.name)
            wrapped = extract_sales_statement(encoded, "REPORT_7KE0RITTZ.PDF")
        finally:
            if previous is None:
                os.environ.pop("ENABLE_GEMINI_EXTRACTION_FALLBACK", None)
            else:
                os.environ["ENABLE_GEMINI_EXTRACTION_FALLBACK"] = previous
        self.assertEqual(
            len(wrapped.get("line_items") or []),
            len(direct.get("line_items") or []),
        )
        self.assertEqual(
            (wrapped.get("totals") or {}).get("extra", {}).get("extraction_method"),
            (direct.get("totals") or {}).get("extra", {}).get("extraction_method"),
        )

    def test_utf16_pdf_opens(self):
        encoded = self.raw.decode("latin1").encode("utf-16")
        doc = _open_sales_pdf(_fitz(), encoded, "report.pdf")
        try:
            self.assertIn("SURANA", doc[0].get_text())
        finally:
            doc.close()

    def test_header_only_is_value_error(self):
        with self.assertRaises(ValueError) as caught:
            _open_sales_pdf(_fitz(), b"%PDF-1.4\n%%EOF\n", "REPORT_7KE0RITTZ.PDF")
        self.assertIn("no objects found", str(caught.exception))

    def test_rebuilt_xref_keeps_page_text(self):
        cut = self.raw[: self.raw.rfind(b"xref")]
        rebuilt = _rebuild_pdf_xref(cut)
        self.assertIsNotNone(rebuilt)
        doc = _fitz().open(stream=rebuilt, filetype="pdf")
        try:
            self.assertIn("SURANA", doc[0].get_text())
        finally:
            doc.close()


if __name__ == "__main__":
    unittest.main()
