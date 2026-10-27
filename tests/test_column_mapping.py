"""Column labels come from the selected layout. They do not guess or calculate."""

from __future__ import annotations

import copy
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from services.base_validator import validate_sales_result
from services.column_mapping import (
    ColumnMapping,
    header_key,
    header_map_for_evidence,
)
from services.focused_row_request import project_semantic_row
from services.recovery_runner import apply_phase2_recovery, build_targeted_payloads
from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    empty_line_item,
)
from services.stock_identity_profiles import (
    generic_expected_closing,
    select_profile,
)

_ZL_EXTRACT = (
    Path(__file__).resolve().parents[1]
    / "qa_results_images"
    / "extracted"
    / "0000700077_2026_08_ZL_06_304_06092026141652.json"
)


def _item(name, opening, receipts, sales, closing):
    item = empty_line_item()
    item["product_name"] = name
    item["opening_qty"] = opening
    item["receipts_qty"] = receipts
    item["sales_qty"] = sales
    item["closing_qty"] = closing
    item["extra"] = {}
    return item


def _statement(method, layout="", kind="opening_receipts_sales_closing", items=None, mapping=None, filename="statement.pdf"):
    extra = {
        "extraction_method": method,
        "layout": layout,
        "stock_identity_kind": kind,
    }
    if mapping is not None:
        extra["column_mapping"] = mapping
    statement = {
        "source_file": filename,
        "line_items": items
        or [_item("ROW", 47, 0, 4, 45)],
        "totals": {"sales_value": None, "closing_value": None, "extra": extra},
    }
    return statement


def _ready(statement):
    marked = _apply_stock_identity_validation(statement)
    marked["phase1"] = {
        "route": {},
        "validation": validate_sales_result(marked),
    }
    return marked


def _labels(profile):
    return {item.printed_header: item.canonical_field for item in profile.column_mappings}


