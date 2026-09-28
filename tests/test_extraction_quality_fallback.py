"""Existing parsers stay authoritative. Gemini runs only for a failed quality gate."""

import unittest
from unittest import mock

from services.extraction_quality import (
    assess_extraction_quality,
    assess_source_text_quality,
    evaluate_extraction_quality,
    quality_threshold,
)
from services.gemini_extraction_fallback import (
    _call_gemini,
    maybe_apply_gemini_fallback,
    try_gemini_vision_extract,
)


def _item(name="ABANA TAB", opening=10, receipts=0, sales=2, closing=8, sales_value=0):
    return {
        "product_name": name,
        "packing": "60'S",
        "opening_qty": opening,
        "receipts_qty": receipts,
        "sales_qty": sales,
        "closing_qty": closing,
        "sales_value": sales_value,
        "closing_value": 0,
    }


class QualityGateTests(unittest.TestCase):
    def test_named_parser_is_not_sent_to_gemini(self):
        result = {
            "line_items": [_item() for _ in range(6)],
            "totals": {
                "extra": {
                    "extraction_method": "marg_qty_value_dump_xls",
                    "stock_identity_fail_count": 6,
                    "stock_identity_kind": "opening_receipts_sales_closing",
                }
            },
        }
        quality = evaluate_extraction_quality(result)
        self.assertFalse(quality["should_fallback"])
        self.assertEqual(quality["quality"], "good")

    def test_quantity_only_zeros_are_valid(self):
        result = {
            "line_items": [
                _item(name=f"ROW {index}", opening=0, receipts=0, sales=0, closing=0)
                for index in range(8)
            ],
            "totals": {"extra": {"extraction_method": "gemini_vision"}},
        }
        quality = evaluate_extraction_quality(result)
        self.assertFalse(quality["should_fallback"])
        self.assertNotIn("missing_numeric_values", quality["reasons"])

    def test_empty_extract_requests_fallback(self):
        quality = evaluate_extraction_quality(
            {"line_items": [], "totals": {"extra": {"extraction_method": "gemini_vision"}}}
        )
        self.assertTrue(quality["should_fallback"])
        self.assertIn("empty_line_items", quality["reasons"])

    def test_ocr_garbage_requests_fallback(self):
        result = {
            "line_items": [_item(name="0pening"), _item(name="5a1es"), _item(name="C1osing")],
            "totals": {"extra": {"extraction_method": "gemini_vision"}},
        }
        quality = evaluate_extraction_quality(result)
        self.assertTrue(quality["should_fallback"])
        self.assertIn("high_ocr_noise", quality["reasons"])

    def test_closing_only_sheet_does_not_fail_identity(self):
        result = {
            "line_items": [
                _item(name=f"ROW {index}", opening=0, receipts=0, sales=0, closing=4)
                for index in range(10)
            ],
            "totals": {
                "extra": {
                    "extraction_method": "gemini_vision",
                    "stock_identity_kind": "opening_receipts_sales_closing",
                    "stock_identity_fail_count": 10,
                }
            },
        }
        quality = evaluate_extraction_quality(result)
        self.assertNotIn("stock_identity_failure", quality["reasons"])
        self.assertFalse(quality["should_fallback"])

    def test_repeated_rows_request_fallback(self):
        row = _item(name="ARJUNA TAB", opening=1, receipts=0, sales=1, closing=0)
        result = {
            "line_items": [dict(row) for _ in range(8)],
            "totals": {"extra": {"extraction_method": "gemini_vision"}},
        }
        quality = evaluate_extraction_quality(result)
        self.assertIn("duplicate_rows", quality["reasons"])
        self.assertEqual(quality["quality"], "poor")

    def test_named_parser_keeps_repeated_rows(self):
        row = _item(name="ARJUNA TAB")
        result = {
            "line_items": [dict(row) for _ in range(8)],
            "totals": {"extra": {"extraction_method": "marg_qty_value_dump_xls"}},
        }
        quality = evaluate_extraction_quality(result)
        self.assertFalse(quality["should_fallback"])

    def test_backwards_period_requests_fallback_for_generic_extract(self):
        result = {
            "period_from": "2026-09-01",
            "period_to": "2026-08-01",
            "line_items": [_item(name=f"ROW {index}") for index in range(4)],
            "totals": {"extra": {"extraction_method": "gemini_vision"}},
        }
        quality = evaluate_extraction_quality(result)
        self.assertIn("invalid_period", quality["reasons"])


