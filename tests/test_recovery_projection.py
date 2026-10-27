"""A canonical recovery value is kept only when its printed header is present."""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from services.base_validator import validate_sales_result
from services.recovery_projection import project_recovery_candidate
from services.recovery_runner import apply_phase2_recovery, merge_stock_recovery
from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    empty_line_item,
)
from services.stock_identity_profiles import select_profile

BAL_MAPPING = {"Bal": "closing_qty"}
QTY_MAPPING = {
    "Opening": "opening_qty",
    "Purchase": "receipts_qty",
    "Sales": "sales_qty",
    "Closing": "closing_qty",
}


def _item(name, opening, receipts, sales, closing, **extra):
    item = empty_line_item()
    item["product_name"] = name
    item["opening_qty"] = opening
    item["receipts_qty"] = receipts
    item["sales_qty"] = sales
    item["closing_qty"] = closing
    item["extra"] = dict(extra)
    return item


def _statement(items, method="pharma_hub_jun_jul_photo", layout="pharma_hub_jun_jul", mapping=None, kind="opening_receipts_sales_closing"):
    extra = {
        "extraction_method": method,
        "layout": layout,
        "stock_identity_kind": kind,
    }
    if mapping is not None:
        extra["column_mapping"] = mapping
    statement = _apply_stock_identity_validation(
        {"line_items": items, "totals": {"extra": extra}}
    )
    statement["phase1"] = {"route": {}, "validation": validate_sales_result(statement)}
    return statement


def _fixed(projected):
    return projected["line_items"][0]["fixed"]


def _sales(product, **fixed):
    body = {
        "product_name": product,
        "opening_qty": None,
        "receipts_qty": None,
        "sales_qty": None,
        "closing_qty": None,
    }
    body.update(fixed)
    return {
        "line_items": [{"fixed": body, "dynamic": {}}],
        "dynamic": {},
    }


