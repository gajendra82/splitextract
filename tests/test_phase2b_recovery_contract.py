"""Recovery reads printed values. It does not calculate them. No Gemini calls."""

from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from services.base_validator import validate_sales_result
from services.focused_row_request import (
    EXTRACTION_CONTRACT,
    build_focused_row_payload,
    extraction_contract,
    project_semantic_row,
)
from services.hybrid_schema import SalesExtractionOutput
from services.recovery_runner import (
    RecoveryProviderError,
    apply_phase2_recovery,
    build_targeted_payloads,
    merge_stock_recovery,
)
from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    empty_line_item,
)

MAPPING = {
    "Opening": "opening_qty",
    "Purchase": "receipts_qty",
    "Sales": "sales_qty",
    "Closing": "closing_qty",
}
START_MAPPING = {
    "Start Qty": "opening_qty",
    "Inward": "receipts_qty",
    "Outward": "sales_qty",
    "End Qty": "closing_qty",
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


def _statement(items, mapping=None, method="swil_erp", layout=""):
    extra = {
        "extraction_method": method,
        "layout": layout,
        "stock_identity_kind": "opening_receipts_sales_closing",
    }
    if mapping is not None:
        extra["column_mapping"] = mapping
    statement = _apply_stock_identity_validation(
        {
            "line_items": items,
            "totals": {"extra": extra},
        }
    )
    statement["phase1"] = {
        "route": {},
        "validation": validate_sales_result(statement),
    }
    return statement


def _parsed(body):
    return SalesExtractionOutput.model_validate_json(json.dumps(body))


class RecoveryContractTests(unittest.TestCase):
    def test_generic_contract_does_not_name_a_layout(self):
        prompt = extraction_contract()
        self.assertEqual(prompt.split("\n")[0], EXTRACTION_CONTRACT.split("\n")[0])
        for banned in ("Op =", "Pur =", "Sale =", "Bal =", "CLARINA", "KAPIKACHHU", "PILEX"):
            self.assertNotIn(banned, prompt)
        self.assertIn("Do not calculate a missing quantity.", prompt)
        self.assertIn("No column mapping was supplied.", prompt)
        mapped = extraction_contract(START_MAPPING)
        self.assertIn("Start Qty = opening_qty", mapped)
        self.assertNotIn("Op =", mapped)

    def test_printed_closing_wins_over_a_calculated_canonical_value(self):
        projected = project_semantic_row(
            {
                "product_name": "ROW",
                "opening_qty": 47,
                "receipts_qty": 0,
                "sales_qty": 4,
                "closing_qty": 43,
                "Opening": 47,
                "Purchase": 0,
                "Sales": 4,
                "Closing": 45,
            },
            MAPPING,
        )
        fixed = projected["line_items"][0]["fixed"]
        self.assertEqual(
            (fixed["opening_qty"], fixed["receipts_qty"], fixed["sales_qty"], fixed["closing_qty"]),
            (47, 0, 4, 45),
        )
        statement = _statement([_item("ROW", 47, 0, 4, 45)], MAPPING)
        merged = merge_stock_recovery(statement, _parsed(projected))
        row = merged["line_items"][0]
        self.assertEqual(row["closing_qty"], 45.0)
        self.assertEqual(row["extra"]["expected_closing"], 43.0)
        self.assertFalse(row["extra"]["stock_identity_ok"])
        self.assertFalse(merged["totals"]["extra"]["stock_validation"]["is_valid"])

    def test_missing_closing_stays_null(self):
        projected = project_semantic_row(
            {
                "product_name": "ROW",
                "opening_qty": 47,
                "receipts_qty": 0,
                "sales_qty": 4,
                "closing_qty": None,
            },
            MAPPING,
        )
        self.assertIsNone(projected["line_items"][0]["fixed"]["closing_qty"])
        statement = _statement([_item("ROW", 47, 0, 4, None)])
        merged = merge_stock_recovery(statement, _parsed(projected))
        self.assertIsNone(merged["line_items"][0]["closing_qty"])
        self.assertNotEqual(merged["line_items"][0]["closing_qty"], 43)

    def test_printed_zero_stays_zero_and_missing_stays_null(self):
        zero = project_semantic_row(
            {"product_name": "ROW", "receipts_qty": 0, "Purchase": 0},
            MAPPING,
        )
        missing = project_semantic_row(
            {"product_name": "ROW", "receipts_qty": None},
            MAPPING,
        )
        self.assertEqual(zero["line_items"][0]["fixed"]["receipts_qty"], 0)
        self.assertIsNone(missing["line_items"][0]["fixed"]["receipts_qty"])
        parsed_zero = _parsed(zero)
        self.assertEqual(parsed_zero.line_items[0].fixed.receipts_qty, 0.0)
        parsed_missing = SalesExtractionOutput.model_validate_json(
            json.dumps(
                {
                    "line_items": [
                        {
                            "fixed": {
                                "product_name": "ROW",
                                "receipts_qty": "-",
                                "opening_qty": "0",
                            },
                            "dynamic": {},
                        }
                    ]
                }
            )
        )
        self.assertIsNone(parsed_missing.line_items[0].fixed.receipts_qty)
        self.assertEqual(parsed_missing.line_items[0].fixed.opening_qty, 0.0)

    def test_recovery_null_does_not_erase_an_existing_value(self):
        statement = _statement([_item("ROW", 47, 0, 4, 45)])
        parsed = _parsed(
            {
                "line_items": [
                    {
                        "fixed": {
                            "product_name": "ROW",
                            "opening_qty": None,
                            "receipts_qty": None,
                            "sales_qty": None,
                            "closing_qty": None,
                        },
                        "dynamic": {},
                    }
                ]
            }
        )
        merged = merge_stock_recovery(statement, parsed)
        row = merged["line_items"][0]
        self.assertEqual(row["opening_qty"], 47)
        self.assertEqual(row["receipts_qty"], 0)
        self.assertEqual(row["sales_qty"], 4)
        self.assertEqual(row["closing_qty"], 45)

    def test_unknown_column_stays_dynamic(self):
        projected = project_semantic_row(
            {
                "product_name": "ROW",
                "opening_qty": 10,
                "closing_qty": 10,
                "dynamic": {"Special Adj": 7},
                "Weird Qty": 4,
            }
        )
        fixed = projected["line_items"][0]["fixed"]
        dynamic = projected["line_items"][0]["dynamic"]
        self.assertEqual(dynamic["Special Adj"], 7)
        self.assertEqual(dynamic["Weird Qty"], 4)
        self.assertIsNone(fixed["opening_qty"])
        self.assertIsNone(fixed["receipts_qty"])
        self.assertIsNone(fixed["sales_qty"])
        self.assertIsNone(fixed["closing_qty"])
        statement = _statement([_item("ROW", 10, None, None, 10)])
        merged = merge_stock_recovery(statement, _parsed(projected))
        self.assertEqual(merged["line_items"][0]["extra"]["Special Adj"], 7)
        self.assertEqual(merged["line_items"][0]["extra"]["Weird Qty"], 4)
        self.assertIsNone(merged["line_items"][0]["receipts_qty"])
        self.assertIsNone(merged["line_items"][0]["sales_qty"])

    def test_only_a_supplied_mapping_fills_canonical_fields(self):
        printed = {
            "product_name": "ROW",
            "opening_qty": 99,
            "closing_qty": 11,
            "Start Qty": 10,
            "Inward": 2,
            "Outward": 1,
            "End Qty": 15,
        }
        projected = project_semantic_row(printed, START_MAPPING)
        fixed = projected["line_items"][0]["fixed"]
        self.assertEqual(fixed["opening_qty"], 10)
        self.assertEqual(fixed["receipts_qty"], 2)
        self.assertEqual(fixed["sales_qty"], 1)
        self.assertEqual(fixed["closing_qty"], 15)
        unmapped = project_semantic_row(printed)
        self.assertIsNone(unmapped["line_items"][0]["fixed"]["opening_qty"])
        self.assertEqual(unmapped["line_items"][0]["dynamic"]["Start Qty"], 10)
        self.assertIsNone(unmapped["line_items"][0]["fixed"]["receipts_qty"])

    def test_image_text_and_excel_evidence_use_the_same_reader_contract(self):
        image = build_focused_row_payload(b"crop-bytes", headers=["Opening", "Special Adj", "Closing"])
        prompt = image["contents"][0]["parts"][0]["text"]
        self.assertIn("Special Adj", prompt)
        self.assertNotIn("Op =", prompt)
        self.assertEqual(image["contents"][0]["parts"][1]["inline_data"]["mime_type"], "image/png")
        self.assertEqual(image["generationConfig"]["responseJsonSchema"]["title"], "SemanticRow")
        self.assertNotIn("image_to_data", Path(build_focused_row_payload.__code__.co_filename).read_text(encoding="utf-8").split("def request_fingerprint")[0])

        pdf = _pdf(
            [
                "Opening | Purchase | Sales | Closing",
                "ROW | 47 | 0 | 4 | 45",
            ]
        )
        excel = _xlsx()
        for filename, payload in (
            ("statement.pdf", pdf),
            ("statement.xlsx", excel),
        ):
            statement = _statement(
                [_item("ROW", 47, 0, 4, 45)],
                method="pharma_hub_jun_jul_photo",
                layout="pharma_hub_jun_jul",
            )
            payloads = build_targeted_payloads(
                statement,
                strategy="targeted_stock_extraction",
                file_bytes=payload,
                filename=filename,
            )
            self.assertEqual(len(payloads), 1)
            parts = payloads[0]["contents"][0]["parts"]
            self.assertNotIn("inline_data", parts[1])
            self.assertIn("Do not calculate a missing quantity.", parts[0]["text"])
            self.assertIn("Opening = opening_qty", parts[0]["text"])
            self.assertIn("45", parts[1]["text"])
            self.assertNotEqual(
                payloads[0]["generationConfig"]["responseJsonSchema"].get("title"),
                "SemanticRow",
            )

    def test_two_rows_do_not_share_values_and_a_bad_equation_is_not_repaired(self):
        first = _item("ROW A", 47, 0, 4, 45)
        second = _item("ROW B", 100, 20, 10, 120)
        statement = _statement([first, second], MAPPING)
        from services.stock_row_crops import StockRowCrop

        crops = [
            StockRowCrop(
                "ROW A",
                b"CROP-A",
                20,
                10,
                True,
                False,
                header_labels=("Opening", "Purchase", "Sales", "Closing"),
            ),
            StockRowCrop(
                "ROW B",
                b"CROP-B",
                20,
                10,
                True,
                False,
                header_labels=("Opening", "Purchase", "Sales", "Closing"),
            ),
        ]

        def caller(payload):
            image = payload["contents"][0]["parts"][1]["inline_data"]["data"]
            prompt = payload["contents"][0]["parts"][0]["text"]
            self.assertIn("Do not calculate a missing quantity.", prompt)
            self.assertNotIn("Op =", prompt)
            if image == b"CROP-A":
                self.assertIn("ROW A", prompt)
                self.assertNotIn("ROW B", prompt)
                return json.dumps(
                    {
                        "product_name": "ROW A",
                        "opening_qty": 47,
                        "receipts_qty": 0,
                        "sales_qty": 4,
                        "stock_out_qty": None,
                        "closing_qty": 45,
                    }
                )
            return json.dumps(
                {
                    "product_name": "ROW B",
                    "opening_qty": 100,
                    "receipts_qty": 20,
                    "sales_qty": 10,
                    "stock_out_qty": None,
                    "closing_qty": 120,
                }
            )

        with patch("services.stock_row_crops.locate_row_crops", return_value=crops):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg",
                filename="photo.jpeg",
            )
        rows = {item["product_name"]: item for item in recovered["line_items"]}
        self.assertEqual(rows["ROW A"]["closing_qty"], 45)
        self.assertEqual(rows["ROW B"]["closing_qty"], 120)
        self.assertNotEqual(rows["ROW A"]["closing_qty"], 43)
        self.assertNotEqual(rows["ROW B"]["closing_qty"], 110)
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertEqual(recovered["phase2"]["attempts"], 2)

    def test_provider_failure_does_not_start_another_extraction(self):
        statement = _statement([_item("ROW", 47, 0, 4, 45)])
        calls = []

        def caller(_payload):
            calls.append(1)
            raise RecoveryProviderError("429")

        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=caller,
            filename="statement.xlsx",
        )
        self.assertEqual(calls, [1])
        self.assertEqual(recovered["phase2"]["failure_type"], "PROVIDER_ERROR")
        self.assertEqual(recovered["phase2"]["attempts"], 0)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 45)
        runner = Path(__file__).resolve().parents[1].joinpath("services", "recovery_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("Op ->", runner)
        self.assertNotIn("opening_qty +", runner)


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
    sheet.title = "Stock"
    sheet.append(["Opening", "Purchase", "Sales", "Special Adj", "Closing"])
    sheet.append(["ROW", 47, 0, 4, 7, 45])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