class SourceTextQualityTests(unittest.TestCase):
    def test_empty_ocr_requests_fallback(self):
        quality = assess_source_text_quality("")
        self.assertTrue(quality["should_fallback"])
        self.assertIn("empty_ocr_text", quality["reasons"])
        self.assertEqual(quality["score"], 0)

    def test_garbage_ocr_requests_fallback(self):
        garbage = "@@@ ### $$$ %%%\n" * 20 + "!!!! **** ~~~~\n" * 10
        quality = assess_source_text_quality(garbage)
        self.assertTrue(quality["should_fallback"])
        self.assertTrue(
            set(quality["reasons"])
            & {"high_ocr_noise", "missing_stock_headers", "low_ocr_quality_score"}
        )

    def test_good_headers_are_trusted(self):
        text = (
            "Sales & Stock Statement\n"
            "PRODUCT NAME PACKING OPENING RECEIPT SALES CLOSING QTY VALUE\n"
            "ABANA TAB 60'S 10 0 2 8\n"
            "ARJUNA TAB 60'S 5 1 1 5\n"
            "BONNISAN DROPS 30ML 94 0 26 68\n"
        )
        quality = assess_source_text_quality(text)
        self.assertFalse(quality["should_fallback"])
        self.assertGreaterEqual(quality["score"], quality_threshold())

    def test_assess_extraction_quality_alias(self):
        result = {
            "line_items": [_item()],
            "totals": {"extra": {"extraction_method": "marg_qty_value_dump_xls"}},
        }
        self.assertEqual(
            assess_extraction_quality(result),
            evaluate_extraction_quality(result),
        )


class FallbackDispatchTests(unittest.TestCase):
    def test_good_result_does_not_call_gemini(self):
        result = {
            "line_items": [_item()],
            "totals": {"extra": {"extraction_method": "prompt_datewise_layout"}},
        }
        with mock.patch(
            "services.gemini_extraction_fallback._call_gemini"
        ) as caller:
            kept = maybe_apply_gemini_fallback(result, b"pdf-bytes", "a.pdf", ".pdf")
        caller.assert_not_called()
        self.assertEqual(kept["totals"]["extra"]["gemini_fallback"], "not_called")
        self.assertEqual(kept["line_items"][0]["opening_qty"], 10)

    def test_bad_result_accepts_a_valid_gemini_read(self):
        result = {
            "line_items": [],
            "totals": {"sales_value": None, "closing_value": None, "extra": {}},
        }
        parsed = {
            "stockist_name": "PALAK MEDICAL AGENCY",
            "period_from": "2026-08-01",
            "period_to": "2026-08-31",
            "line_items": [
                {
                    "product_name": "ARJUNA TAB",
                    "packing": "60'S",
                    "opening_qty": 27,
                    "receipts_qty": 0,
                    "sales_qty": 5,
                    "sales_value": 1238,
                    "closing_qty": 22,
                    "closing_value": 4854,
                }
            ],
        }
        with mock.patch(
            "services.gemini_extraction_fallback._call_gemini", return_value=parsed
        ) as caller, mock.patch(
            "services.gemini_extraction_fallback._document_parts",
            return_value=[{"text": "page"}],
        ):
            kept = maybe_apply_gemini_fallback(result, b"img", "a.jpg", ".jpg")
        caller.assert_called_once()
        self.assertEqual(kept["line_items"][0]["product_name"], "ARJUNA TAB")
        self.assertEqual(kept["line_items"][0]["sales_value"], 1238)
        self.assertEqual(kept["line_items"][0]["closing_value"], 4854)
        self.assertEqual(kept["totals"]["extra"]["gemini_fallback"], "success")

    def test_invalid_gemini_read_is_not_used(self):
        result = {
            "line_items": [],
            "totals": {"sales_value": None, "closing_value": None, "extra": {}},
        }
        parsed = {"line_items": [{"product_name": "0pening", "opening_qty": None}]}
        with mock.patch(
            "services.gemini_extraction_fallback._call_gemini", return_value=parsed
        ), mock.patch(
            "services.gemini_extraction_fallback._document_parts",
            return_value=[{"text": "page"}],
        ):
            kept = maybe_apply_gemini_fallback(result, b"img", "a.jpg", ".jpg")
        self.assertEqual(kept["line_items"], [])
        self.assertEqual(kept["totals"]["extra"]["gemini_fallback"], "validation_failed")
        self.assertTrue(kept["totals"]["extra"]["extraction_failed"])

    def test_fallback_uses_split_extract_quota_client(self):
        response = mock.Mock()
        response.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "{\"line_items\": []}"}]}}],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 4},
        }
        with mock.patch(
            "services.gemini_extraction_fallback._document_parts",
            return_value=[{"inline_data": {"mime_type": "image/png", "data": "abc"}}],
        ), mock.patch.dict(
            "sys.modules",
            {
                "app": mock.Mock(
                    call_gemini_with_quota=mock.Mock(return_value=response),
                    get_current_model_config=mock.Mock(
                        return_value={"name": "gemini-2.5-flash-lite", "timeout": 60, "max_output_tokens": 8192}
                    ),
                )
            },
        ):
            import app as app_module

            parsed = _call_gemini(b"img", ".jpg", [{"inline_data": {"mime_type": "image/png", "data": "abc"}}])
        app_module.call_gemini_with_quota.assert_called_once()
        self.assertEqual(
            app_module.call_gemini_with_quota.call_args.kwargs["request_type"],
            "vision",
        )
        self.assertEqual(
            app_module.call_gemini_with_quota.call_args.kwargs["model"],
            "gemini-2.5-flash-lite",
        )
        self.assertEqual(parsed, {"line_items": []})

    def test_txt_does_not_call_vision(self):
        result = {
            "line_items": [],
            "totals": {"sales_value": None, "closing_value": None, "extra": {}},
        }
        with mock.patch(
            "services.gemini_extraction_fallback._call_gemini"
        ) as caller:
            kept = maybe_apply_gemini_fallback(result, b"plain", "a.txt", ".txt")
        caller.assert_not_called()
        self.assertEqual(kept["totals"]["extra"]["gemini_fallback"], "unavailable_text_only")

    def test_early_vision_extract_validates_items(self):
        parsed = {
            "stockist_name": "TEST",
            "line_items": [
                {
                    "product_name": "ARJUNA TAB",
                    "opening_qty": 1,
                    "receipts_qty": 0,
                    "sales_qty": 1,
                    "closing_qty": 0,
                    "sales_value": 100,
                    "closing_value": 0,
                }
            ],
        }
        with mock.patch(
            "services.gemini_extraction_fallback._document_parts",
            return_value=[{"inline_data": {"mime_type": "image/png", "data": "x"}}],
        ), mock.patch(
            "services.gemini_extraction_fallback._call_gemini", return_value=parsed
        ):
            result = try_gemini_vision_extract(b"pdf", "scan.pdf", ".pdf")
        self.assertIsNotNone(result)
        self.assertEqual(result["line_items"][0]["product_name"], "ARJUNA TAB")
        self.assertEqual(result["totals"]["extra"]["gemini_fallback"], "early_success")