class ProjectionGuardTests(unittest.TestCase):
    def test_contradictory_closing_without_source_is_not_promoted(self):
        projected = project_recovery_candidate(
            {"product_name": "ROW", "closing_qty": 43},
            BAL_MAPPING,
            evidence_headers=["Bal"],
        )
        self.assertIsNone(_fixed(projected)["closing_qty"])
        merged = merge_stock_recovery(
            _statement([_item("ROW", 47, 0, 4, 45)]),
            _parsed(projected),
        )
        self.assertEqual(merged["line_items"][0]["closing_qty"], 45)

    def test_printed_source_wins_over_contradictory_canonical(self):
        projected = project_recovery_candidate(
            {"product_name": "ROW", "Bal": 45, "closing_qty": 43},
            BAL_MAPPING,
            evidence_headers=["Bal"],
        )
        self.assertEqual(_fixed(projected)["closing_qty"], 45.0)
        self.assertNotEqual(_fixed(projected)["closing_qty"], 43)

    def test_bare_canonical_image_value_is_rejected(self):
        projected = project_recovery_candidate(
            {"product_name": "ROW", "closing_qty": 43},
            {"Closing": "closing_qty"},
            evidence_headers=["Closing"],
        )
        self.assertIsNone(_fixed(projected)["closing_qty"])

    def test_pdf_text_recovery_rejects_a_calculated_closing(self):
        recovered = _run_text("statement.pdf", _pdf(_printed_lines()))
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 45)
        self.assertEqual(recovered["phase2"]["status"], "unresolved")

    def test_excel_recovery_rejects_a_calculated_closing(self):
        recovered = _run_text("statement.xlsx", _xlsx())
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 45)
        self.assertEqual(recovered["phase2"]["status"], "unresolved")

    def test_fenced_sales_json_does_not_promote_a_calculated_closing(self):
        statement = _statement([_item("ROW", 47, 0, 4, 45)])
        fenced = "```json\n" + json.dumps(
            _sales("ROW", opening_qty=47, receipts_qty=0, sales_qty=4, closing_qty=43)
        ) + "\n```"

        def caller(_payload):
            return fenced

        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=caller,
            file_bytes=b"not-an-image",
            filename="photo.jpeg",
        )
        row = recovered["line_items"][0]
        self.assertEqual(
            (row["opening_qty"], row["receipts_qty"], row["sales_qty"], row["closing_qty"]),
            (47, 0, 4, 45),
        )
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertFalse(recovered["phase1"]["validation"]["accepted"])

    def test_fenced_semantic_row_uses_the_printed_header(self):
        statement = _statement([_item("ROW", 47, 0, 4, 45)])
        from services.stock_row_crops import StockRowCrop

        fenced = "```json\n" + json.dumps(
            {
                "product_name": "ROW",
                "opening_qty": 47,
                "receipts_qty": 0,
                "sales_qty": 4,
                "closing_qty": 99,
                "Opening": 47,
                "Purchase": 0,
                "Sales": 4,
                "Closing": 43,
            }
        ) + "\n```"

        def caller(_payload):
            return fenced

        with patch(
            "services.stock_row_crops.locate_row_crops",
            return_value=[
                StockRowCrop("ROW", b"CROP", 20, 10, True, False, header_labels=tuple(QTY_MAPPING))
            ],
        ):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg",
                filename="photo.jpeg",
            )
        row = recovered["line_items"][0]
        self.assertEqual(row["closing_qty"], 43)
        self.assertNotEqual(row["closing_qty"], 99)
        self.assertEqual(recovered["phase2"]["status"], "recovered")

    def test_full_document_without_mapping_does_not_promote_canonicals(self):
        statement = _statement(
            [_item("ROW", 47, 0, 4, 45)],
            method="swil_erp",
            layout="",
        )

        def caller(_payload):
            return json.dumps(_sales("ROW", opening_qty=47, receipts_qty=0, sales_qty=4, closing_qty=43))

        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=caller,
            file_bytes=b"not-an-image",
            filename="photo.jpeg",
        )
        row = recovered["line_items"][0]
        self.assertEqual((row["opening_qty"], row["receipts_qty"], row["sales_qty"], row["closing_qty"]), (47, 0, 4, 45))
        self.assertEqual(recovered["phase2"]["status"], "unresolved")

    def test_partial_mapping_drops_the_unmapped_closing(self):
        projected = project_recovery_candidate(
            {
                "product_name": "ROW",
                "Opening": 47,
                "Sales": 4,
                "opening_qty": 47,
                "sales_qty": 4,
                "closing_qty": 45,
            },
            {"Opening": "opening_qty", "Sales": "sales_qty"},
            evidence_headers=["Opening", "Sales"],
        )
        fixed = _fixed(projected)
        self.assertEqual(fixed["opening_qty"], 47.0)
        self.assertEqual(fixed["sales_qty"], 4.0)
        self.assertIsNone(fixed["closing_qty"])

    def test_printed_zero_is_promoted(self):
        projected = project_recovery_candidate(
            {"product_name": "ROW", "Pur": 0, "receipts_qty": None},
            {"Pur": "receipts_qty"},
            evidence_headers=["Pur"],
        )
        self.assertEqual(_fixed(projected)["receipts_qty"], 0.0)

    def test_blank_printed_value_is_not_promoted(self):
        for blank in (None, "", "-"):
            projected = project_recovery_candidate(
                {"product_name": "ROW", "Pur": blank, "receipts_qty": 9},
                {"Pur": "receipts_qty"},
                evidence_headers=["Pur"],
            )
            self.assertIsNone(_fixed(projected)["receipts_qty"], blank)

    def test_existing_quantity_survives_a_null_recovery(self):
        statement = _statement([_item("ROW", 47, 0, 4, 45)])
        merged = merge_stock_recovery(statement, _parsed(_sales("ROW", closing_qty=None)))
        self.assertEqual(merged["line_items"][0]["closing_qty"], 45)

    def test_existing_dynamic_key_is_not_overwritten(self):
        statement = _statement([_item("ROW", 47, 0, 4, 45, batch="ABC")])
        body = _sales("ROW")
        body["line_items"][0]["dynamic"] = {"batch": "XYZ"}
        merged = merge_stock_recovery(statement, _parsed(body))
        self.assertEqual(merged["line_items"][0]["extra"]["batch"], "ABC")

    def test_new_dynamic_key_is_added(self):
        statement = _statement([_item("ROW", 47, 0, 4, 45)])
        body = _sales("ROW")
        body["line_items"][0]["dynamic"] = {"Special Adj": 7}
        merged = merge_stock_recovery(statement, _parsed(body))
        self.assertEqual(merged["line_items"][0]["extra"]["Special Adj"], 7)

    def test_existing_zero_survives_a_null_recovery(self):
        statement = _statement([_item("ROW", 47, 0, 4, 45)])
        merged = merge_stock_recovery(statement, _parsed(_sales("ROW", receipts_qty=None)))
        self.assertEqual(merged["line_items"][0]["receipts_qty"], 0)

    def test_unknown_profile_discards_canonical_recovery(self):
        statement = _statement(
            [_item("ROW", 47, 0, 4, 45)],
            method="unseen_customer_layout",
            layout="custom",
            kind="unknown",
        )
        self.assertEqual(select_profile(statement).column_mappings, ())
        calls = []

        def caller(_payload):
            calls.append(1)
            return json.dumps(_sales("ROW", closing_qty=43))

        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=caller,
            filename="statement.pdf",
        )
        self.assertEqual(calls, [])
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 45)
        self.assertEqual(recovered["phase2"]["attempts"], 0)

    def test_global_excel_alias_is_not_trusted(self):
        statement = _statement(
            [_item("ROW", 47, 0, 4, 45)],
            method="swil_erp",
            layout="",
            mapping={"opening_qty": {"header": "Op", "column": "A", "confidence": 1}},
        )
        labels = {item.printed_header: item.canonical_field for item in select_profile(statement).column_mappings}
        self.assertNotIn("Op", labels)

    def test_profile_owned_excel_mapping_is_trusted(self):
        statement = _statement(
            [_item("ROW", 47, 0, 4, 45)],
            method="pack_op_pur_bal_stock_sale_vision",
            layout="pack_op_pur_bal_stock_sale",
            mapping={"opening_qty": {"header": "Op", "column": "A", "confidence": 1}},
        )
        labels = {item.printed_header: item.canonical_field for item in select_profile(statement).column_mappings}
        self.assertEqual(labels["Op"], "opening_qty")

    def test_mapping_does_not_leak_across_profiles(self):
        owned = _statement(
            [_item("ROW", 47, 0, 4, 45)],
            method="pack_op_pur_bal_stock_sale_vision",
            layout="pack_op_pur_bal_stock_sale",
        )
        other = _statement(
            [_item("ROW", 47, 0, 4, 45)],
            method="pharma_hub_jun_jul_photo",
            layout="pharma_hub_jun_jul",
        )
        pack = {item.printed_header for item in select_profile(owned).column_mappings}
        pharma = {item.printed_header for item in select_profile(other).column_mappings}
        self.assertIn("Op", pack)
        self.assertNotIn("Op", pharma)

    def test_identity_validation_does_not_replace_a_printed_closing(self):
        statement = _statement([_item("ROW", 47, 0, 4, 45)])
        from services.stock_row_crops import StockRowCrop

        def caller(_payload):
            return json.dumps(
                {
                    "product_name": "ROW",
                    "Opening": 47,
                    "Purchase": 0,
                    "Sales": 4,
                    "Closing": 45,
                    "closing_qty": 43,
                }
            )

        with patch(
            "services.stock_row_crops.locate_row_crops",
            return_value=[StockRowCrop("ROW", b"CROP", 20, 10, True, False, header_labels=tuple(QTY_MAPPING))],
        ):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg",
                filename="photo.jpeg",
            )
        row = recovered["line_items"][0]
        self.assertEqual(row["closing_qty"], 45)
        self.assertEqual(row["extra"]["expected_closing"], 43.0)
        self.assertFalse(row["extra"]["stock_identity_ok"])
        self.assertEqual(recovered["phase2"]["status"], "unresolved")

    def test_calculated_closing_is_never_invented(self):
        projected = project_recovery_candidate(
            {
                "product_name": "ROW",
                "Opening": 47,
                "Purchase": 0,
                "Sales": 4,
                "closing_qty": 43,
            },
            QTY_MAPPING,
            evidence_headers=["Opening", "Purchase", "Sales"],
        )
        self.assertIsNone(_fixed(projected)["closing_qty"])
        self.assertEqual(_fixed(projected)["opening_qty"], 47.0)

    def test_values_do_not_cross_between_rows(self):
        first = project_recovery_candidate(
            {"product_name": "ROW A", "Bal": 45, "closing_qty": 43},
            BAL_MAPPING,
            evidence_headers=["Bal"],
        )
        second = project_recovery_candidate(
            {"product_name": "ROW B", "Closing": 12, "closing_qty": 99},
            {"Closing": "closing_qty"},
            evidence_headers=["Closing"],
        )
        self.assertEqual(_fixed(first)["closing_qty"], 45.0)
        self.assertEqual(_fixed(second)["closing_qty"], 12.0)
        document = project_recovery_candidate(
            {
                "line_items": [
                    {"fixed": {"product_name": "ROW A", "closing_qty": 43}, "dynamic": {"Bal": 45}},
                    {"fixed": {"product_name": "ROW B", "closing_qty": 7}, "dynamic": {}},
                ]
            },
            BAL_MAPPING,
            evidence_headers=["Bal"],
        )
        closings = [item["fixed"]["closing_qty"] for item in document["line_items"]]
        self.assertEqual(closings, [45.0, None])


