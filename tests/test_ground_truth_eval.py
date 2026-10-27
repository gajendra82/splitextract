"""Tests for source-ground-truth OCR evaluation utilities."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.ground_truth.classify import (
    MODEL_DERIVED,
    PARSER_DERIVED,
    SOURCE_VERIFIED,
    UNKNOWN,
    classify_reference_source,
)
from services.ground_truth.html_table import (
    parse_html_tables,
    packing_hint_from_field_id,
    row_lookup,
)
from services.ground_truth.matching import (
    detect_numeric_concatenation,
    detect_numeric_splitting,
    exact_numeric_match,
    match_field,
    normalize_numeric,
    normalize_text,
)
from services.ground_truth.metrics import aggregate_metrics, compute_iou, document_status
from services.ground_truth.schema import (
    only_source_verified_fields,
    validate_ground_truth_document,
)


class SchemaTests(unittest.TestCase):
    def test_schema_accepts_source_verified(self):
        payload = {
            "document_id": "demo.jpg",
            "page": 1,
            "source": {
                "file": "demo.jpg",
                "source_type": "image",
                "source_verified": True,
            },
            "fields": [
                {
                    "field_id": "q1",
                    "field_type": "quantity",
                    "printed_header": "Issues",
                    "expected_raw": "37",
                    "bbox": None,
                    "bbox_verified": False,
                    "verified": True,
                    "source_verified": True,
                }
            ],
        }
        ok, errors = validate_ground_truth_document(payload)
        self.assertTrue(ok, errors)

    def test_bbox_without_verified_flag_rejected(self):
        payload = {
            "document_id": "demo.jpg",
            "page": 1,
            "source": {"file": "x", "source_type": "image", "source_verified": True},
            "fields": [
                {
                    "field_id": "q1",
                    "field_type": "quantity",
                    "printed_header": "Issues",
                    "expected_raw": "37",
                    "bbox": [0.1, 0.2, 0.3, 0.4],
                    "bbox_verified": False,
                    "verified": True,
                    "source_verified": True,
                }
            ],
        }
        ok, errors = validate_ground_truth_document(payload)
        self.assertFalse(ok)
        self.assertTrue(any("bbox_without_bbox_verified" in e for e in errors))

    def test_model_derived_fields_filtered(self):
        payload = {
            "document_id": "d",
            "source": {"file": "f", "source_verified": True},
            "fields": [
                {
                    "field_id": "a",
                    "field_type": "quantity",
                    "expected_raw": "1",
                    "source_verified": True,
                    "verified": True,
                },
                {
                    "field_id": "b",
                    "field_type": "quantity",
                    "expected_raw": "2",
                    "provenance": "MODEL_DERIVED",
                    "source_verified": True,
                    "verified": True,
                },
            ],
        }
        kept = only_source_verified_fields(payload)
        self.assertEqual([f["field_id"] for f in kept], ["a"])


class ClassifyTests(unittest.TestCase):
    def test_tesseract_qa_is_model_derived(self):
        self.assertEqual(
            classify_reference_source(
                {"source": "ocr", "notes": ["OCR used tesseract psm 6."]}
            ),
            MODEL_DERIVED,
        )

    def test_embedded_is_source_verified(self):
        self.assertEqual(
            classify_reference_source({"source": "embedded"}),
            SOURCE_VERIFIED,
        )

    def test_parser_derived(self):
        self.assertEqual(
            classify_reference_source({"source": "parser"}),
            PARSER_DERIVED,
        )

    def test_unknown_default(self):
        self.assertEqual(classify_reference_source({}), UNKNOWN)


class NumericMatchTests(unittest.TestCase):
    def test_exact_numeric(self):
        self.assertTrue(exact_numeric_match("37", "37"))
        self.assertTrue(exact_numeric_match("37.0", "37"))
        self.assertFalse(exact_numeric_match("37", "3752"))
        self.assertFalse(exact_numeric_match("37", "3.7"))

    def test_concatenation_detection(self):
        self.assertTrue(detect_numeric_concatenation("37", "3752"))
        self.assertFalse(detect_numeric_concatenation("37", "37"))
        self.assertFalse(detect_numeric_concatenation("37", "52"))

    def test_splitting_detection(self):
        self.assertTrue(detect_numeric_splitting("3752", ["37", "52"]))
        self.assertFalse(detect_numeric_splitting("37", ["37"]))

    def test_normalize_helpers(self):
        self.assertEqual(normalize_numeric("19,892.88"), 19892.88)
        self.assertEqual(normalize_text("  Foo\nBar "), "foo bar")


class ColumnAwareTests(unittest.TestCase):
    def test_html_table_column_match(self):
        html = (
            "<table><tr><td>PRODUCT</td><td>Issues</td><td>Free</td><td>Goods</td></tr>"
            "<tr><td>ITEM A</td><td>37</td><td>33</td><td>52</td></tr></table>"
        )
        cells = parse_html_tables(html)
        hit = row_lookup(cells, product_key="ITEM A", header="Issues")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["value"], "37")
        result = match_field(
            {
                "field_id": "issues",
                "field_type": "quantity",
                "printed_header": "Issues",
                "expected_raw": "37",
                "product_key": "ITEM A",
            },
            ocr_text=html,
            table_cells=cells,
        )
        self.assertTrue(result["correct"])

    def test_rowspan_colspan_sales_closing_alignment(self):
        """Regression: VL multi-row headers must not shift Sales into Closing."""
        html = (
            "<table>"
            '<tr><td rowspan="2">Product Name</td><td rowspan="2">Strength</td>'
            '<td rowspan="2">Op. Stock</td><td colspan="2">Purchase</td>'
            '<td rowspan="2">Purchase Rtn.Qty</td>'
            '<td rowspan="2">Purchase Ret Free Qty</td>'
            '<td rowspan="2">Sales Ret Qty</td>'
            '<td rowspan="2">Sales Ret Free Qty</td>'
            '<td colspan="2">Sales</td>'
            '<td rowspan="2">CL.Stock As On</td></tr>'
            "<tr><td>Qty</td><td>Free</td><td>Qty</td><td>Free</td></tr>"
            "<tr><td>GASEX ELAICHI FLA SYRUP</td><td>200 ML</td><td>49</td>"
            "<td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td>"
            "<td>2</td><td>0</td><td>47</td></tr>"
            "<tr><td>LIV-52 SYRUP</td><td>200 ML</td><td>615</td>"
            "<td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td>"
            "<td>123</td><td>0</td><td>492</td></tr>"
            "</table>"
        )
        cells = parse_html_tables(html)
        headers = [c["value"] for c in cells if c.get("is_header")]
        self.assertIn("Sales Qty", headers)
        self.assertIn("CL.Stock As On", headers)
        gasex_sales = row_lookup(cells, product_key="GASEX ELAICHI", header="Sales")
        gasex_cl = row_lookup(
            cells, product_key="GASEX ELAICHI", header="Cl. Stock As On"
        )
        liv_sales = row_lookup(cells, product_key="LIV-52 SYRUP", header="Sales")
        liv_cl = row_lookup(
            cells, product_key="LIV-52 SYRUP", header="Cl. Stock As On"
        )
        self.assertEqual(gasex_sales["value"], "2")
        self.assertEqual(gasex_cl["value"], "47")
        self.assertEqual(liv_sales["value"], "123")
        self.assertEqual(liv_cl["value"], "492")
        # Blank Op/Cl cells are MISSING, not silent COLUMN_CONFUSION.
        blank = (
            "<table>"
            '<tr><td rowspan="2">Product Name</td><td rowspan="2">Op. Stock</td>'
            '<td colspan="2">Sales</td><td rowspan="2">CL.Stock As On</td></tr>'
            "<tr><td>Qty</td><td>Free</td></tr>"
            "<tr><td>AACTARIL SOAP</td><td></td><td></td><td></td><td></td></tr>"
            "</table>"
        )
        result = match_field(
            {
                "field_id": "aact_op",
                "field_type": "quantity",
                "printed_header": "Op. Stock",
                "expected_raw": "69",
                "product_key": "AACTARIL SOAP",
            },
            ocr_text=blank,
        )
        self.assertFalse(result["correct"])
        self.assertEqual(result["error_type"], "MISSING")
        self.assertFalse(result["silent_wrong"])

    def test_missing_uom_column_shifts_qty_headers(self):
        html = (
            "<table>"
            '<tr><td colspan="2">ITEM DESCRIPTION</td><td>OPENING</td>'
            "<td>RECEIPT</td><td>ISSUE</td><td>CLOSING</td><td>M.EXP</td></tr>"
            "<tr><td>AACTARIL SOAP</td><td>75GM</td><td>PKT</td><td>72</td>"
            "<td>-</td><td>-</td><td>72 12/27</td></tr>"
            "<tr><td>ABANA TABLET</td><td>60 NOS</td><td>Bot</td><td>100</td>"
            "<td>-</td><td>3</td><td>97 4/28</td></tr>"
            "</table>"
        )
        cells = parse_html_tables(html)
        self.assertEqual(
            row_lookup(cells, product_key="AACTARIL SOAP", header="OPENING")["value"],
            "72",
        )
        self.assertEqual(
            row_lookup(cells, product_key="AACTARIL SOAP", header="CLOSING")["value"],
            "72",
        )
        self.assertEqual(
            row_lookup(cells, product_key="ABANA TABLET", header="ISSUE")["value"],
            "3",
        )
        self.assertEqual(
            row_lookup(cells, product_key="ABANA TABLET", header="CLOSING")["value"],
            "97",
        )
        self.assertEqual(
            row_lookup(cells, product_key="AACTARIL SOAP", header="M.EXP")["value"],
            "12/27",
        )

    def test_sr_no_column_and_early_exp(self):
        html = (
            "<table><tr><td>Sr. No.</td><td>Product Name</td><td>Packing</td>"
            "<td>OpStk</td><td>Sale Qty</td><td>Clstk</td><td>Early Exp.</td></tr>"
            "<tr><td>2</td><td>ACTARIL SOAP</td><td>75GM</td><td>64.0</td>"
            "<td>20.0</td><td>44.0</td><td>04/2029</td></tr></table>"
        )
        cells = parse_html_tables(html)
        hit = row_lookup(cells, product_key="ACTARIL SOAP", header="Early Exp.")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["value"], "04/2029")
        self.assertEqual(
            row_lookup(cells, product_key="ACTARIL SOAP", header="OpStk")["value"],
            "64.0",
        )

    def test_packing_hint_and_total_row_preference(self):
        html = (
            "<table><tr><td>PRODUCT NAME</td><td>PACKING</td>"
            "<td>Op.Bal. Qty.</td><td>Closing Balance</td></tr>"
            "<tr><td>LIV-52 SYP.</td><td>100ML</td><td>164</td><td>140</td></tr>"
            "<tr><td>LIV-52 SYP</td><td>200ML</td><td>120</td><td>204</td></tr>"
            "<tr><td>Total sales in this period :</td><td></td>"
            "<td>22973</td><td>22973</td></tr>"
            "<tr><td>Total closing stock (Sales value) :</td><td></td>"
            "<td>89086</td><td>89086</td></tr>"
            "</table>"
        )
        cells = parse_html_tables(html)
        self.assertEqual(packing_hint_from_field_id("liv200_op"), "200")
        self.assertEqual(
            row_lookup(
                cells,
                product_key="LIV-52 SYP.",
                header="Op.Bal. Qty.",
                packing_hint="200",
            )["value"],
            "120",
        )
        self.assertEqual(
            row_lookup(
                cells, product_key="TOTAL", header="Closing Balance"
            )["value"],
            "89086",
        )
        # TOTAL + Closing amount prefers the closing-stock banner row.
        html_amt = (
            "<table><tr><td>Product Name</td><td>Sales amount</td>"
            "<td>Closing amount</td></tr>"
            "<tr><td>Total sales in this period :</td><td>22973</td><td>22973</td></tr>"
            "<tr><td>Total closing stock (Sales value) :</td>"
            "<td>89086</td><td>89086</td></tr></table>"
        )
        cells_amt = parse_html_tables(html_amt)
        self.assertEqual(
            row_lookup(
                cells_amt, product_key="TOTAL", header="Closing amount"
            )["value"],
            "89086",
        )

    def test_total_row_without_uom_not_shifted(self):
        html = (
            "<table>"
            '<tr><td colspan="2">ITEM DESCRIPTION</td><td>OPENING</td>'
            "<td>RECEIPT</td><td>ISSUE</td><td>CLOSING</td><td>M.EXP</td></tr>"
            "<tr><td>AACTARIL SOAP</td><td>75GM</td><td>PKT</td><td>72</td>"
            "<td>-</td><td>-</td><td>72 12/27</td></tr>"
            "<tr><td>TOTAL</td><td></td><td>503184</td><td>277797</td>"
            "<td>110666</td><td>670315</td><td></td></tr>"
            "</table>"
        )
        cells = parse_html_tables(html)
        self.assertEqual(
            row_lookup(cells, product_key="TOTAL", header="OPENING")["value"],
            "503184",
        )
        self.assertEqual(
            row_lookup(cells, product_key="TOTAL", header="ISSUE")["value"],
            "110666",
        )

    def test_pack_column_holds_opening_and_fr_holds_sal_cb(self):
        html = (
            "<table><tr><td>SNO</td><td>ITEM</td><td>DESCRIPTION</td>"
            "<td>PACK</td><td>OB</td><td>PUR</td><td>FR</td><td>SAL</td>"
            "<td>FR</td><td>CB</td><td>RATE</td><td>VALUE</td></tr>"
            "<tr><td>4</td><td>BONISAN 200ML</td><td>200ML</td><td>241</td>"
            "<td>0</td><td>0</td><td>0</td><td>83</td><td>2</td><td>156</td>"
            "<td>114.28</td><td>17827.68</td></tr>"
            "<tr><td>13</td><td>CYSTONE TAB</td><td>PC</td><td>27</td>"
            "<td>0</td><td>0</td><td>14</td><td>0</td><td>13</td><td>161.57</td>"
            "<td>2100.41</td><td></td></tr>"
            "<tr><td>50</td><td>LIV-52 SYP 200ML</td><td>200ML</td><td>35</td>"
            "<td>0</td><td>0</td><td>33</td><td>0</td><td>2</td><td>178.28</td>"
            "<td>356.56</td><td></td></tr>"
            "</table>"
        )
        cells = parse_html_tables(html)
        self.assertEqual(
            row_lookup(cells, product_key="BONISAN 200ML", header="OB")["value"],
            "241",
        )
        self.assertEqual(
            row_lookup(cells, product_key="CYSTONE TAB", header="SAL")["value"],
            "14",
        )
        self.assertEqual(
            row_lookup(cells, product_key="CYSTONE TAB", header="CB")["value"],
            "13",
        )
        self.assertEqual(
            row_lookup(cells, product_key="LIV-52 SYP 200ML", header="OB")["value"],
            "35",
        )
        self.assertEqual(
            row_lookup(cells, product_key="LIV-52 SYP 200ML", header="CB")["value"],
            "2",
        )

    def test_header_alias_cl_stock_as_on(self):
        html = (
            "<table><tr><td>Product Name</td><td>CL.Stock As On</td></tr>"
            "<tr><td>X</td><td>1</td></tr></table>"
        )
        result = match_field(
            {
                "field_id": "h_cl",
                "field_type": "header",
                "printed_header": "Cl. Stock As On",
                "expected_raw": "Cl. Stock As On",
            },
            ocr_text=html,
        )
        self.assertTrue(result["correct"])

    def test_days_in_closing_shifts_qty_columns_right(self):
        """Liquidation Days under Closing ⇒ Receipt/Issues/Closing realign."""
        html = (
            "<table>"
            '<tr><td rowspan="2">Product Name</td><td rowspan="2">Packing</td>'
            '<td rowspan="2">Opening</td><td rowspan="2">Receipt</td>'
            '<td colspan="3">Goods</td><td>Stocks from</td></tr>'
            "<tr><td>Issues Repl.</td><td>Issue</td><td>Closing</td>"
            "<td>Liquidation</td></tr>"
            "<tr><td>LIV-52 SYP</td><td>100ML</td><td>70</td><td>6</td>"
            "<td>64</td><td></td><td>142 Days</td><td></td></tr>"
            "</table>"
        )
        cells = parse_html_tables(html)
        self.assertEqual(
            row_lookup(cells, product_key="LIV-52 SYP", header="Receipt")["value"],
            "70",
        )
        self.assertEqual(
            row_lookup(cells, product_key="LIV-52 SYP", header="Issues")["value"],
            "6",
        )
        self.assertEqual(
            row_lookup(cells, product_key="LIV-52 SYP", header="Closing")["value"],
            "64",
        )

    def test_garbled_sales_qty_header_token_match(self):
        html = (
            "<table><tr><td>Sr. Product Name</td><td>Pack</td>"
            r"<td>0g\n0tv</td><td>0g\n0tv</td><td>Sales\n0tv</td>"
            r"<td>Inst\n0tv</td><td>Closure\n0tv</td></tr>"
            "<tr><td>1 ITEM</td><td>60</td><td>1</td><td>2</td><td>3</td>"
            "<td>0</td><td>4</td></tr></table>"
        )
        sales = match_field(
            {
                "field_id": "h_sales",
                "field_type": "header",
                "printed_header": "Sales Qty",
                "expected_raw": "Sales Qty",
            },
            ocr_text=html,
        )
        self.assertTrue(sales["correct"])
        # Bare OCR garbage must not invent Op Qty.
        op = match_field(
            {
                "field_id": "h_op",
                "field_type": "header",
                "printed_header": "Op Qty",
                "expected_raw": "Op Qty",
            },
            ocr_text=html,
        )
        self.assertFalse(op["correct"])
        self.assertEqual(op["error_type"], "MISSING")

    def test_cl_stock_header_does_not_match_op_stock_token(self):
        html = (
            "<table><tr><td>Product Name</td><td>Op. Stock</td>"
            "<td>CL.Stock As On</td></tr>"
            "<tr><td>X</td><td>1</td><td>2</td></tr></table>"
        )
        result = match_field(
            {
                "field_id": "h_cl",
                "field_type": "header",
                "printed_header": "Cl. Stock As On",
                "expected_raw": "Cl. Stock As On",
            },
            ocr_text=html,
        )
        self.assertTrue(result["correct"])
        self.assertNotEqual(normalize_text(result["predicted_raw"]), "op. stock")

    def test_concatenation_not_accepted_as_column_value(self):
        html = (
            "<table><tr><td>PRODUCT</td><td>Issues</td><td>Goods</td></tr>"
            "<tr><td>ITEM A</td><td>3752</td><td></td></tr></table>"
        )
        result = match_field(
            {
                "field_id": "issues",
                "field_type": "quantity",
                "printed_header": "Issues",
                "expected_raw": "37",
                "product_key": "ITEM A",
            },
            ocr_text=html,
        )
        self.assertFalse(result["correct"])
        self.assertEqual(result["error_type"], "NUMERIC_CONCATENATION")
        self.assertTrue(result["silent_wrong"])


class MetricsTests(unittest.TestCase):
    def test_iou(self):
        self.assertAlmostEqual(compute_iou([0, 0, 2, 2], [1, 1, 3, 3]), 1 / 7)
        self.assertIsNone(compute_iou([0, 0, 1], [0, 0, 1, 1]))

    def test_precision_recall_silent_wrong(self):
        matches = [
            {"correct": True, "silent_wrong": False, "critical": False, "predicted_raw": "1", "field_type": "quantity", "priority": "high"},
            {"correct": False, "silent_wrong": True, "critical": True, "predicted_raw": "3752", "field_type": "quantity", "error_type": "NUMERIC_CONCATENATION", "priority": "high"},
            {"correct": False, "silent_wrong": False, "critical": True, "predicted_raw": None, "field_type": "quantity", "error_type": "MISSING", "priority": "high"},
        ]
        metrics = aggregate_metrics(matches)
        self.assertAlmostEqual(metrics["quantity_accuracy"], 1 / 3)
        self.assertAlmostEqual(metrics["silent_wrong_rate"], 1 / 3)
        self.assertEqual(document_status(matches), "PARTIALLY_CORRECT")


class CheckpointTests(unittest.TestCase):
    def test_verification_progress_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verification_progress.json"
            payload = {"completed_field_keys": ["a::f1"], "decisions": {}}
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("a::f1", loaded["completed_field_keys"])


if __name__ == "__main__":
    unittest.main()
