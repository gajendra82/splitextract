"""Phase 1 router, evidence, schema, and validator tests."""

from __future__ import annotations

import io
import unittest

import fitz

from services.base_validator import validate_sales_result
from services.document_router import route_document
from services.evidence import (
    build_document_evidence,
    evidence_for_route,
    tokens_from_tesseract_data,
)
from services.sales_statement_extractor import empty_line_item
from services.schemas import (
    SalesLineItem,
    SchemaValidationError,
    normalize_sales_statement,
    parse_optional_number,
)


def _pdf(pages: list) -> bytes:
    doc = fitz.open()
    try:
        for text in pages:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text)
        return doc.tobytes()
    finally:
        doc.close()


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (12, 30), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _statement(items, **extra):
    totals_extra = {
        "extraction_method": "zl_secondary_xlsx",
        "stock_identity_fail_count": 0,
        "stock_validation": {"is_valid": True},
    }
    totals_extra.update(extra)
    return {
        "stockist_name": "ACME PHARMA",
        "period_from": "2026-08-01",
        "period_to": "2026-08-31",
        "line_items": items,
        "totals": {
            "sales_value": 10,
            "closing_value": 4,
            "extra": totals_extra,
        },
    }


class DocumentRouterTests(unittest.TestCase):
    def test_digital_pdf(self):
        text = "STOCK AND SALES STATEMENT\nProduct Opening Receipt Sales Closing\nAMOXICILLIN 10 0 2 8\n"
        route = route_document(
            _pdf([text, text]),
            "statement.pdf",
            requested_product="sales_statement",
        )
        self.assertEqual(route.document_type, "sales_statement")
        self.assertEqual(route.file_kind, "pdf")
        self.assertEqual(route.pdf_kind, "digital")
        self.assertFalse(route.requires_ocr)
        self.assertGreaterEqual(route.confidence, 0.9)

    def test_scanned_pdf(self):
        route = route_document(
            _pdf(["", ""]),
            "scan.pdf",
            requested_product="sales_statement",
        )
        self.assertEqual(route.pdf_kind, "scanned")
        self.assertTrue(route.requires_ocr)
        self.assertIn("embedded_text_below_threshold", route.reasons)

    def test_unknown_pdf(self):
        route = route_document(
            b"not-a-pdf",
            "broken.pdf",
            requested_product="sales_statement",
        )
        self.assertEqual(route.file_kind, "pdf")
        self.assertEqual(route.pdf_kind, "unknown")
        self.assertFalse(route.requires_ocr)

    def test_image_xlsx_xls_docx_txt_and_unsupported(self):
        image = route_document(_png(), "photo.png", requested_product="sales_statement")
        self.assertEqual(image.file_kind, "image")
        self.assertIsNone(image.pdf_kind)
        self.assertTrue(image.requires_ocr)

        xlsx = route_document(b"PK\x03\x04", "book.xlsx", requested_product="sales_statement")
        self.assertEqual(xlsx.file_kind, "xlsx")
        self.assertEqual(xlsx.document_type, "sales_statement")

        xls = route_document(b"\xd0\xcf\x11\xe0", "book.xls", requested_product="sales_statement")
        self.assertEqual(xls.file_kind, "xls")

        docx = route_document(b"PK\x03\x04", "note.docx", requested_product="sales_statement")
        self.assertEqual(docx.file_kind, "docx")
        self.assertEqual(docx.document_type, "sales_statement")

        text = route_document(b"STOCK", "note.txt", requested_product="sales_statement")
        self.assertEqual(text.file_kind, "txt")

        rejected = route_document(b"a,b\n", "sheet.csv", requested_product="sales_statement")
        self.assertEqual(rejected.document_type, "unsupported")
        self.assertEqual(rejected.file_kind, "unsupported")

        stock_csv = route_document(
            b"STOCK,SALES,OPENING,CLOSING\n1,2,3,4\n",
            "book.csv",
            requested_product="sales_statement",
        )
        self.assertEqual(stock_csv.document_type, "unsupported")
        self.assertEqual(stock_csv.file_kind, "unsupported")
        self.assertIn("csv_not_supported", stock_csv.reasons)
        self.assertNotIn("page_stats", stock_csv.to_dict())

    def test_invoice_lane_rejects_txt_and_accepts_pdf(self):
        invoice = route_document(
            _pdf(["INVOICE NO 12345 TOTAL AMOUNT 100.00 TAXABLE VALUE 90.00"]),
            "invoice.pdf",
            requested_product="invoice",
        )
        self.assertEqual(invoice.document_type, "invoice")
        self.assertEqual(invoice.pdf_kind, "digital")
        txt = route_document(b"hello", "note.txt", requested_product="invoice")
        self.assertEqual(txt.document_type, "unsupported")
        self.assertEqual(txt.file_kind, "txt")

    def test_same_pdf_follows_the_caller_product(self):
        payload = _pdf(
            ["STOCK AND SALES STATEMENT OPENING RECEIPTS SALES CLOSING AMOXICILLIN 10"]
        )
        sales = route_document(payload, "statement.pdf", requested_product="sales_statement")
        invoice = route_document(payload, "statement.pdf", requested_product="invoice")
        unspecified = route_document(payload, "statement.pdf")
        self.assertEqual(sales.document_type, "sales_statement")
        self.assertEqual(invoice.document_type, "invoice")
        self.assertEqual(unspecified.document_type, "unsupported")
        self.assertIn("lane_not_specified", unspecified.reasons)

    def test_supported_image_excel_and_document_kinds(self):
        samples = [
            (b"\xff\xd8\xff", "photo.jpg", "image", "sales_statement"),
            (b"\xff\xd8\xff", "photo.jpeg", "image", "sales_statement"),
            (b"II*\x00", "scan.tif", "image", "sales_statement"),
            (b"MM\x00*", "scan.tiff", "image", "sales_statement"),
            (b"BM", "scan.bmp", "image", "sales_statement"),
            (b"RIFF\x00\x00\x00\x00WEBP", "scan.webp", "image", "sales_statement"),
            (b"\xd0\xcf\x11\xe0", "book.xls", "xls", "sales_statement"),
            (b"PK\x03\x04", "book.xlsx", "xlsx", "sales_statement"),
            (b"PK\x03\x04", "book.xlsm", "xlsx", "sales_statement"),
            (b"\xd0\xcf\x11\xe0", "note.doc", "doc", "sales_statement"),
            (b"PK\x03\x04", "note.docx", "docx", "sales_statement"),
            (b"<table>", "note.html", "html", "sales_statement"),
            (b"<table>", "note.htm", "html", "sales_statement"),
            (b"STOCK", "note.txt", "txt", "sales_statement"),
        ]
        for payload, name, kind, product in samples:
            route = route_document(payload, name, requested_product=product)
            self.assertEqual(route.file_kind, kind, name)
            self.assertEqual(route.document_type, product, name)
            if kind == "image":
                self.assertTrue(route.requires_ocr, name)
        xlsm = route_document(b"PK\x03\x04", "book.xlsm", requested_product="invoice")
        self.assertEqual(xlsm.document_type, "invoice")
        self.assertEqual(xlsm.file_kind, "xlsx")
        self.assertIn("xlsm_treated_as_xlsx", xlsm.reasons)


