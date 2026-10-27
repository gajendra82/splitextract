"""Agreement rules for the 181-image engine comparison. No live OCR."""

from __future__ import annotations

import unittest

from scripts.compare_za_181_engines import (
    aggregate,
    compare_document,
    compare_quantity_fields,
    normalize_name,
    numbers_equal,
)


class ComparisonRuleTests(unittest.TestCase):
    def test_both_missing_quantity_is_not_an_agreement(self):
        compared = compare_quantity_fields({}, {})
        self.assertEqual(compared, [])

    def test_numeric_formatting_matches_and_different_values_do_not(self):
        self.assertTrue(numbers_equal(10, 10.0))
        self.assertFalse(numbers_equal(10, 11))

    def test_name_normalization_ignores_spacing_only(self):
        self.assertEqual(normalize_name("Liv 52  Syrup"), normalize_name("liv-52 syrup"))
        self.assertNotEqual(normalize_name("Liv 52"), normalize_name("Liv 52 DS"))

    def test_duplicate_product_keeps_first_row(self):
        text = "\n".join(
            [
                "Opening Qty Receipt Qty Issue Qty Closing Qty",
                "Liv 52 Syrup 5 1 2 4",
                "Liv 52 Syrup 9 9 9 9",
            ]
        )
        other = "\n".join(
            [
                "Opening Qty Receipt Qty Issue Qty Closing Qty",
                "Liv 52 Syrup 5 1 2 4",
            ]
        )
        row = compare_document("demo.jpg", text, other)
        self.assertEqual(row["tess_products"], 1)
        self.assertEqual(row["tess_duplicate_products"], 1)
        self.assertEqual(row["shared_products"], 1)
        self.assertEqual(row["qty_agree"], row["qty_compared"])
        self.assertGreater(row["qty_compared"], 0)

    def test_one_sided_product_is_not_counted_correct(self):
        left = "Opening Qty\nLiv 52 Syrup 5 0 0 5"
        right = "Opening Qty\nCystone Tab 3 0 0 3"
        row = compare_document("demo.jpg", left, right)
        self.assertEqual(row["shared_products"], 0)
        self.assertEqual(row["only_tesseract_products"], 1)
        self.assertEqual(row["only_paddle_products"], 1)
        self.assertEqual(row["qty_compared"], 0)
        self.assertFalse(row["exact_document_agreement"])
        categories = {item["category"] for item in row["disagreements"]}
        self.assertIn("missing_row", categories)
        self.assertIn("extra_row", categories)

    def test_pair_layout_does_not_read_pack_size_or_amount_as_quantity(self):
        header = "\n".join(
            [
                "ITEM DESCRIPTION OPENING RECEIPT ISSUE CLOSING",
                "QTY VALUE QTY VALUE QTY VALUE QTY VALUE",
            ]
        )
        left = header + "\nCystone Tab 60'S 68 11160.84 100 16413.00 73 100.00 95 200.00"
        right = header + "\nCystone Tab 60'S 68 11160.84 100 16413.00 73 100.00 96 200.00"
        row = compare_document("pair.jpg", left, right)
        self.assertEqual(row["shared_products"], 1)
        self.assertEqual(row["field_counts"]["opening_qty"]["agree"], 1)
        self.assertEqual(row["field_counts"]["opening_qty"]["disagree"], 0)
        self.assertEqual(row["field_counts"]["closing_qty"]["disagree"], 1)
        self.assertNotEqual(row["field_counts"]["opening_qty"]["agree"], 0)

    def test_apostrophe_pack_and_short_noise_stay_out_of_the_name(self):
        row = compare_document(
            "noise.jpg",
            "Cystone Tab. 60 'S 96 bal 16 80",
            "Cystone Tab. 60 'S 96 - 16 80",
        )
        self.assertEqual(row["shared_products"], 1)
        self.assertEqual(row["only_tesseract_products"], 0)
        self.assertEqual(row["positional_agree"], 3)
        self.assertEqual(row["positional_compared"], 3)

    def test_headerless_numbers_stay_positional(self):
        left = "Bonnisan Drop 30 ML. 25 - 4 21"
        right = "Bonnisan Drop 30 ML. 25 - 4 21"
        row = compare_document("plain.jpg", left, right)
        self.assertEqual(row["shared_products"], 1)
        self.assertEqual(row["qty_compared"], 0)
        self.assertEqual(row["positional_compared"], 3)
        self.assertEqual(row["positional_agree"], 3)

    def test_aggregate_denominators_match_rows(self):
        rows = [
            compare_document(
                "a.jpg",
                "Out Qty Cl Qty\nCystone Tab 4 0 1",
                "Out Qty Cl Qty\nCystone Tab 4 0 2",
            )
        ]
        summary = aggregate(rows)
        self.assertEqual(summary["images"], 1)
        self.assertEqual(summary["unique_images"], 1)
        self.assertEqual(summary["qty_agree"] + (summary["qty_compared"] - summary["qty_agree"]), summary["qty_compared"])
        self.assertLess(summary["quantity_agreement"], 1)


if __name__ == "__main__":
    unittest.main()