def _parsed(body):
    from services.hybrid_schema import SalesExtractionOutput

    return SalesExtractionOutput.model_validate(body)


def _printed_lines():
    return [
        "Opening | Purchase | Sales | Closing",
        "ROW | 47 | 0 | 4 | 45",
    ]


def _pdf(lines):
    import fitz

    document = fitz.open()
    page = document.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 18
    data = document.tobytes()
    document.close()
    return data


def _xlsx():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Opening", "Purchase", "Sales", "Closing"])
    sheet.append(["ROW", 47, 0, 4, 45])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _run_text(filename, file_bytes):
    statement = _statement([_item("ROW", 47, 0, 4, 45)])

    def caller(payload):
        schema = (payload.get("generationConfig") or {}).get("responseJsonSchema") or {}
        prompt = payload["contents"][0]["parts"][0]["text"]
        if schema.get("title") == "SemanticRow":
            raise AssertionError("text recovery must not use the image schema")
        if "Opening = opening_qty" not in prompt or "Closing = closing_qty" not in prompt:
            raise AssertionError("text recovery did not receive the trusted mapping")
        return json.dumps(
            _sales("ROW", opening_qty=47, receipts_qty=0, sales_qty=4, closing_qty=43)
        )

    return apply_phase2_recovery(
        statement,
        gemini_caller=caller,
        file_bytes=file_bytes,
        filename=filename,
    )


if __name__ == "__main__":
    unittest.main()