class EarlyImageOnlyPdfGateTests(unittest.TestCase):
    def test_text_pdf_skips_early_vision(self):
        from services.sales_statement_extractor import (
            _maybe_early_vision_for_image_only_pdf,
        )

        page = mock.Mock()
        page.get_text.return_value = "PRODUCT NAME " * 20 + "OPENING RECEIPT SALES CLOSING"
        doc = {0: page}
        doc_obj = mock.MagicMock()
        doc_obj.page_count = 1
        doc_obj.__getitem__.side_effect = doc.__getitem__
        with mock.patch(
            "services.gemini_extraction_fallback.try_gemini_vision_extract"
        ) as vision:
            out = _maybe_early_vision_for_image_only_pdf(
                doc_obj, b"%PDF", "texty.pdf", zoom=2.0
            )
        self.assertIsNone(out)
        vision.assert_not_called()

    def test_poor_ocr_image_pdf_calls_vision(self):
        from services.sales_statement_extractor import (
            _maybe_early_vision_for_image_only_pdf,
        )

        page = mock.Mock()
        page.get_text.return_value = ""
        doc_obj = mock.MagicMock()
        doc_obj.page_count = 1
        doc_obj.__getitem__.return_value = page
        vision_result = {
            "line_items": [_item()],
            "totals": {"extra": {}},
        }
        with mock.patch(
            "services.sales_statement_extractor._ocr_pdf_page_text",
            return_value=("@@@ ### !!!", b"img"),
        ), mock.patch(
            "services.gemini_extraction_fallback.try_gemini_vision_extract",
            return_value=vision_result,
        ) as vision:
            out = _maybe_early_vision_for_image_only_pdf(
                doc_obj, b"%PDF", "scan.pdf", zoom=2.0
            )
        vision.assert_called_once()
        self.assertEqual(out["line_items"][0]["product_name"], "ABANA TAB")
        self.assertIn("early_vision_reason", out["totals"]["extra"])

    def test_known_group_wise_ocr_keeps_parser_path(self):
        from services.sales_statement_extractor import (
            _maybe_early_vision_for_image_only_pdf,
        )

        page = mock.Mock()
        page.get_text.return_value = ""
        doc_obj = mock.MagicMock()
        doc_obj.page_count = 1
        doc_obj.__getitem__.return_value = page
        group_wise_ocr = (
            "Group Wise Sales\n"
            "PRODUCT Op.Stock Receipt Sales Cl.Stock\n"
            "ABANA TAB 10 0 2 8\n"
        )
        with mock.patch(
            "services.sales_statement_extractor._ocr_pdf_page_text",
            return_value=(group_wise_ocr, b"img"),
        ), mock.patch(
            "services.sales_statement_extractor._is_group_wise_sales_opstock_format",
            return_value=True,
        ), mock.patch(
            "services.gemini_extraction_fallback.try_gemini_vision_extract"
        ) as vision:
            out = _maybe_early_vision_for_image_only_pdf(
                doc_obj, b"%PDF", "group.pdf", zoom=2.0
            )
        self.assertIsNone(out)
        vision.assert_not_called()


if __name__ == "__main__":
    unittest.main()