class EvidenceTests(unittest.TestCase):
    def test_pdf_pages_tokens_and_bbox_without_image_bytes(self):
        payload = _pdf(
            [
                "PAGE ONE STOCK STATEMENT AMOXICILLIN OPENING 10",
                "PAGE TWO STOCK STATEMENT PARACETAMOL CLOSING 4",
            ]
        )
        route = route_document(payload, "two.pdf", requested_product="sales_statement")
        evidence = build_document_evidence(payload, "two.pdf", route)
        self.assertEqual(evidence.source_type, "pdf")
        self.assertEqual(evidence.extraction_engine, "pymupdf")
        self.assertEqual(len(evidence.pages), 2)
        self.assertEqual([page.page_number for page in evidence.pages], [1, 2])
        self.assertGreater(evidence.pages[0].width or 0, 0)
        self.assertTrue(evidence.pages[0].tokens)
        token = evidence.pages[0].tokens[0]
        self.assertEqual(token.page_number, 1)
        self.assertEqual(token.source_engine, "pymupdf")
        self.assertIsNotNone(token.bbox)
        self.assertLess(token.bbox.x0, token.bbox.x1)
        summary = evidence.summary()
        self.assertNotIn("tokens", summary["pages"][0])
        self.assertNotIn("text", summary["pages"][0])
        self.assertNotIn("image_bytes", summary["pages"][0])
        self.assertGreater(summary["pages"][0]["text_length"], 0)
        self.assertGreater(summary["pages"][0]["token_count"], 0)

    def test_ocr_token_confidence_and_bbox(self):
        tokens = tokens_from_tesseract_data(
            {
                "text": ["OPENING", "", "ZERO"],
                "conf": ["87.5", "-1", "0"],
                "left": [10, 0, 1],
                "top": [20, 0, 2],
                "width": [40, 1, 3],
                "height": [12, 1, 4],
            },
            page_number=2,
        )
        self.assertEqual([token.text for token in tokens], ["OPENING", "ZERO"])
        self.assertEqual(tokens[0].text, "OPENING")
        self.assertEqual(tokens[0].confidence, 87.5)
        self.assertEqual(tokens[1].confidence, 0.0)
        self.assertEqual(tokens[0].page_number, 2)
        self.assertEqual(tokens[0].bbox.x1, 50)
        self.assertEqual(tokens[0].source_engine, "tesseract")

    def test_api_summary_reuses_page_stats_without_token_or_image_bytes(self):
        payload = _pdf(["PAGE ONE STOCK STATEMENT AMOXICILLIN OPENING 10"])
        route = route_document(
            payload,
            "one.pdf",
            requested_product="sales_statement",
            collect_page_stats=True,
        )
        evidence = evidence_for_route(
            payload,
            "one.pdf",
            route,
            retain_tokens=False,
            retain_text=False,
        )
        self.assertEqual(evidence.pages[0].tokens, [])
        self.assertEqual(evidence.pages[0].text, "")
        self.assertIsNone(evidence.pages[0].image_ref)
        summary = evidence.summary()
        self.assertGreater(summary["pages"][0]["text_length"], 0)
        self.assertGreater(summary["pages"][0]["token_count"], 0)
        self.assertNotIn("image_bytes", summary["pages"][0])
        self.assertNotIn("tokens", summary["pages"][0])
        self.assertFalse(summary["metadata"]["image_bytes_stored"])

        image_route = route_document(_png(), "photo.png", requested_product="sales_statement")
        image_evidence = evidence_for_route(_png(), "photo.png", image_route)
        self.assertEqual(image_evidence.pages[0].image_ref, "upload")
        self.assertFalse(image_evidence.pages[0].metadata["image_bytes_stored"])
        self.assertNotIn("image_bytes", image_evidence.summary()["pages"][0])