class ColumnMappingTests(unittest.TestCase):
    def test_known_profile_returns_mappings(self):
        profile = select_profile(
            _statement("monthly_ss_report", "monthly_ss_opening_pur_sale_closing")
        )
        labels = _labels(profile)
        self.assertEqual(labels["Sale Qty"], "sales_qty")
        self.assertEqual(labels["Opening"], "opening_qty")
        self.assertEqual(profile.column_mappings[0].source, "parser")

    def test_unknown_profile_returns_no_mappings(self):
        profile = select_profile(
            _statement("unseen_customer_layout", "unseen_customer_layout", "unknown")
        )
        self.assertEqual(profile.profile_id, "unknown")
        self.assertEqual(profile.column_mappings, ())

    def test_mapping_is_attached_to_the_profile_not_the_filename(self):
        pharma = select_profile(
            _statement(
                "pharma_hub_jun_jul_photo",
                "pharma_hub_jun_jul",
                filename="0000700077_ZL.jpeg",
            )
        )
        other_name = select_profile(
            _statement(
                "pharma_hub_jun_jul_photo",
                "pharma_hub_jun_jul",
                filename="ordinary-scan.jpeg",
            )
        )
        self.assertEqual(_labels(pharma), _labels(other_name))
        leaked = select_profile(
            _statement(
                "unseen_customer_layout",
                "unseen_customer_layout",
                "unknown",
                filename="0000700077_ZL.jpeg",
            )
        )
        self.assertEqual(leaked.column_mappings, ())

    def test_mapping_does_not_calculate_values(self):
        projected = project_semantic_row(
            {
                "product_name": "ROW",
                "opening_qty": 47,
                "receipts_qty": 0,
                "sales_qty": 4,
                "closing_qty": 43,
                "Op": 47,
                "Purc": 0,
                "Sale": 4,
                "Cl qty": 45,
            },
            {
                "Op": "opening_qty",
                "Purc": "receipts_qty",
                "Sale": "sales_qty",
                "Cl qty": "closing_qty",
            },
        )
        fixed = projected["line_items"][0]["fixed"]
        self.assertEqual(
            (fixed["opening_qty"], fixed["receipts_qty"], fixed["sales_qty"], fixed["closing_qty"]),
            (47, 0, 4, 45),
        )
        self.assertNotEqual(fixed["closing_qty"], 43)

    def test_mapping_does_not_validate_values(self):
        item = _item("ROW", 47, 0, 4, 45)
        self.assertEqual(generic_expected_closing(item), 43.0)
        self.assertNotEqual(item["closing_qty"], generic_expected_closing(item))
        profile = select_profile(
            _statement("medivision_op_wsale_photo", kind="opening_receipts_sales_closing")
        )
        self.assertTrue(profile.scores)
        self.assertEqual(
            header_map_for_evidence(profile.column_mappings, ["Op", "Purc", "Sale", "Cl qty"])[
                "Cl qty"
            ],
            "closing_qty",
        )

    def test_op_maps_only_when_the_selected_profile_defines_it(self):
        profile = select_profile(_statement("medivision_op_wsale_photo"))
        mapped = header_map_for_evidence(profile.column_mappings, ["Op", "Purc", "Sale", "Cl qty"])
        self.assertEqual(mapped["Op"], "opening_qty")
        self.assertEqual(profile.column_mappings[0].layout_id, "medivision_op_wsale")
        self.assertEqual(profile.column_mappings[0].source, "explicit_layout_definition")

    def test_op_without_that_profile_does_not_map(self):
        profile = select_profile(_statement("swil_erp"))
        self.assertEqual(header_map_for_evidence(profile.column_mappings, ["Op", "Pur", "Sale", "Bal"]), {})
        self.assertNotIn("Op", _labels(profile))

    def test_closing_value_does_not_map_to_closing_qty(self):
        profile = select_profile(_statement("product_stock_report"))
        mapped = header_map_for_evidence(
            profile.column_mappings,
            ["Opening", "Purchase", "Sale Qty", "Closing Value"],
        )
        self.assertEqual(mapped["Closing Value"], "closing_value")
        self.assertNotIn("closing_qty", mapped.values())

    def test_sale_qty_maps_to_sales_qty(self):
        profile = select_profile(_statement("monthly_ss_report", "monthly_ss_opening_pur_sale_closing"))
        mapped = header_map_for_evidence(profile.column_mappings, ["Sale Qty"])
        self.assertEqual(mapped["Sale Qty"], "sales_qty")

    def test_sale_value_does_not_map_to_sales_qty(self):
        profile = select_profile(_statement("monthly_ss_report", "monthly_ss_opening_pur_sale_closing"))
        mapped = header_map_for_evidence(profile.column_mappings, ["Sale Qty", "Sale Value"])
        self.assertEqual(mapped["Sale Qty"], "sales_qty")
        self.assertEqual(mapped["Sale Value"], "sales_value")
        self.assertNotEqual(mapped["Sale Value"], "sales_qty")

    def test_multiple_mapped_columns_work(self):
        profile = select_profile(
            _statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul")
        )
        mapped = header_map_for_evidence(
            profile.column_mappings,
            ["Opening", "Purchase", "Sales", "Stock-Out", "Closing", "Stock-Value"],
        )
        self.assertEqual(
            mapped,
            {
                "Opening": "opening_qty",
                "Purchase": "receipts_qty",
                "Sales": "sales_qty",
                "Stock-Out": "stock_out_qty",
                "Closing": "closing_qty",
                "Stock-Value": "closing_value",
            },
        )

    def test_duplicate_canonical_mapping_is_not_silently_chosen(self):
        mappings = (
            ColumnMapping("Opening Qty", "opening_qty", "parser", "fixture"),
            ColumnMapping("Opening Stock", "opening_qty", "parser", "fixture"),
        )
        self.assertEqual(
            header_map_for_evidence(mappings, ["Opening Qty", "Opening Stock"]),
            {},
        )
        self.assertEqual(
            header_map_for_evidence(mappings, ["Opening Qty"]),
            {"Opening Qty": "opening_qty"},
        )

    def test_unknown_printed_column_remains_dynamic(self):
        projected = project_semantic_row(
            {
                "product_name": "ROW",
                "opening_qty": 10,
                "Special Adj": 7,
            },
            {"Opening": "opening_qty"},
        )
        self.assertEqual(projected["line_items"][0]["dynamic"]["Special Adj"], 7)
        self.assertNotIn("special_adj_qty", projected["line_items"][0]["fixed"])

    def test_normalization_handles_harmless_whitespace_and_case(self):
        profile = select_profile(_statement("monthly_ss_report", "monthly_ss_opening_pur_sale_closing"))
        mapped = header_map_for_evidence(profile.column_mappings, ["  sale   QTY "])
        self.assertEqual(list(mapped.values()), ["sales_qty"])
        self.assertEqual(header_key("Sale Qty"), header_key("saleqty"))

    def test_aggressive_fuzzy_matching_does_not_create_mappings(self):
        monthly = select_profile(_statement("monthly_ss_report", "monthly_ss_opening_pur_sale_closing"))
        pharma = select_profile(_statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul"))
        medivision = select_profile(_statement("medivision_op_wsale_photo"))
        self.assertEqual(
            header_map_for_evidence(
                monthly.column_mappings,
                ["Salesman", "Closing Valuation", "Operational", "Sale"],
            ),
            {},
        )
        self.assertNotIn(
            "Op",
            header_map_for_evidence(pharma.column_mappings, ["Op", "Pur", "Sale", "Bal"]),
        )
        self.assertNotIn(
            "Operational",
            header_map_for_evidence(medivision.column_mappings, ["Operational"]),
        )

    def test_zl_mapping_comes_from_the_recognized_layout(self):
        statement = _statement(
            "pharma_hub_jun_jul_photo",
            "pharma_hub_jun_jul",
            filename="0000700077_2026_08_ZL_06_304.jpeg",
        )
        profile = select_profile(statement)
        opening = next(item for item in profile.column_mappings if item.canonical_field == "opening_qty")
        self.assertEqual(opening.printed_header, "Opening")
        self.assertEqual(opening.layout_id, "pharma_hub_jun_jul")
        self.assertEqual(opening.source, "explicit_layout_definition")
        self.assertNotIn("Op", _labels(profile))
        self.assertEqual(
            header_map_for_evidence(profile.column_mappings, ["Op", "Pur", "Sale", "SS Sp Qty", "Bal"]),
            {},
        )

    def test_unknown_customer_does_not_obtain_the_zl_mapping(self):
        profile = select_profile(
            _statement(
                "unseen_customer_layout",
                "",
                "unknown",
                filename="0000700077_ZL.jpeg",
            )
        )
        self.assertEqual(profile.column_mappings, ())
        self.assertFalse(profile.supports_recovery)

    def test_not_scored_profile_does_not_become_recovery_enabled(self):
        statement = _statement(
            "medica_opstk_columns",
            mapping={"Op": "opening_qty", "Bal": "closing_qty"},
        )
        profile = select_profile(statement)
        self.assertEqual(profile.profile_id, "not_scored")
        self.assertFalse(profile.supports_recovery)
        self.assertEqual(profile.column_mappings, ())

    def test_existing_parser_output_remains_unchanged(self):
        statement = _statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul")
        before = copy.deepcopy(statement)
        select_profile(statement)
        self.assertEqual(statement, before)

    def test_existing_identity_formula_remains_unchanged(self):
        item = _item("ROW", 106, 100, 131, 74)
        item["extra"]["stock_out_qty"] = 1
        item["extra"]["sales_scheme_qty"] = 0
        self.assertEqual(generic_expected_closing(item), 74.0)
        clarina = _item("ROW", 47, 0, 4, 45)
        self.assertEqual(generic_expected_closing(clarina), 43.0)

    def test_mappings_do_not_leak_between_profiles(self):
        pharma = select_profile(_statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul"))
        monthly = select_profile(_statement("monthly_ss_report", "monthly_ss_opening_pur_sale_closing"))
        generic = select_profile(_statement("swil_erp"))
        self.assertIn("Opening", _labels(pharma))
        self.assertNotIn("Sale Qty", _labels(pharma))
        self.assertIn("Sale Qty", _labels(monthly))
        self.assertNotIn("Stock-Out", _labels(monthly))
        self.assertEqual(generic.column_mappings, ())
        self.assertEqual(
            header_map_for_evidence(generic.column_mappings, ["Opening", "Op", "Sale Qty"]),
            {},
        )

    def test_saved_zl_extract_exposes_the_pharma_hub_layout_mapping(self):
        if not _ZL_EXTRACT.is_file():
            self.skipTest("saved ZL extract is not in the workspace")
        statement = json.loads(_ZL_EXTRACT.read_text(encoding="utf-8"))
        before = copy.deepcopy(statement.get("line_items"))
        profile = select_profile(statement)
        self.assertEqual(statement.get("line_items"), before)
        self.assertEqual(
            str((statement.get("totals") or {}).get("extra", {}).get("extraction_method")),
            "pharma_hub_jun_jul_photo",
        )
        opening = next(item for item in profile.column_mappings if item.canonical_field == "opening_qty")
        self.assertEqual(opening.printed_header, "Opening")
        self.assertEqual(opening.source, "explicit_layout_definition")
        self.assertEqual(opening.layout_id, "pharma_hub_jun_jul")
        self.assertNotIn("Op", _labels(profile))


class ColumnMappingIntegrationTests(unittest.TestCase):
    def test_known_profile_reads_mapped_columns_and_identity_still_rejects(self):
        statement = _ready(
            _statement(
                "medivision_op_wsale_photo",
                filename="stock.jpeg",
            )
        )
        from services.stock_row_crops import StockRowCrop

        crops = [
            StockRowCrop(
                "ROW",
                b"CROP",
                20,
                10,
                True,
                False,
                header_labels=("Op", "Purc", "Sale", "Cl qty"),
            )
        ]

        def caller(payload):
            prompt = payload["contents"][0]["parts"][0]["text"]
            self.assertIn("Op = opening_qty", prompt)
            self.assertIn("Cl qty = closing_qty", prompt)
            self.assertNotIn("closing_qty +", prompt)
            self.assertNotIn("43", prompt)
            return json.dumps(
                {
                    "product_name": "ROW",
                    "opening_qty": 47,
                    "receipts_qty": 0,
                    "sales_qty": 4,
                    "closing_qty": 43,
                    "Cl qty": 45,
                }
            )

        with patch("services.stock_row_crops.locate_row_crops", return_value=crops):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg",
                filename="stock.jpeg",
            )
        row = recovered["line_items"][0]
        self.assertEqual(row["opening_qty"], 47)
        self.assertEqual(row["receipts_qty"], 0)
        self.assertEqual(row["sales_qty"], 4)
        self.assertEqual(row["closing_qty"], 45)
        self.assertEqual(row["extra"]["expected_closing"], 43.0)
        self.assertFalse(row["extra"]["stock_identity_ok"])
        self.assertEqual(recovered["phase2"]["status"], "unresolved")

    def test_unknown_profile_discards_model_canonical_values(self):
        statement = _ready(
            _statement("unseen_customer_layout", "custom", "unknown", filename="custom.jpeg")
        )
        calls = []

        def caller(_payload):
            calls.append(1)
            return json.dumps(
                {
                    "product_name": "ROW",
                    "opening_qty": 1,
                    "receipts_qty": 2,
                    "sales_qty": 3,
                    "closing_qty": 4,
                }
            )

        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=caller,
            file_bytes=b"jpeg",
            filename="custom.jpeg",
        )
        self.assertEqual(calls, [])
        self.assertEqual(recovered["phase2"]["attempts"], 0)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 45)
        projected = project_semantic_row(
            {
                "product_name": "ROW",
                "opening_qty": 1,
                "receipts_qty": 2,
                "sales_qty": 3,
                "closing_qty": 4,
                "Op": 47,
            }
        )
        fixed = projected["line_items"][0]["fixed"]
        self.assertIsNone(fixed["opening_qty"])
        self.assertIsNone(fixed["closing_qty"])
        self.assertEqual(projected["line_items"][0]["dynamic"]["Op"], 47)

    def test_global_excel_alias_is_not_a_trusted_recovery_mapping(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["OB Qty", "In Qty", "Out Qty", "CB Qty"])
        sheet.append(["ROW", 47, 0, 4, 45])
        buffer = io.BytesIO()
        workbook.save(buffer)
        stored = {
            "opening_qty": {"header": "OB Qty", "column": "A", "confidence": 0.9},
            "receipts_qty": {"header": "In Qty", "column": "B", "confidence": 0.9},
            "sales_qty": {"header": "Out Qty", "column": "C", "confidence": 0.9},
            "closing_qty": {"header": "CB Qty", "column": "D", "confidence": 0.9},
        }
        statement = _ready(_statement("swil_erp", mapping=stored, filename="stock.xlsx"))
        payloads = build_targeted_payloads(
            statement,
            strategy="targeted_stock_extraction",
            file_bytes=buffer.getvalue(),
            filename="stock.xlsx",
        )
        prompt = payloads[0]["contents"][0]["parts"][0]["text"]
        self.assertNotIn("OB Qty = opening_qty", prompt)
        self.assertNotIn("In Qty = receipts_qty", prompt)
        self.assertNotIn("Opening = opening_qty", prompt)
        self.assertNotIn("Op = opening_qty", prompt)
        other = select_profile(_statement("swil_erp", filename="stock.xlsx"))
        self.assertEqual(other.column_mappings, ())
        owned = select_profile(
            _statement(
                "pack_op_pur_bal_stock_sale_vision",
                "pack_op_pur_bal_stock_sale",
                mapping={"opening_qty": {"header": "Op", "column": "A", "confidence": 1}},
            )
        )
        self.assertEqual(_labels(owned)["Op"], "opening_qty")
        leaked = select_profile(
            _statement(
                "pharma_hub_jun_jul_photo",
                "pharma_hub_jun_jul",
                mapping={"opening_qty": {"header": "Op", "column": "A", "confidence": 1}},
            )
        )
        self.assertNotIn("Op", _labels(leaked))
        self.assertEqual(_labels(leaked)["Opening"], "opening_qty")

    def test_product_wise_issues_are_not_the_sales_amount_column(self):
        profile = select_profile(_statement("product_wise_stock_statement"))
        mapped = header_map_for_evidence(
            profile.column_mappings,
            ["Opening", "Receipt", "Issues", "Closing", "Sales"],
        )
        self.assertEqual(mapped["Issues"], "sales_qty")
        self.assertNotIn("Sales", mapped)


if __name__ == "__main__":
    unittest.main()
