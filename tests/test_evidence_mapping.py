"""Evidence headers align to a layout only through trusted text, aliases, or indexes."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from services.base_validator import validate_sales_result
from services.column_mapping import ColumnMapping
from services.evidence_mapping import NO_SAFE_MATCH, align_evidence_columns
from services.focused_row_request import project_semantic_row
from services.recovery_runner import apply_phase2_recovery, build_targeted_payloads
from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    empty_line_item,
)
from services.stock_identity_profiles import IdentityProfile, select_profile

_ZL_OCR = (
    "[___ltem",
    "Name",
    "wil",
    "pack",
    "OF",
    "Puy",
    "SP",
    "Put",
    "Val",
    "Sele",
    "88",
    "Sp",
    "Qty",
    "Sale",
    "Val__Bal|",
    "Bal",
    "Val|",
    "i",
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


def _statement(method, layout="", kind="opening_receipts_sales_closing", filename="statement.pdf", product="ROW"):
    return {
        "source_file": filename,
        "line_items": [_item(product, 47, 0, 4, 45)],
        "totals": {
            "extra": {
                "extraction_method": method,
                "layout": layout,
                "stock_identity_kind": kind,
            }
        },
    }


def _ready(statement):
    marked = _apply_stock_identity_validation(statement)
    marked["phase1"] = {"route": {}, "validation": validate_sales_result(marked)}
    return marked


def _profile(mappings, profile_id="generic"):
    return IdentityProfile(profile_id, True, True, tuple(mappings))


def _map(header, field, layout, aliases=(), column_index=None):
    return ColumnMapping(
        header,
        field,
        "explicit_layout_definition",
        layout,
        aliases=tuple(aliases),
        column_index=column_index,
    )


class EvidenceMappingTests(unittest.TestCase):
    def test_exact_normalized_header(self):
        profile = select_profile(_statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul"))
        aligned = align_evidence_columns(
            profile,
            profile.column_mappings,
            ["  OPENING ", "purchase"],
        )
        self.assertEqual(aligned.matched["OPENING"], "opening_qty")
        self.assertEqual(aligned.matched["purchase"], "receipts_qty")
        self.assertEqual(aligned.alignment_source, "exact_normalized")
        self.assertTrue(aligned.promotion_allowed)

    def test_explicit_profile_scoped_alias(self):
        profile = select_profile(_statement("product_stock_report"))
        aligned = align_evidence_columns(profile, profile.column_mappings, ["Sale Qty"])
        self.assertEqual(aligned.matched["Sale Qty"], "sales_qty")
        self.assertEqual(aligned.matches[0].source, "explicit_alias")

    def test_alias_isolation(self):
        pack = select_profile(
            _statement("pack_op_pur_bal_stock_sale_vision", "pack_op_pur_bal_stock_sale")
        )
        pharma = select_profile(_statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul"))
        self.assertEqual(
            align_evidence_columns(pack, pack.column_mappings, ["Op"]).matched["Op"],
            "opening_qty",
        )
        self.assertEqual(
            align_evidence_columns(pharma, pharma.column_mappings, ["Op"]).alignment_source,
            NO_SAFE_MATCH,
        )

    def test_trusted_positional_mapping(self):
        mappings = (
            _map("Opening", "opening_qty", "fixture", column_index=0),
            _map("Purchase", "receipts_qty", "fixture", column_index=1),
            _map("Sales", "sales_qty", "fixture", column_index=2),
            _map("Closing", "closing_qty", "fixture", column_index=3),
        )
        profile = _profile(mappings)
        aligned = align_evidence_columns(
            profile,
            mappings,
            ["Op", "Pur", "Sale", "Bal"],
            evidence_geometry=[
                {"header": "Op", "column_index": 0},
                {"header": "Pur", "column_index": 1},
                {"header": "Sale", "column_index": 2},
                {"header": "Bal", "column_index": 3},
            ],
        )
        self.assertEqual(
            aligned.matched,
            {
                "Op": "opening_qty",
                "Pur": "receipts_qty",
                "Sale": "sales_qty",
                "Bal": "closing_qty",
            },
        )
        self.assertEqual(aligned.alignment_source, "source_column_index")

    def test_damaged_ocr_with_trusted_column_index(self):
        mappings = (
            _map("Purchase", "receipts_qty", "fixture", column_index=4),
        )
        profile = _profile(mappings)
        aligned = align_evidence_columns(
            profile,
            mappings,
            ["Puy"],
            evidence_geometry=[{"header": "Puy", "column_index": 4}],
        )
        self.assertEqual(aligned.matched["Puy"], "receipts_qty")
        self.assertEqual(aligned.matches[0].trusted_header, "Purchase")

    def test_damaged_ocr_without_structure_is_no_safe_match(self):
        profile = select_profile(_statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul"))
        aligned = align_evidence_columns(profile, profile.column_mappings, ["Puy", "OF", "Sele"])
        self.assertEqual(aligned.alignment_source, NO_SAFE_MATCH)
        self.assertFalse(aligned.promotion_allowed)
        self.assertEqual(aligned.matched, {})

    def test_fuzzy_similarity_alone_is_no_safe_match(self):
        profile = select_profile(_statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul"))
        aligned = align_evidence_columns(
            profile,
            profile.column_mappings,
            ["Puy", "Sele", "OF", "Bal"],
        )
        self.assertEqual(aligned.alignment_source, NO_SAFE_MATCH)
        self.assertNotIn("receipts_qty", aligned.matched.values())

    def test_ambiguous_headers_are_not_promoted(self):
        mappings = (
            _map("Opening Qty", "opening_qty", "fixture"),
            _map("Opening Stock", "opening_qty", "fixture"),
        )
        profile = _profile(mappings)
        aligned = align_evidence_columns(
            profile,
            mappings,
            ["Opening Qty", "Opening Stock"],
        )
        self.assertEqual(aligned.matched, {})
        self.assertIn("opening_qty", aligned.ambiguous)
        self.assertFalse(aligned.promotion_allowed)

    def test_duplicate_canonical_mapping_blocks_both(self):
        mappings = (
            _map("Sale", "sales_qty", "fixture"),
            _map("Sold", "sales_qty", "fixture"),
            _map("Closing", "closing_qty", "fixture"),
        )
        profile = _profile(mappings)
        aligned = align_evidence_columns(profile, mappings, ["Sale", "Sold", "Closing"])
        self.assertNotIn("sales_qty", aligned.matched.values())
        self.assertEqual(aligned.matched["Closing"], "closing_qty")

    def test_quantity_and_value_stay_separate(self):
        profile = select_profile(
            _statement("pack_op_pur_bal_stock_sale_vision", "pack_op_pur_bal_stock_sale")
        )
        aligned = align_evidence_columns(
            profile,
            profile.column_mappings,
            ["Sale", "Sale Val", "Bal.", "Bal Val"],
        )
        self.assertEqual(aligned.matched["Sale"], "sales_qty")
        self.assertEqual(aligned.matched["Sale Val"], "sales_value")
        self.assertEqual(aligned.matched["Bal."], "closing_qty")
        self.assertEqual(aligned.matched["Bal Val"], "closing_value")

    def test_unknown_header_is_not_promoted(self):
        profile = select_profile(_statement("monthly_ss_report", "monthly_ss_opening_pur_sale_closing"))
        aligned = align_evidence_columns(
            profile,
            profile.column_mappings,
            ["Sale Qty", "Special Adj"],
        )
        self.assertEqual(aligned.matched, {"Sale Qty": "sales_qty"})
        self.assertIn("Special Adj", aligned.unmatched_headers)
        projected = project_semantic_row(
            {"product_name": "ROW", "sales_qty": 1, "Special Adj": 7},
            aligned.matched,
        )
        self.assertEqual(projected["line_items"][0]["dynamic"]["Special Adj"], 7)

    def test_unknown_profile_has_no_alignment(self):
        profile = select_profile(_statement("unseen_customer_layout", "", "unknown"))
        injected = (_map("Op", "opening_qty", "pharma_hub_jun_jul"),)
        aligned = align_evidence_columns(profile, injected, ["Op", "Pur", "Sale", "Bal"])
        self.assertEqual(aligned.alignment_source, NO_SAFE_MATCH)
        self.assertIn("profile_not_recoverable", aligned.rejection_reasons)

    def test_filename_does_not_change_alignment(self):
        left = select_profile(
            _statement(
                "pharma_hub_jun_jul_photo",
                "pharma_hub_jun_jul",
                filename="0000700077_ZL.jpeg",
            )
        )
        right = select_profile(
            _statement(
                "pharma_hub_jun_jul_photo",
                "pharma_hub_jun_jul",
                filename="ordinary.jpeg",
            )
        )
        headers = ["Opening", "Op"]
        self.assertEqual(
            align_evidence_columns(left, left.column_mappings, headers).matched,
            align_evidence_columns(right, right.column_mappings, headers).matched,
        )

    def test_product_name_does_not_change_alignment(self):
        clarina = select_profile(
            _statement("medivision_op_wsale_photo", product="CLARINA")
        )
        other = select_profile(
            _statement("medivision_op_wsale_photo", product="OTHER PRODUCT")
        )
        headers = ["Op", "Purc"]
        self.assertEqual(
            align_evidence_columns(clarina, clarina.column_mappings, headers).matched,
            align_evidence_columns(other, other.column_mappings, headers).matched,
        )

    def test_profile_does_not_inherit_another_profiles_aliases(self):
        pharma = select_profile(_statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul"))
        medi = select_profile(_statement("medivision_op_wsale_photo"))
        headers = ["Op", "Purc", "Opening"]
        pharma_aligned = align_evidence_columns(pharma, pharma.column_mappings, headers)
        medi_aligned = align_evidence_columns(medi, medi.column_mappings, headers)
        self.assertEqual(pharma_aligned.matched, {"Opening": "opening_qty"})
        self.assertEqual(medi_aligned.matched["Op"], "opening_qty")
        self.assertNotIn("Opening", medi_aligned.matched)

    def test_alignment_is_evidence_local(self):
        profile = select_profile(_statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul"))
        first = align_evidence_columns(profile, profile.column_mappings, ["Opening", "Closing"])
        second = align_evidence_columns(profile, profile.column_mappings, ["Sales"])
        self.assertEqual(set(first.matched), {"Opening", "Closing"})
        self.assertEqual(second.matched, {"Sales": "sales_qty"})
        self.assertNotIn("Opening", second.matched)

    def test_partial_evidence_receives_only_present_columns(self):
        statement = _ready(_statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul", filename="row.pdf"))
        pdf = _pdf(
            [
                "Page 1 2026 08 31 0000700077 extra digits 12345 67890",
                "Sales | Closing",
                "ROW | 4 | 45",
            ]
        )
        payloads = build_targeted_payloads(
            statement,
            strategy="targeted_stock_extraction",
            file_bytes=pdf,
            filename="row.pdf",
        )
        prompt = payloads[0]["contents"][0]["parts"][0]["text"]
        self.assertIn("Sales = sales_qty", prompt)
        self.assertIn("Closing = closing_qty", prompt)
        self.assertNotIn("Opening = opening_qty", prompt)
        self.assertNotIn("Purchase = receipts_qty", prompt)

    def test_alignment_does_not_calculate(self):
        profile = select_profile(_statement("medivision_op_wsale_photo"))
        aligned = align_evidence_columns(
            profile,
            profile.column_mappings,
            ["Op", "Purc", "Sale", "Cl qty"],
        )
        projected = project_semantic_row(
            {
                "product_name": "ROW",
                "opening_qty": 47,
                "receipts_qty": 0,
                "sales_qty": 4,
                "closing_qty": 43,
                "Cl qty": 45,
            },
            aligned.matched,
        )
        self.assertEqual(projected["line_items"][0]["fixed"]["closing_qty"], 45)
        self.assertNotIn(43, aligned.matched.values())

    def test_alignment_does_not_validate(self):
        profile = select_profile(_statement("medivision_op_wsale_photo"))
        aligned = align_evidence_columns(
            profile,
            profile.column_mappings,
            ["Op", "Purc", "Sale", "Cl qty"],
        )
        self.assertEqual(aligned.matched["Cl qty"], "closing_qty")
        self.assertNotIn("stock_identity", aligned.alignment_source)

    def test_zl_ocr_alignment_is_deterministic_and_unsafe(self):
        profile = select_profile(
            _statement(
                "pharma_hub_jun_jul_photo",
                "pharma_hub_jun_jul",
                filename="0000700077_2026_08_ZL_06_304.jpeg",
                product="H CLARINA ANTI ACNE CREAM",
            )
        )
        first = align_evidence_columns(profile, profile.column_mappings, _ZL_OCR)
        second = align_evidence_columns(profile, profile.column_mappings, _ZL_OCR)
        self.assertEqual(first.matched, {})
        self.assertEqual(first, second)
        self.assertEqual(first.alignment_source, NO_SAFE_MATCH)
        self.assertFalse(first.promotion_allowed)

    def test_image_width_does_not_change_semantic_alignment(self):
        profile = select_profile(_statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul"))
        narrow = align_evidence_columns(
            profile,
            profile.column_mappings,
            ["Opening", "Closing"],
            evidence_metadata={"width": 800},
        )
        wide = align_evidence_columns(
            profile,
            profile.column_mappings,
            ["Opening", "Closing"],
            evidence_metadata={"width": 4000},
        )
        self.assertEqual(narrow.matched, wide.matched)

    def test_column_order_follows_the_evidence(self):
        profile = select_profile(_statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul"))
        aligned = align_evidence_columns(
            profile,
            profile.column_mappings,
            ["Closing", "Opening", "Sales"],
        )
        self.assertEqual(list(aligned.matched), ["Closing", "Opening", "Sales"])


class EvidenceMappingIntegrationTests(unittest.TestCase):
    def test_exact_headers_reach_the_reader_and_identity_keeps_45(self):
        statement = _ready(
            _statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul", filename="stock.jpeg")
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
                header_labels=("Opening", "Purchase", "Sales", "Closing"),
            )
        ]

        def caller(payload):
            prompt = payload["contents"][0]["parts"][0]["text"]
            self.assertIn("Opening = opening_qty", prompt)
            self.assertNotIn("Op = opening_qty", prompt)
            return json.dumps(
                {
                    "product_name": "ROW",
                    "opening_qty": 47,
                    "receipts_qty": 0,
                    "sales_qty": 4,
                    "closing_qty": 43,
                    "Closing": 45,
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
        self.assertEqual(row["closing_qty"], 45)
        self.assertEqual(row["extra"]["expected_closing"], 43.0)
        self.assertFalse(row["extra"]["stock_identity_ok"])
        self.assertEqual(recovered["phase2"]["status"], "unresolved")

    def test_layout_alias_reaches_the_reader(self):
        statement = _ready(
            _statement(
                "pack_op_pur_bal_stock_sale_vision",
                "pack_op_pur_bal_stock_sale",
                filename="pack.jpeg",
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
                header_labels=("Op", "Pur", "Sale", "Bal."),
            )
        ]

        def caller(payload):
            prompt = payload["contents"][0]["parts"][0]["text"]
            self.assertIn("Op = opening_qty", prompt)
            self.assertIn("Pur = receipts_qty", prompt)
            self.assertIn("Sale = sales_qty", prompt)
            self.assertIn("Bal. = closing_qty", prompt)
            return json.dumps(
                {
                    "product_name": "ROW",
                    "opening_qty": 47,
                    "receipts_qty": 0,
                    "sales_qty": 4,
                    "closing_qty": 45,
                }
            )

        with patch("services.stock_row_crops.locate_row_crops", return_value=crops):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg",
                filename="pack.jpeg",
            )
        row = recovered["line_items"][0]
        self.assertEqual(
            (row["opening_qty"], row["receipts_qty"], row["sales_qty"], row["closing_qty"]),
            (47, 0, 4, 45),
        )
        self.assertEqual(recovered["phase2"]["status"], "unresolved")

    def test_damaged_headers_without_indexes_do_not_promote(self):
        statement = _ready(
            _statement("pharma_hub_jun_jul_photo", "pharma_hub_jun_jul", filename="stock.jpeg")
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
                header_labels=("OF", "Puy", "Sele", "Bal"),
            )
        ]
        calls = []

        def caller(payload):
            calls.append(payload["contents"][0]["parts"][0]["text"])
            return json.dumps(
                {
                    "product_name": "ROW",
                    "opening_qty": 1,
                    "receipts_qty": 2,
                    "sales_qty": 3,
                    "closing_qty": 4,
                }
            )

        with patch("services.stock_row_crops.locate_row_crops", return_value=crops):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg",
                filename="stock.jpeg",
            )
        self.assertIn("No column mapping was supplied.", calls[0])
        self.assertEqual(recovered["line_items"][0]["opening_qty"], 47)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 45)

    def test_unknown_profile_preserves_parser_values(self):
        statement = _ready(_statement("unseen_customer_layout", "", "unknown", filename="custom.jpeg"))
        calls = []

        def caller(_payload):
            calls.append(1)
            return "{}"

        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=caller,
            file_bytes=b"jpeg",
            filename="custom.jpeg",
        )
        self.assertEqual(calls, [])
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 45)
        self.assertEqual(recovered["phase2"]["attempts"], 0)


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


if __name__ == "__main__":
    unittest.main()