class SchemaTests(unittest.TestCase):
    def test_missing_is_null_and_explicit_zero_stays_zero(self):
        self.assertIsNone(parse_optional_number(None, field="opening_qty"))
        self.assertIsNone(parse_optional_number("", field="opening_qty"))
        self.assertIsNone(parse_optional_number("NA", field="opening_qty"))
        self.assertIsNone(parse_optional_number("-", field="opening_qty"))
        self.assertEqual(parse_optional_number(0, field="opening_qty"), 0.0)
        self.assertEqual(parse_optional_number(0.0, field="opening_qty"), 0.0)
        self.assertEqual(parse_optional_number("0", field="opening_qty"), 0.0)
        self.assertEqual(parse_optional_number("100", field="sales_qty"), 100.0)
        self.assertEqual(parse_optional_number("12.5", field="sales_qty"), 12.5)

    def test_invalid_number_raises(self):
        with self.assertRaises(SchemaValidationError):
            SalesLineItem.from_mapping(
                {"product_name": "AMOXICILLIN", "opening_qty": "not-a-number"}
            )

    def test_line_item_structure_and_projection_does_not_mutate_parser_output(self):
        raw = empty_line_item()
        raw["product_name"] = "AMOXICILLIN"
        raw["opening_qty"] = 0
        original_qty = raw["opening_qty"]
        statement = _statement([raw])
        projected = normalize_sales_statement(statement)
        item = projected["line_items"][0]
        self.assertEqual(
            set(item),
            {
                "product_name",
                "product_code",
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "closing_qty",
                "opening_value",
                "receipts_value",
                "sales_value",
                "closing_value",
                "dynamic",
            },
        )
        self.assertEqual(item["dynamic"], {})
        self.assertEqual(item["opening_qty"], 0.0)
        self.assertIsNone(item["opening_value"])
        self.assertEqual(raw["opening_qty"], original_qty)
        self.assertNotIn("opening_value", raw)
        self.assertEqual(statement["line_items"][0]["receipts_qty"], 0.0)

    def test_invalid_token_becomes_a_schema_error_not_zero(self):
        raw = {
            "product_name": "AMOXICILLIN",
            "opening_qty": "not-a-number",
            "sales_qty": "NA",
            "closing_qty": "",
            "receipts_qty": "100",
        }
        statement = _statement([raw])
        projected = normalize_sales_statement(statement)
        item = projected["line_items"][0]
        self.assertIsNone(item["opening_qty"])
        self.assertIsNone(item["sales_qty"])
        self.assertIsNone(item["closing_qty"])
        self.assertEqual(item["receipts_qty"], 100.0)
        self.assertTrue(projected["metadata"]["schema_errors"])
        self.assertEqual(raw["opening_qty"], "not-a-number")
        self.assertFalse(any(value == 0 for value in (
            item["opening_qty"],
            item["sales_qty"],
            item["closing_qty"],
        )))


