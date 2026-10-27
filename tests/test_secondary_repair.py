"""Evidence-gated secondary OCR repairs for residual GT-20 cases."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from services.ground_truth.matching import match_field
from services.ground_truth.schema import only_source_verified_fields
from services.ocr import secondary_repair as sr

ROOT = Path(__file__).resolve().parents[1]
DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
GT = ROOT / "phase4" / "ground_truth_50" / "per_document"
VL = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"

DOC_1078 = "0000701078_2026_08_ZL_03_362_05092026080346.jpeg"
DOC_0275 = "0000700275_2026_08_ZL_18_374_04092026171615.pdf"
DOC_1151 = "0000701151_2026_08_ZL_14_361_03092026025650.jpg"
DOC_0209 = "0000700209_2026_08_ZL_18_374_04092026060313.pdf"
DOC_0124 = "0000700124_2026_08_ZL_06_521_02092026204703.pdf"


def _vl_text(doc: str) -> str:
    payload = json.loads((VL / f"{doc}.json").read_text(encoding="utf-8"))
    return payload["pages"][0].get("text") or ""


def _gt_fields(doc: str):
    gt = json.loads((GT / f"{doc}.json").read_text(encoding="utf-8"))
    return only_source_verified_fields(gt)


def _field(doc: str, field_id: str):
    for f in _gt_fields(doc):
        if f.get("field_id") == field_id:
            return f
    raise AssertionError(f"missing field {field_id} on {doc}")


@unittest.skipUnless((DATASET / DOC_1078).is_file(), "1078 source missing")
@unittest.skipUnless((VL / f"{DOC_1078}.json").is_file(), "1078 VL missing")
class HimalayaSecondaryRepairTests(unittest.TestCase):
    def test_1078_closing_and_expiry_from_source_ocr(self):
        text, cells, report = sr.apply_secondary_repairs(DOC_1078, _vl_text(DOC_1078))
        types = {r["type"] for r in report.repairs}
        self.assertIn("himalaya_row_anchor_ocr", types)
        close = match_field(
            _field(DOC_1078, "aact_close"), ocr_text=text, table_cells=cells
        )
        exp = match_field(
            _field(DOC_1078, "talekt_exp"), ocr_text=text, table_cells=cells
        )
        self.assertTrue(close.get("correct"), close)
        self.assertTrue(exp.get("correct"), exp)
        self.assertEqual(close.get("predicted_raw"), "69")
        self.assertEqual(exp.get("predicted_raw"), "12")


@unittest.skipUnless((DATASET / DOC_0275).is_file(), "0275 source missing")
@unittest.skipUnless((VL / f"{DOC_0275}.json").is_file(), "0275 VL missing")
class LivStrikeSecondaryRepairTests(unittest.TestCase):
    def test_0275_printed_closing_not_prior_row_bleed(self):
        text, cells, report = sr.apply_secondary_repairs(DOC_0275, _vl_text(DOC_0275))
        self.assertTrue(
            any(r.get("type") == "liv_printed_closing_ocr" for r in report.repairs),
            report.repairs,
        )
        m = match_field(_field(DOC_0275, "liv100_cl"), ocr_text=text, table_cells=cells)
        self.assertTrue(m.get("correct"), m)
        self.assertEqual(m.get("predicted_raw"), "37")


@unittest.skipUnless((DATASET / DOC_1151).is_file(), "1151 source missing")
@unittest.skipUnless((VL / f"{DOC_1151}.json").is_file(), "1151 VL missing")
class TotalFooterSecondaryRepairTests(unittest.TestCase):
    def test_1151_total_footer_ocr_without_corrupting_rows(self):
        text, cells, report = sr.apply_secondary_repairs(DOC_1151, _vl_text(DOC_1151))
        self.assertTrue(
            any(r.get("type") == "total_footer_ocr" for r in report.repairs),
            report.repairs,
        )
        # Must not apply himalaya short-layout digit fallback (54→1 regression).
        self.assertFalse(
            any(r.get("type") == "himalaya_row_anchor_ocr" for r in report.repairs),
            report.repairs,
        )
        tot_op = match_field(
            _field(DOC_1151, "tot_op"), ocr_text=text, table_cells=cells
        )
        tot_cl = match_field(
            _field(DOC_1151, "tot_cl"), ocr_text=text, table_cells=cells
        )
        aact_cl = match_field(
            _field(DOC_1151, "aact_cl"), ocr_text=text, table_cells=cells
        )
        self.assertTrue(tot_op.get("correct"), tot_op)
        self.assertTrue(tot_cl.get("correct"), tot_cl)
        self.assertTrue(aact_cl.get("correct"), aact_cl)
        self.assertEqual(aact_cl.get("predicted_raw"), "54")


@unittest.skipUnless((DATASET / DOC_0209).is_file(), "0209 source missing")
@unittest.skipUnless((VL / f"{DOC_0209}.json").is_file(), "0209 VL missing")
class UnresolvedSecondaryRepairTests(unittest.TestCase):
    def test_0209_blank_aactaril_stays_missing(self):
        text, cells, report = sr.apply_secondary_repairs(DOC_0209, _vl_text(DOC_0209))
        self.assertFalse(
            any(r.get("corrected") for r in report.repairs if "aact" in str(r).lower()),
            report.repairs,
        )
        op = match_field(_field(DOC_0209, "aact_op"), ocr_text=text, table_cells=cells)
        cl = match_field(_field(DOC_0209, "aact_cl"), ocr_text=text, table_cells=cells)
        self.assertEqual(op.get("error_type"), "MISSING")
        self.assertEqual(cl.get("error_type"), "MISSING")

    @unittest.skipUnless((DATASET / DOC_0124).is_file(), "0124 source missing")
    def test_0124_unreadable_op_header_stays_missing(self):
        text, cells, _report = sr.apply_secondary_repairs(DOC_0124, _vl_text(DOC_0124))
        m = match_field(_field(DOC_0124, "h_op"), ocr_text=text, table_cells=cells)
        self.assertEqual(m.get("error_type"), "MISSING")


@unittest.skipUnless(DATASET.is_dir(), "dataset root missing")
class SecondaryRepairNoRegressionLayoutTests(unittest.TestCase):
    """Secondary OCR must not break already-correct fields on other layouts."""

    # Diverse GT-20 docs that were FULLY_CORRECT without needing secondary fixes.
    NON_TARGET_DOCS = (
        "0000700084_2026_08_ZL_06_8236_03092026155136.pdf",  # PDF stock
        "0000700308_2026_08_ZL_18_8175_03092026072320.jpg",  # Days-in-Closing layout
        "0000700406_2026_08_ZL_10_384_05092026140711.jpg",  # dense photo table
    )

    def test_non_target_fully_correct_docs_stay_correct(self):
        for doc in self.NON_TARGET_DOCS:
            with self.subTest(doc=doc):
                if not (DATASET / doc).is_file() or not (VL / f"{doc}.json").is_file():
                    self.skipTest(f"fixture missing: {doc}")
                text, cells, report = sr.apply_secondary_repairs(doc, _vl_text(doc))
                # These layouts must not receive himalaya/LIV/TOTAL surgical writes
                # unless VL was already missing a TOTAL qty row (none of these are).
                self.assertFalse(
                    any(r.get("corrected") for r in report.repairs),
                    report.repairs,
                )
                for f in _gt_fields(doc):
                    m = match_field(f, ocr_text=text, table_cells=cells)
                    self.assertTrue(
                        m.get("correct"),
                        f"{doc} {f.get('field_id')}: {m}",
                    )


class SecondaryRepairSafetyUnitTests(unittest.TestCase):
    def test_no_source_image_leaves_vl_unchanged(self):
        html = (
            "<table><tr><td>PRODUCT NAME</td><td>Closing Balance</td></tr>"
            "<tr><td>AACTARIL SOAP</td><td>89</td></tr></table>"
        )
        with mock.patch.object(sr, "resolve_source_path", return_value=None):
            text, cells, report = sr.apply_secondary_repairs("missing.doc", html)
        self.assertEqual(text, html)
        self.assertEqual(report.unresolved[0]["reason"], "source_image_unavailable")
        self.assertTrue(any(c.get("value") == "89" for c in cells))

    def test_himalaya_gated_without_expiry_breakage_header(self):
        """Short himalaya layouts must not run Closing/Expiry digit repairs."""
        from services.ground_truth.html_table import parse_html_tables

        html = (
            "<table><tr><td>PRODUCT NAME</td><td>PACKING</td>"
            "<td>Op.Bal. Qty.</td><td>Issue Qty.</td><td>Closing Balance</td></tr>"
            "<tr><td>AACTARIL SOAP</td><td>75GM</td><td>68</td><td>14</td>"
            "<td>54</td></tr></table>"
        )
        cells = parse_html_tables(html)
        report = sr.SecondaryRepairReport(document_id="short")
        out = sr._repair_himalaya_qty_cells(
            image=None, cells=cells, report=report, page_text="AACTARILSOAP 75GM 68 0 68 14 54 0"
        )
        close = next(
            c
            for c in out
            if c.get("product_key") == "AACTARIL SOAP"
            and c.get("header") == "Closing Balance"
        )
        self.assertEqual(close.get("value"), "54")
        self.assertEqual(report.repairs, [])

    def test_feature_flag_defaults_off(self):
        html = "<table><tr><td>PRODUCT</td><td>1</td></tr></table>"
        with mock.patch.dict("os.environ", {"OCR_SECONDARY_REPAIR": "false"}, clear=False):
            text, _cells, report = sr.maybe_apply_secondary_repairs("x.pdf", html)
        self.assertEqual(text, html)
        self.assertEqual(report.unresolved[0]["reason"], "secondary_repair_disabled")

    def test_flag_truthiness_matrix(self):
        cases = [
            (None, False),
            ("", False),
            ("false", False),
            ("0", False),
            ("no", False),
            ("off", False),
            ("true", True),
            ("TRUE", True),
            ("1", True),
            ("yes", True),
            ("on", True),
        ]
        for raw, expect in cases:
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {}, clear=False):
                    if raw is None:
                        os.environ.pop("OCR_SECONDARY_REPAIR", None)
                    else:
                        os.environ["OCR_SECONDARY_REPAIR"] = raw
                    self.assertEqual(sr.secondary_repair_enabled(), expect)

    def test_flag_off_does_not_invoke_secondary_ocr(self):
        html = "<table><tr><td>PRODUCT NAME</td><td>1</td></tr></table>"
        with mock.patch.dict("os.environ", {"OCR_SECONDARY_REPAIR": "false"}):
            with mock.patch.object(sr, "apply_secondary_repairs") as apply:
                with mock.patch.object(sr, "_ocr") as ocr:
                    text, _, report = sr.maybe_apply_secondary_repairs("x.pdf", html)
        apply.assert_not_called()
        ocr.assert_not_called()
        self.assertEqual(text, html)
        self.assertEqual(report.unresolved[0]["reason"], "secondary_repair_disabled")

    def test_pipeline_exception_preserves_original_text(self):
        from services.document_intelligence.pipeline import _maybe_secondary_repair_bundle
        from services.ocr.base import OCRResult
        from services.ocr.orchestrator import OCRBundle

        original = "<table><tr><td>KEEP</td><td>42</td></tr></table>"
        bundle = OCRBundle(
            primary=OCRResult(text=original, engine="t", metadata={}),
            metadata={},
        )
        with mock.patch.dict("os.environ", {"OCR_SECONDARY_REPAIR": "true"}):
            with mock.patch.object(
                sr, "maybe_apply_secondary_repairs", side_effect=RuntimeError("boom")
            ):
                _maybe_secondary_repair_bundle(
                    bundle, document_id="x.pdf", image_bytes=b"abc"
                )
        self.assertEqual(bundle.primary.text, original)
        self.assertIn("boom", bundle.metadata.get("secondary_repair_error", ""))

    def test_observability_report_redacts_ocr_snippets(self):
        report = sr.SecondaryRepairReport(document_id="d")
        report.repairs.append(
            {
                "type": "total_footer_ocr",
                "corrected": True,
                "line": "SECRET PRODUCT LINE 69",
                "raw": "TOTAL 834578 ...",
            }
        )
        report.unresolved.append(
            {"reason": "x", "line": "secret", "digits": ["1", "2"], "candidates": ["37"]}
        )
        public = report.to_dict(redact_snippets=True)
        self.assertNotIn("line", public["repairs"][0])
        self.assertNotIn("raw", public["repairs"][0])
        self.assertEqual(public["repairs"][0]["type"], "total_footer_ocr")
        self.assertNotIn("line", public["unresolved"][0])
        self.assertNotIn("digits", public["unresolved"][0])
        full = report.to_dict(redact_snippets=False)
        self.assertEqual(full["repairs"][0]["line"], "SECRET PRODUCT LINE 69")

    def test_production_core_does_not_run_secondary_ocr_engine(self):
        """Core may consume DI OCR HTML via override, but must not run secondary OCR.

        Gating / Tesseract secondary repair stays in DocumentIntelligencePipeline.
        """
        import services.sales_statement_extractor as sse

        src = Path(sse.__file__).read_text(encoding="utf-8", errors="ignore")
        core = src.split("def extract_sales_statement_core")[1][:5000]
        self.assertNotIn("maybe_apply_secondary_repairs", core)
        self.assertNotIn("apply_secondary_repairs", core)
        self.assertNotIn("secondary_repair_enabled", core)
        # Allowed: statement_from_ocr_html when DI override is present.
        self.assertIn("statement_from_ocr_html", core)

    def test_unrelated_sales_total_footer_not_fabricated(self):
        """SSA Opening/Closing layout + sales TOTAL digits must not invent qty TOTAL."""
        from services.ground_truth.html_table import parse_html_tables
        from PIL import Image

        html = (
            "<table><tr><td>Product Name</td><td>Opening</td><td>Closing</td></tr>"
            "<tr><td>AACTARIL SOAP</td><td></td><td></td></tr></table>"
        )
        cells = parse_html_tables(html)
        report = sr.SecondaryRepairReport(document_id="ssa")
        image = Image.new("RGB", (200, 200), "white")
        fake_page = "TOTAL SALES VALUE 200429 00\nTOTAL 834578 60 119 45 775274"
        out_cells, out_text = sr._repair_total_row(
            image, cells, html, report, page_text=fake_page
        )
        self.assertFalse(any(r.get("type") == "total_footer_ocr" for r in report.repairs))
        self.assertTrue(
            any(
                u.get("reason") == "total_footer_layout_not_himalaya_qty"
                for u in report.unresolved
            ),
            report.unresolved,
        )
        self.assertFalse(
            any(
                (not c.get("is_header"))
                and str(c.get("product_key") or "").upper() == "TOTAL"
                and str(c.get("value") or "").isdigit()
                for c in out_cells
            )
        )
        self.assertEqual(out_text, html)

    def test_ambiguous_header_layout_cannot_map_footer_digits(self):
        """Even with Op.Bal layout, sales-value TOTAL context is rejected."""
        from services.ground_truth.html_table import parse_html_tables
        from PIL import Image

        html = (
            "<table><tr><td>PRODUCT NAME</td><td>PACKING</td>"
            "<td>Op.Bal. Qty.</td><td>Receipt Qty.</td><td>Total Qty.</td>"
            "<td>Issue Qty.</td><td>Closing Balance</td></tr>"
            "<tr><td>AACTARIL SOAP</td><td>75GM</td><td>68</td><td>0</td>"
            "<td>68</td><td>14</td><td>54</td></tr></table>"
        )
        cells = parse_html_tables(html)
        report = sr.SecondaryRepairReport(document_id="sales_total")
        image = Image.new("RGB", (400, 400), "white")
        # Bottom OCR returns sales narrative; page text also sales-context.
        sales = "TOTAL closing stock (Sales value): 163170 00 00 00 200429"
        with mock.patch.object(sr, "_ocr", return_value=sales):
            out_cells, out_text = sr._repair_total_row(
                image, cells, html, report, page_text=sales
            )
        self.assertFalse(any(r.get("corrected") for r in report.repairs))
        self.assertFalse(
            any(
                str(c.get("product_key") or "").upper() == "TOTAL"
                and str(c.get("value") or "").isdigit()
                for c in out_cells
                if not c.get("is_header")
            )
        )

    def test_blank_aactaril_cells_not_filled_from_noise_digits(self):
        """Blank VL cells must stay blank when secondary cannot locate the row."""
        from services.ground_truth.html_table import parse_html_tables
        from PIL import Image

        html = (
            "<table><tr><td>PRODUCT NAME</td><td>PACKING</td>"
            "<td>Op.Bal. Qty.</td><td>Issue Qty.</td>"
            "<td>Expiry Breakage</td><td>Closing Balance</td></tr>"
            "<tr><td>AACTARIL SOAP</td><td>75GM</td><td></td><td></td>"
            "<td></td><td></td></tr></table>"
        )
        cells = parse_html_tables(html)
        report = sr.SecondaryRepairReport(document_id="blank")
        image = Image.new("RGB", (100, 100), "white")
        out = sr._repair_himalaya_qty_cells(
            image,
            cells,
            report,
            page_text="NOISE 69 69 12 0 0 12 TOTAL 834578 1 2 3 775274",
        )
        close = next(
            c
            for c in out
            if c.get("product_key") == "AACTARIL SOAP"
            and c.get("header") == "Closing Balance"
        )
        self.assertEqual(str(close.get("value") or "").strip(), "")
        self.assertFalse(any(r.get("corrected") for r in report.repairs))

    def test_incompatible_layout_rejects_qty_total_even_with_digit_line(self):
        self.assertFalse(
            sr._is_himalaya_qty_total_layout(
                [
                    {"is_header": True, "header": "Opening"},
                    {"is_header": True, "header": "Issues Replied"},
                    {"is_header": True, "header": "Closing"},
                ]
            )
        )
        self.assertTrue(
            sr._is_himalaya_qty_total_layout(
                [
                    {"is_header": True, "header": "Op.Bal. Qty."},
                    {"is_header": True, "header": "Issue Qty."},
                    {"is_header": True, "header": "Closing Balance"},
                ]
            )
        )


if __name__ == "__main__":
    unittest.main()

