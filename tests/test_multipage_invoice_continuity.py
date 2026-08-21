"""Multi-page invoice continuity, validation, and page-level merge tests."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.multipage_invoice_continuity import (  # noqa: E402
    PageClassification,
    apply_controlled_combined_ocr_fallback,
    classify_page_continuity,
    combined_result_beats_page_level,
    extract_items_from_full_data,
    merge_multipage_page_level_extractions,
    normalize_invoice_number,
    validate_page_line_items,
)


def _item(name, qty, rate, amount, batch=""):
    return {
        "product_description": name,
        "quantity": str(qty),
        "unit_price": str(rate),
        "total_amount": str(amount),
        "lot_batch_number": batch,
        "additional_fields": {},
    }


def _page(inv, items):
    return {
        "invoice_no": inv,
        "ocr_text": f"Invoice {inv}" if inv else "continuation table",
        "full_data": {
            "data": {
                "invoice_no": inv or "",
                "line_items": {"items": items, "count": len(items)},
            }
        },
    }


class TestNormalizeInvoice(unittest.TestCase):
    def test_strips_spaces_hyphens_case(self):
        self.assertEqual(normalize_invoice_number("inv-001"), "INV001")
        self.assertEqual(normalize_invoice_number(" INV 001 "), "INV001")
        self.assertEqual(normalize_invoice_number("Inv_001"), "INV001")

    def test_keeps_distinct_numbers(self):
        self.assertNotEqual(
            normalize_invoice_number("INV001"),
            normalize_invoice_number("INV002"),
        )


class TestClassifyContinuity(unittest.TestCase):
    def test_same_invoice(self):
        cls = classify_page_continuity(
            page_index=1,
            page_invoice_no="INV-001",
            current_invoice_no="INV001",
            attach_decision=True,
        )
        self.assertEqual(cls, PageClassification.SAME_INVOICE)

    def test_new_invoice(self):
        cls = classify_page_continuity(
            page_index=1,
            page_invoice_no="INV002",
            current_invoice_no="INV001",
            attach_decision=False,
        )
        self.assertEqual(cls, PageClassification.NEW_INVOICE)

    def test_continuation_no_invoice(self):
        cls = classify_page_continuity(
            page_index=1,
            page_invoice_no=None,
            current_invoice_no="INV001",
            attach_decision=True,
        )
        self.assertEqual(cls, PageClassification.CONTINUATION_NO_INVOICE)


class TestPageValidation(unittest.TestCase):
    def test_rejects_footer_as_product(self):
        result = validate_page_line_items([
            _item("Authorized Signatory", 1, 1, 1),
            _item("ZYTANIX 5 TAB", 4, 247.5, 990),
        ])
        names = [i["product_description"] for i in result.accepted_items]
        self.assertIn("ZYTANIX 5 TAB", names)
        self.assertEqual(len(result.rejected_items), 1)

    def test_flags_gst_as_quantity(self):
        # qty=18 (GST%), rate huge, amount tiny → reject
        result = validate_page_line_items([
            _item("BAD ROW", 18, 5000, 10),
        ])
        self.assertTrue(
            result.status in {"invalid", "suspicious"}
            or result.rejected_items
            or any("gst" in w for w in result.warnings)
        )

    def test_keeps_valid_pharma_row(self):
        result = validate_page_line_items([
            _item("LINID TAB 10TAB", 48, 152.5, 7320),
        ])
        self.assertEqual(len(result.accepted_items), 1)
        self.assertEqual(result.status, "ok")


class TestScenarioMerge(unittest.TestCase):
    """Scenarios 1–6 from the multi-page requirements."""

    def test_scenario1_same_invoice_across_pages(self):
        page_results = [
            _page("INV001", [_item("A", 1, 10, 10), _item("B", 2, 10, 20)]),
            _page("INV001", [_item("C", 1, 10, 10), _item("D", 1, 10, 10)]),
        ]
        group = {
            "invoice_no": "INV001",
            "pages": [0, 1],
            "extracted_data": page_results[0]["full_data"],
        }
        result = merge_multipage_page_level_extractions(
            group=group, page_results=page_results, batch_id="t1"
        )
        self.assertTrue(result.applied)
        self.assertFalse(result.needs_combined_fallback)
        names = [
            i["product_description"]
            for i in extract_items_from_full_data(result.extracted_data)
        ]
        self.assertEqual(sorted(names), ["A", "B", "C", "D"])

    def test_scenario2_missing_invoice_on_continuation(self):
        page_results = [
            _page("INV001", [_item("A", 1, 10, 10), _item("B", 1, 10, 10)]),
            _page(None, [_item("C", 1, 10, 10), _item("D", 1, 10, 10)]),
        ]
        group = {
            "invoice_no": "INV001",
            "pages": [0, 1],
            "extracted_data": page_results[0]["full_data"],
        }
        result = merge_multipage_page_level_extractions(
            group=group, page_results=page_results, batch_id="t2"
        )
        names = [
            i["product_description"]
            for i in extract_items_from_full_data(result.extracted_data)
        ]
        self.assertEqual(sorted(names), ["A", "B", "C", "D"])
        self.assertEqual(
            result.page_audits[1].classification,
            PageClassification.CONTINUATION_NO_INVOICE,
        )

    def test_scenario3_new_invoice_is_separate_group(self):
        # Continuity attach is upstream; here each group merges alone
        g1 = merge_multipage_page_level_extractions(
            group={
                "invoice_no": "INV001",
                "pages": [0],
                "extracted_data": _page("INV001", [_item("A", 1, 10, 10)])["full_data"],
            },
            page_results=[_page("INV001", [_item("A", 1, 10, 10)])],
        )
        self.assertFalse(g1.applied)  # single page — no multipage merge
        g2_pages = [
            _page("INV001", [_item("A", 1, 10, 10), _item("B", 1, 10, 10)]),
            _page("INV002", [_item("C", 1, 10, 10), _item("D", 1, 10, 10)]),
        ]
        # If wrongly grouped together, products would mix — production grouping
        # prevents this. Simulate correct separate groups:
        pod1 = merge_multipage_page_level_extractions(
            group={"invoice_no": "INV001", "pages": [0], "extracted_data": g2_pages[0]["full_data"]},
            page_results=g2_pages,
        )
        pod2 = merge_multipage_page_level_extractions(
            group={"invoice_no": "INV002", "pages": [1], "extracted_data": g2_pages[1]["full_data"]},
            page_results=g2_pages,
        )
        self.assertFalse(pod1.applied)
        self.assertFalse(pod2.applied)
        self.assertEqual(
            [i["product_description"] for i in extract_items_from_full_data(g2_pages[0]["full_data"])],
            ["A", "B"],
        )
        self.assertEqual(
            [i["product_description"] for i in extract_items_from_full_data(g2_pages[1]["full_data"])],
            ["C", "D"],
        )

    def test_scenario4_multiple_continuation_pages(self):
        page_results = [
            _page("INV001", [_item("A", 1, 10, 10)]),
            _page(None, [_item("B", 1, 10, 10)]),
            _page(None, [_item("C", 1, 10, 10)]),
        ]
        result = merge_multipage_page_level_extractions(
            group={
                "invoice_no": "INV001",
                "pages": [0, 1, 2],
                "extracted_data": page_results[0]["full_data"],
            },
            page_results=page_results,
            batch_id="t4",
        )
        names = [
            i["product_description"]
            for i in extract_items_from_full_data(result.extracted_data)
        ]
        self.assertEqual(sorted(names), ["A", "B", "C"])

    def test_scenario5_bad_page_isolated(self):
        page_results = [
            _page("INV001", [_item("GOOD1", 2, 50, 100)]),
            _page(
                None,
                [
                    _item("Authorized Signatory", 1, 1, 1),
                    _item("Grand Total", 18, 999999, 1),  # gst-as-qty + footer
                ],
            ),
            _page(None, [_item("GOOD3", 3, 10, 30)]),
        ]
        result = merge_multipage_page_level_extractions(
            group={
                "invoice_no": "INV001",
                "pages": [0, 1, 2],
                "extracted_data": page_results[0]["full_data"],
            },
            page_results=page_results,
            batch_id="t5",
        )
        self.assertTrue(result.applied)
        names = [
            i["product_description"]
            for i in extract_items_from_full_data(result.extracted_data)
        ]
        self.assertIn("GOOD1", names)
        self.assertIn("GOOD3", names)
        self.assertNotIn("Authorized Signatory", names)
        self.assertNotIn("Grand Total", names)
        # Bad page must not force combined OCR when other pages are valid
        self.assertFalse(result.needs_combined_fallback)

    def test_scenario6_duplicate_product_aggregation(self):
        page_results = [
            _page("INV001", [_item("Product A", 10, 5, 50, batch="B1")]),
            _page(None, [_item("Product B", 20, 5, 100, batch="B2")]),
            _page(None, [_item("Product A", 5, 5, 25, batch="B1")]),
        ]
        result = merge_multipage_page_level_extractions(
            group={
                "invoice_no": "INV001",
                "pages": [0, 1, 2],
                "extracted_data": page_results[0]["full_data"],
            },
            page_results=page_results,
            batch_id="t6",
        )
        items = extract_items_from_full_data(result.extracted_data)
        by_name = {i["product_description"]: i for i in items}
        self.assertEqual(float(by_name["Product A"]["quantity"]), 15.0)
        self.assertEqual(float(by_name["Product B"]["quantity"]), 20.0)
        # Similar but different name must not merge
        page_results2 = [
            _page("INV001", [_item("AMLOPIN 5", 10, 5, 50, batch="X")]),
            _page(None, [_item("AMLOPIN 10", 5, 5, 25, batch="X")]),
        ]
        result2 = merge_multipage_page_level_extractions(
            group={
                "invoice_no": "INV001",
                "pages": [0, 1],
                "extracted_data": page_results2[0]["full_data"],
            },
            page_results=page_results2,
        )
        names2 = [
            i["product_description"]
            for i in extract_items_from_full_data(result2.extracted_data)
        ]
        self.assertEqual(sorted(names2), ["AMLOPIN 10", "AMLOPIN 5"])


class TestCombinedFallbackGate(unittest.TestCase):
    def test_weaker_combined_does_not_overwrite(self):
        page_level = {
            "data": {
                "line_items": {
                    "items": [
                        _item("A", 1, 10, 10),
                        _item("B", 1, 10, 10),
                        _item("C", 1, 10, 10),
                    ],
                    "count": 3,
                }
            }
        }
        combined = {
            "data": {
                "line_items": {
                    "items": [_item("Authorized Signatory", 18, 1, 1)],
                    "count": 1,
                }
            }
        }
        self.assertFalse(combined_result_beats_page_level(page_level, combined))
        chosen, used = apply_controlled_combined_ocr_fallback(
            group={"invoice_no": "INV001"},
            page_level_data=page_level,
            combined_data=combined,
        )
        self.assertFalse(used)
        self.assertEqual(
            len(extract_items_from_full_data(chosen)),
            3,
        )

    def test_stronger_combined_can_replace_empty_page_level(self):
        page_level = {"data": {"line_items": {"items": [], "count": 0}}}
        combined = {
            "data": {
                "line_items": {
                    "items": [_item("A", 1, 10, 10), _item("B", 2, 10, 20)],
                    "count": 2,
                }
            }
        }
        self.assertTrue(combined_result_beats_page_level(page_level, combined))

    def test_equal_count_valid_but_different_keeps_page_level(self):
        """Deployment gate: both valid & usable, different products → page-level wins."""
        page_level = {
            "data": {
                "invoice_summary": {
                    "invoice_no": "INV001",
                    "total": "100.00",
                    "vendor": "STOCKIST A",
                    "customer": "HOSPITAL X",
                },
                "line_items": {
                    "items": [
                        _item("ZYTANIX 5 TAB", 4, 25, 100),
                        _item("LINID TAB", 2, 50, 100),
                    ],
                    "count": 2,
                },
            }
        }
        combined = {
            "data": {
                "invoice_summary": {
                    "invoice_no": "INV001",
                    "total": "999.00",  # different metadata/totals
                    "vendor": "OTHER VENDOR",
                    "customer": "OTHER CUSTOMER",
                },
                "line_items": {
                    "items": [
                        _item("AMLOPIN 5", 1, 40, 40),
                        _item("SUPRADYN", 3, 20, 60),
                    ],
                    "count": 2,
                },
            }
        }
        self.assertFalse(combined_result_beats_page_level(page_level, combined))
        chosen, used = apply_controlled_combined_ocr_fallback(
            group={"invoice_no": "INV001"},
            page_level_data=page_level,
            combined_data=combined,
        )
        self.assertFalse(used)
        names = [
            i["product_description"] for i in extract_items_from_full_data(chosen)
        ]
        self.assertEqual(sorted(names), ["LINID TAB", "ZYTANIX 5 TAB"])
        # Metadata/totals from page-level must remain (not combined's)
        summary = chosen["data"]["invoice_summary"]
        self.assertEqual(summary["total"], "100.00")
        self.assertEqual(summary["vendor"], "STOCKIST A")


class TestStockistPathsBypassGenericMerge(unittest.TestCase):
    """Prove stockist handlers `continue` before generic merge in both loops."""

    def _multipage_block_snippet(self, source: str, marker: str) -> str:
        start = source.find(marker)
        self.assertGreater(start, 0, msg=f"marker not found: {marker}")
        # Take a large window covering stockist + generic merge
        return source[start: start + 12000]

    def test_split_and_extract_stockist_continues_before_generic(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app.py"), encoding="utf-8") as fh:
            source = fh.read()
        block = self._multipage_block_snippet(
            source,
            "# Multi-page invoices: stockist-specific Vision merges first",
        )
        # Order markers
        markers = [
            "_bharath_pages_are_duplicate_copies",
            "ocr_suggests_jackson_medicals",
            "_is_pharmacea_link_vendor",
            "_apply_palepu_multipage_vision_line_items",
            "_apply_nic_irp_multipage_line_items",
            "merge_multipage_page_level_extractions",
        ]
        positions = [block.find(m) for m in markers]
        self.assertTrue(all(p >= 0 for p in positions), msg=positions)
        self.assertEqual(positions, sorted(positions))

        # Each stockist path must continue before generic merge is reached
        for stockist_fn in markers[:-1]:
            snip = block[block.find(stockist_fn): block.find(
                "merge_multipage_page_level_extractions")]
            self.assertIn(
                "continue",
                snip,
                msg=f"{stockist_fn} must continue before generic merge",
            )

    def test_generic_merge_does_not_rewrite_invoice_summary_when_base_present(self):
        """Generic merge only replaces line_items; keeps base summary/totals."""
        base = {
            "data": {
                "invoice_summary": {
                    "invoice_no": "JK-100",
                    "total": "5000.00",
                    "vendor": "JACKSON MEDICALS",
                    "tax": "250.00",
                },
                "line_items": {
                    "items": [_item("OLD", 1, 1, 1)],
                    "count": 1,
                },
            }
        }
        page_results = [
            {
                "invoice_no": "JK-100",
                "full_data": {
                    "data": {
                        "line_items": {
                            "items": [_item("AMLOPIN", 2, 10, 20)],
                            "count": 1,
                        }
                    }
                },
            },
            {
                "invoice_no": "JK-100",
                "full_data": {
                    "data": {
                        "line_items": {
                            "items": [_item("SUPRADYN", 1, 15, 15)],
                            "count": 1,
                        }
                    }
                },
            },
        ]
        result = merge_multipage_page_level_extractions(
            group={
                "invoice_no": "JK-100",
                "pages": [0, 1],
                "extracted_data": base,
            },
            page_results=page_results,
        )
        self.assertTrue(result.applied)
        summary = result.extracted_data["data"]["invoice_summary"]
        self.assertEqual(summary["total"], "5000.00")
        self.assertEqual(summary["vendor"], "JACKSON MEDICALS")
        self.assertEqual(summary["tax"], "250.00")


if __name__ == "__main__":
    unittest.main()