class ValidatorTests(unittest.TestCase):
    def test_valid_named_parser_is_accepted(self):
        result = _statement(
            [
                {
                    "product_name": "AMOXICILLIN",
                    "opening_qty": 10,
                    "receipts_qty": 0,
                    "sales_qty": 2,
                    "closing_qty": 8,
                }
            ]
        )
        decision = validate_sales_result(result)
        self.assertTrue(decision["accepted"])
        self.assertEqual(decision["status"], "pass")
        self.assertTrue(all(decision["checks"].values()))

    def test_empty_result_is_rejected(self):
        decision = validate_sales_result(_statement([]))
        self.assertFalse(decision["accepted"])
        self.assertEqual(decision["status"], "unresolved")
        self.assertIn("empty_line_items", decision["reasons"])
        self.assertFalse(decision["checks"]["rows"])

    def test_extraction_failed_is_rejected(self):
        result = _statement(
            [{"product_name": "AMOXICILLIN", "opening_qty": 1, "closing_qty": 1}]
        )
        result["totals"]["extra"]["extraction_failed"] = True
        decision = validate_sales_result(result)
        self.assertFalse(decision["accepted"])
        self.assertIn("extraction_failed", decision["reasons"])
        self.assertFalse(decision["checks"]["quality"])

    def test_stock_identity_failure_is_rejected_for_named_parser(self):
        result = _statement(
            [
                {
                    "product_name": "AMOXICILLIN",
                    "opening_qty": 10,
                    "receipts_qty": 0,
                    "sales_qty": 0,
                    "closing_qty": 99,
                }
            ],
            stock_identity_fail_count=1,
            stock_validation={"is_valid": False},
        )
        decision = validate_sales_result(result)
        self.assertFalse(decision["accepted"])
        self.assertIn("stock_identity_failure", decision["reasons"])
        self.assertFalse(decision["checks"]["stock_identity"])
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "zl_secondary_xlsx",
        )

    def test_duplicate_heavy_named_parser_is_rejected(self):
        row = {
            "product_name": "AMOXICILLIN",
            "opening_qty": 4,
            "receipts_qty": 1,
            "sales_qty": 1,
            "closing_qty": 4,
        }
        decision = validate_sales_result(_statement([dict(row) for _ in range(6)]))
        self.assertFalse(decision["accepted"])
        self.assertIn("duplicate_rows", decision["reasons"])
        self.assertFalse(decision["checks"]["duplicates"])

    def test_identity_percent_above_threshold_rejects_named_parser(self):
        items = [
            {
                "product_name": f"PRODUCT {index} TABLET",
                "opening_qty": 1,
                "receipts_qty": 0,
                "sales_qty": 0,
                "closing_qty": 1,
            }
            for index in range(10)
        ]
        above = _statement(
            items,
            stock_identity_fail_count=3,
            stock_validation={"is_valid": True},
        )
        decision = validate_sales_result(above)
        self.assertFalse(decision["accepted"])
        self.assertEqual(decision["status"], "unresolved")
        self.assertIn("stock_identity_failure", decision["reasons"])
        self.assertFalse(decision["checks"]["stock_identity"])
        self.assertNotIn("extraction_failed", above["totals"]["extra"])

        below = _statement(
            [dict(item) for item in items],
            stock_identity_fail_count=1,
            stock_validation={"is_valid": True},
        )
        self.assertTrue(validate_sales_result(below)["accepted"])

    def test_validator_does_not_mutate_parser_fields(self):
        import copy

        result = _statement(
            [
                {
                    "product_name": "AMOXICILLIN",
                    "opening_qty": 10,
                    "receipts_qty": 0,
                    "sales_qty": 2,
                    "closing_qty": 8,
                }
            ]
        )
        snapshot = copy.deepcopy(result)
        decision = validate_sales_result(result)
        self.assertTrue(decision["accepted"])
        self.assertEqual(result["line_items"], snapshot["line_items"])
        self.assertEqual(result["totals"], snapshot["totals"])
        self.assertNotIn("extraction_failed", result["totals"]["extra"])


