"""Phase 3 measurement tests. Phase 1 and Phase 2 behavior is not redefined here."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from phase3.compare import classify_transition
from phase3.metrics import aggregate, recommend
from phase3.selection import select_batch
from phase3.runner import evaluate_document, run_batch
from phase3.telemetry import project_telemetry
from phase3.verify import is_trusted_ground_truth, verify_against_ground_truth


def _item(name="A", opening=1, receipts=1, sales=1, closing=1):
    return {
        "product_name": name,
        "opening_qty": opening,
        "receipts_qty": receipts,
        "sales_qty": sales,
        "closing_qty": closing,
        "opening_value": None,
        "receipts_value": None,
        "sales_value": None,
        "closing_value": None,
    }


def _statement(accepted, items, phase2=None, method="product_wise_stock_statement"):
    result = {
        "line_items": items,
        "totals": {"extra": {"extraction_method": method}},
        "phase1": {
            "route": {
                "document_type": "sales_statement",
                "file_kind": "pdf",
                "pdf_kind": "digital",
                "requires_ocr": False,
            },
            "evidence": {"pages": [{"extraction_method": "pymupdf"}]},
            "validation": {
                "accepted": accepted,
                "status": "pass" if accepted else "unresolved",
                "failure_type": None if accepted else "STOCK_IDENTITY_FAILURE",
                "quality_score": 90,
                "parser": method,
            },
        },
    }
    if phase2 is not None:
        result["phase2"] = phase2
    return result


class TransitionTests(unittest.TestCase):
    def test_pass_to_pass(self):
        baseline = _statement(True, [_item()])
        compared = classify_transition(baseline, baseline)
        self.assertEqual(compared["transition"], "PASS → PASS")
        self.assertEqual(compared["recovery_state"], "RECOVERY_NOT_ATTEMPTED")

    def test_fail_to_recovered(self):
        baseline = _statement(False, [_item(closing=0)])
        phase2 = _statement(
            True,
            [_item()],
            phase2={"status": "recovered", "failure_type": "STOCK_IDENTITY_FAILURE", "strategy": "targeted_stock_extraction", "attempts": 1},
        )
        compared = classify_transition(baseline, phase2, verification="UNVERIFIED")
        self.assertEqual(compared["transition"], "FAIL → RECOVERED")
        self.assertEqual(compared["verification"], "RECOVERY_UNVERIFIED")

    def test_fail_to_unresolved(self):
        baseline = _statement(False, [])
        phase2 = _statement(
            False,
            [],
            phase2={"status": "unresolved", "failure_type": "EMPTY_OUTPUT", "strategy": "ocr_path", "attempts": 0},
        )
        compared = classify_transition(baseline, phase2)
        self.assertEqual(compared["transition"], "FAIL → UNRESOLVED")
        self.assertEqual(compared["recovery_state"], "RECOVERY_UNRESOLVED")

    def test_fail_to_wrong(self):
        baseline = _statement(False, [_item(closing=None)])
        phase2 = _statement(
            True,
            [_item(closing=5)],
            phase2={"status": "recovered", "failure_type": "STOCK_IDENTITY_FAILURE", "strategy": "targeted_stock_extraction", "attempts": 1},
        )
        compared = classify_transition(baseline, phase2, verification="wrong")
        self.assertEqual(compared["transition"], "FAIL → WRONG")

    def test_pass_to_wrong_when_values_change_against_ground_truth(self):
        baseline = _statement(True, [_item(closing=1)])
        phase2 = _statement(
            True,
            [_item(closing=9)],
            phase2={"status": "recovered", "attempts": 1, "strategy": "targeted_stock_extraction", "failure_type": "STOCK_IDENTITY_FAILURE"},
        )
        compared = classify_transition(baseline, phase2, verification="wrong")
        self.assertEqual(compared["transition"], "PASS → WRONG")

    def test_pass_to_wrong_when_null_becomes_zero(self):
        baseline = _statement(True, [_item(opening=None)])
        changed = _statement(True, [_item(opening=0)])
        compared = classify_transition(baseline, changed)
        self.assertIn("null_became_zero", compared["safety_flags"])
        self.assertEqual(compared["transition"], "PASS → WRONG")


class MetricsTests(unittest.TestCase):
    def test_unverified_acceptance_is_not_verified_correct(self):
        records = [
            {
                "baseline_outcome": "FAIL",
                "transition": "FAIL → RECOVERED",
                "recovery_state": "RECOVERY_ACCEPTED",
                "verification": "RECOVERY_UNVERIFIED",
                "file_type": ".pdf",
                "profile": "product_wise",
                "recovery_strategy": "targeted_stock_extraction",
                "failure_type": "STOCK_IDENTITY_FAILURE",
                "ocr_path": "digital_text",
                "ocr_required": False,
                "attempts": 1,
                "provider_calls": 1,
                "processing_time_ms": 10,
                "safety_flags": [],
            }
        ]
        metrics = aggregate(records)
        self.assertEqual(metrics["phase2"]["verified_correct"], 0)
        self.assertEqual(metrics["phase2"]["unverified_accepted"], 1)
        self.assertEqual(metrics["quality"]["verified_recovery_precision"], None)
        self.assertEqual(metrics["breakdown"]["file_type"][".pdf"]["documents"], 1)
        self.assertEqual(metrics["breakdown"]["layout"]["product_wise"]["FAIL → RECOVERED"], 1)
        self.assertEqual(
            metrics["breakdown"]["recovery_strategy"]["targeted_stock_extraction"]["documents"],
            1,
        )
        self.assertEqual(metrics["breakdown"]["ocr"]["digital_text"]["documents"], 1)
        self.assertEqual(metrics["averages"]["recovery_attempts"], 1.0)
        decision = recommend(metrics)
        self.assertEqual(decision["recommendation"], "INVESTIGATE")

    def test_verified_wrong_stops(self):
        metrics = aggregate(
            [
                {
                    "baseline_outcome": "FAIL",
                    "transition": "FAIL → WRONG",
                    "recovery_state": "RECOVERY_ACCEPTED",
                    "verification": "RECOVERY_VERIFIED_WRONG",
                    "file_type": ".jpg",
                    "profile": "unknown",
                    "recovery_strategy": "targeted_stock_extraction",
                    "failure_type": "OCR_REQUIRED",
                    "ocr_path": "ocr_required",
                    "ocr_required": True,
                    "attempts": 1,
                    "provider_calls": 1,
                    "processing_time_ms": 5,
                    "safety_flags": [],
                }
            ]
        )
        self.assertEqual(metrics["safety"]["fail_to_wrong"], 1)
        self.assertEqual(metrics["phase2"]["verified_wrong"], 1)
        self.assertEqual(recommend(metrics)["recommendation"], "STOP")


class GroundTruthTests(unittest.TestCase):
    def test_low_confidence_is_not_trusted(self):
        self.assertFalse(
            is_trusted_ground_truth(
                {"confidence": "low", "format": "Stock Statement", "line_items": [_item()], "partial": False}
            )
        )
        self.assertEqual(
            verify_against_ground_truth(
                {"confidence": "medium", "format": "Stock Statement", "line_items": [_item()]},
                _statement(True, [_item()]),
            ),
            "UNVERIFIED",
        )


class TelemetryTests(unittest.TestCase):
    def test_telemetry_does_not_modify_extraction(self):
        result = _statement(True, [_item()])
        before = json.dumps(result, sort_keys=True)
        payload = project_telemetry(result, document_id="doc-1", requires_ocr=False)
        self.assertEqual(json.dumps(result, sort_keys=True), before)
        self.assertEqual(payload["document_id"], "doc-1")
        self.assertEqual(payload["phase1_accepted"], True)
        self.assertEqual(payload["ocr_path"], "digital_text")


class ShadowTests(unittest.TestCase):
    def test_shadow_does_not_overwrite_source_or_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = root / "dataset"
            output = root / "phase3_eval"
            dataset.mkdir()
            source = dataset / "sample.xlsx"
            source.write_bytes(b"original-bytes")
            baseline = _statement(False, [_item(closing=None)])

            def extract_fn(file_bytes, filename):
                self.assertEqual(file_bytes, b"original-bytes")
                return json.loads(json.dumps(baseline))

            def recover_fn(result, file_bytes, filename):
                result["line_items"][0]["closing_qty"] = 4
                result["phase2"] = {
                    "status": "recovered",
                    "failure_type": "STOCK_IDENTITY_FAILURE",
                    "strategy": "targeted_stock_extraction",
                    "attempts": 1,
                }
                result["phase1"]["validation"]["accepted"] = True
                return result

            record = evaluate_document(
                source,
                dataset_dir=dataset,
                output_dir=output,
                extract_fn=extract_fn,
                recover_fn=recover_fn,
                allow_pdf_ground_truth=False,
            )
            self.assertEqual(source.read_bytes(), b"original-bytes")
            self.assertEqual(record["transition"], "FAIL → RECOVERED")
            self.assertEqual(record["verification"], "RECOVERY_UNVERIFIED")
            self.assertFalse((dataset / "phase3_eval").exists())

    def test_checkpoint_resume_skips_completed_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = root / "dataset"
            output = root / "out"
            dataset.mkdir()
            source = dataset / "a.txt"
            source.write_text("hello", encoding="utf-8")
            calls = {"n": 0}

            def extract_fn(file_bytes, filename):
                calls["n"] += 1
                return _statement(True, [_item()])

            def recover_fn(result, file_bytes, filename):
                result["phase2"] = {"status": "not_needed", "attempts": 0, "strategy": None, "failure_type": None}
                return result

            selection = [{"relative_path": "a.txt", "path": str(source), "requires_ocr": False}]
            run_batch(
                selection,
                dataset_dir=dataset,
                output_dir=output,
                extract_fn=extract_fn,
                recover_fn=recover_fn,
                allow_pdf_ground_truth=False,
            )
            run_batch(
                selection,
                dataset_dir=dataset,
                output_dir=output,
                extract_fn=extract_fn,
                recover_fn=recover_fn,
                allow_pdf_ground_truth=False,
            )
            self.assertEqual(calls["n"], 1)
            progress = json.loads((output / "progress.json").read_text(encoding="utf-8"))
            self.assertEqual(progress["a.txt"]["state"], "complete")

    def test_output_inside_dataset_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp)
            with self.assertRaises(RuntimeError):
                evaluate_document(
                    dataset / "missing.pdf",
                    dataset_dir=dataset,
                    output_dir=dataset / "phase3_eval",
                    extract_fn=lambda *_: {},
                    recover_fn=lambda result, *_args: result,
                    allow_pdf_ground_truth=False,
                )


class SelectionTests(unittest.TestCase):
    def test_selection_is_not_the_first_alphabetical_hundred(self):
        records = []
        for index in range(120):
            records.append(
                {
                    "relative_path": f"{index:04d}.pdf",
                    "filename": f"{index:04d}.pdf",
                    "file_kind": "pdf",
                    "pdf_kind": "digital" if index % 2 == 0 else "scanned",
                    "extension": ".pdf",
                    "page_count": 1 if index % 3 else 6,
                }
            )
        for index in range(10):
            records.append(
                {
                    "relative_path": f"z{index}.xlsx",
                    "filename": f"z{index}.xlsx",
                    "file_kind": "xlsx",
                    "pdf_kind": None,
                    "extension": ".xlsx",
                    "page_count": None,
                }
            )
        chosen = select_batch(records, limit=100)
        alpha = [row["relative_path"] for row in sorted(records, key=lambda row: row["relative_path"])[:100]]
        picked = [row["relative_path"] for row in chosen]
        self.assertNotEqual(picked, alpha)
        self.assertTrue(any(name.endswith(".xlsx") for name in picked))
        self.assertTrue(any(name.endswith(".pdf") for name in picked))
        self.assertLessEqual(len(chosen), 100)


if __name__ == "__main__":
    unittest.main()