class ParserProjectionTests(unittest.TestCase):
    def test_txt_parser_numbers_stay_and_phase1_is_additive(self):
        from services.sales_statement_extractor import extract_sales_statement

        text = """
NARAYAN MEDICAL (NEW)                                                02/09/2026
BHANDERHATI, HOOGHLY                                                 17:26:01
** Daily Stock Summary (Company-wise) **          From 01/08/2026 to 31/08/2026
Company:HIMALAYA ZEUS [M177]
        Product & Pack        |  Op. | Pur. |Sales|Sales| Adj./| Cl.  | Order |
                              | Stock| Qty. |Retu.| Qty.| Dmg. |Stock | Qty.  |
AACTARIL SOAP,75GM                 62      0     0    10      0     52     -52
CLARINA ANTI ACNE FACEWAS,60ML     51      0     0     9     -1     41     -41
HIORA K TOOTH PASTE,100GM           0     50     0    10      0     40     -40
"""
        result = extract_sales_statement(text.encode("utf-8"), "narayan.txt")
        soap = result["line_items"][0]
        self.assertEqual(soap["product_name"], "AACTARIL SOAP")
        self.assertEqual(soap["opening_qty"], 62)
        self.assertEqual(soap["sales_qty"], 10)
        self.assertEqual(soap["closing_qty"], 52)
        self.assertEqual(result["stockist_name"], "NARAYAN MEDICAL (NEW)")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        phase1 = result["phase1"]
        self.assertEqual(phase1["route"]["document_type"], "sales_statement")
        self.assertEqual(phase1["route"]["file_kind"], "txt")
        self.assertNotIn("page_stats", phase1["route"])
        projected = phase1["normalized"]["line_items"][0]
        self.assertIsNot(projected, soap)
        self.assertEqual(projected["opening_qty"], 62.0)
        self.assertEqual(projected["receipts_qty"], 0.0)
        self.assertNotIn("extraction_failed", (result.get("totals") or {}).get("extra") or {})
        self.assertIn(phase1["validation"]["status"], {"pass", "unresolved"})


if __name__ == "__main__":
    unittest.main()
