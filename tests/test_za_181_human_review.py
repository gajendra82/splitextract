"""Review store, verified-only metrics, and proposal deadline tests."""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scripts import za_181_accuracy_workflow as workflow
from scripts.za_181_accuracy_workflow import proposal_should_skip
from scripts.za_181_human_review import (
    CSV_COLUMNS,
    HumanReviewHandler,
    apply_image_update,
    build_state,
    evaluate_verified,
    import_review_csv,
    load_or_create_state,
    load_review_csv,
    load_state,
    review_counts,
    save_review_csv,
    save_state,
)
from scripts.za_181_proposal_reliability import (
    ProposalAttemptError,
    atomic_write_text,
    call_with_retries,
    classify_request_error,
    proposal_concurrency,
    run_with_deadline,
)

ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _protected_paths() -> list:
    accuracy = ROOT / "phase4" / "za_181_accuracy"
    paths = [
        accuracy / "ground_truth.json",
        accuracy / "ground_truth_progress.json",
        accuracy / "ai_proposal_review.csv",
        accuracy / "ai_proposal_review_summary.md",
        accuracy / "ACCURACY_REPORT.md",
        accuracy / "tesseract_evaluation.json",
        accuracy / "paddleocr_vl_evaluation.json",
        accuracy / "accuracy_comparison.csv",
        accuracy / "ground_truth_review.csv",
        ROOT / "phase4" / "za_ground_truth" / "per_document" / "0000719004_2026_08_ZA_04_550_02092026014949.jpg.json",
        ROOT / "phase4" / "za_ground_truth" / "per_document" / "0000722289_2026_08_ZA_04_589_04092026043529.jpg.json",
    ]
    paths.extend(sorted((accuracy / "proposals").glob("*.json")))
    return paths


def _absent():
    return {"raw": None, "normalized": None, "status": "absent"}


def _verified_row():
    row = {
        "row_id": "r01",
        "row_index": 1,
        "product_name": {"raw": "Cystone Tab", "normalized": "Cystone Tab", "status": "verified"},
        "packing": _absent(),
        **{field: _absent() for field in (
            "opening_qty", "receipt_qty", "sales_qty", "sales_scheme_qty", "stock_out_qty", "closing_qty",
        )},
    }
    row["closing_qty"] = {"raw": 4, "normalized": 4, "status": "verified"}
    return row


def _sample_dataset():
    return {
        "images": [
            {
                "document_id": "verified.jpg",
                "source_file": "verified.jpg",
                "verification_status": "verified",
                "annotation_origin": "existing_source_verified",
                "human_verified": True,
                "rows": [_verified_row()],
            },
            {
                "document_id": "proposed.png",
                "source_file": "proposed.png",
                "verification_status": "unverified",
                "annotation_origin": "AI_PROPOSED",
                "ai_generated": True,
                "human_verified": False,
                "rows": [
                    {
                        "row_id": "r01",
                        "product_name": {"raw": "Cystone Tab", "normalized": "Cystone Tab", "status": "ai_proposed"},
                        "columns": [
                            {
                                "position": 1,
                                "printed_header": "Closing",
                                "semantic": "closing_qty",
                                "raw": "4",
                                "status": "uncertain",
                            }
                        ],
                    }
                ],
            },
            {
                "document_id": "failed.jpg",
                "source_file": "failed.jpg",
                "verification_status": "proposal_failed",
                "annotation_origin": "AI_PROPOSED_FAILED",
                "rows": [],
            },
        ]
    }


class ProtectedFilesTest(unittest.TestCase):
    hashes = {}

    @classmethod
    def setUpClass(cls):
        cls.hashes = {str(path): _sha256(path) for path in _protected_paths()}

    @classmethod
    def tearDownClass(cls):
        for path_text, digest in cls.hashes.items():
            current = _sha256(Path(path_text))
            if current != digest:
                raise AssertionError(f"protected file changed: {path_text}")


class ReviewStoreTests(unittest.TestCase):
    def test_csv_round_trip_and_resume(self):
        with self._tmpdir() as raw:
            directory = Path(raw)
            state = load_or_create_state(directory, _sample_dataset())
            self.assertEqual(review_counts(state)["pending_rows"], 1)
            self.assertTrue(state["images"][0]["locked_source_verified"])
            row = state["images"][1]["rows"][0]
            row["review_status"] = "UNCERTAIN"
            row["reviewer_notes"] = "header is unclear"
            save_state(directory, state)
            backups = list((directory / "backups").glob("*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["images"][1]["rows"][0]["review_status"], "PENDING")
            resumed = load_state(directory)
            self.assertEqual(resumed["images"][1]["rows"][0]["review_status"], "UNCERTAIN")
            loaded = load_review_csv(directory / "review_working.csv")
            self.assertEqual(list(loaded[0].keys()), CSV_COLUMNS)
            save_review_csv(directory / "copy.csv", loaded)
            self.assertEqual(load_review_csv(directory / "copy.csv"), loaded)

    def test_status_rules_and_locked_labels(self):
        state = build_state(_sample_dataset())
        proposed = state["images"][1]
        row = proposed["rows"][0]
        row["review_status"] = "VERIFIED"
        with self.assertRaises(ValueError):
            apply_image_update(state, {"document_id": "proposed.png", "rows": proposed["rows"]})
        row["reviewer_notes"] = "matches the image"
        row["columns"][0]["score_column"] = True
        apply_image_update(state, {"document_id": "proposed.png", "rows": proposed["rows"]})
        self.assertEqual(state["images"][1]["rows"][0]["proposed_product_name"], "Cystone Tab")
        changed = json.loads(json.dumps(state["images"][1]["rows"]))
        changed[0]["proposed_product_name"] = "Changed"
        with self.assertRaises(ValueError):
            apply_image_update(state, {"document_id": "proposed.png", "rows": changed})
        with self.assertRaises(ValueError):
            apply_image_update(state, {"document_id": "verified.jpg", "rows": []})
        with self.assertRaises(ValueError):
            apply_image_update(state, {"document_id": "proposed.png", "rows": []})

    def test_csv_import_cannot_downgrade_or_replace_proposal(self):
        state = build_state(_sample_dataset())
        state["images"][1]["rows"][0]["review_status"] = "VERIFIED"
        state["images"][1]["rows"][0]["reviewer_notes"] = "checked"
        state["images"][1]["rows"][0]["columns"][0]["score_column"] = True
        exported = [
            {
                "image_id": "proposed.png",
                "row_id": "r01",
                "proposed_product_name": "Cystone Tab",
                "review_status": "PENDING",
                "corrected_product_name": "",
                "reviewer_notes": "",
                "score_columns": "",
            }
        ]
        with self.assertRaises(ValueError):
            import_review_csv(state, exported)
        exported[0]["proposed_product_name"] = "Other"
        exported[0]["review_status"] = "VERIFIED"
        exported[0]["reviewer_notes"] = "checked"
        with self.assertRaises(ValueError):
            import_review_csv(state, exported, allow_downgrade=True)

    def _tmpdir(self):
        return __import__("tempfile").TemporaryDirectory()


class MetricTests(unittest.TestCase):
    def test_verified_metrics_exclude_pending_proposals(self):
        dataset = _sample_dataset()
        dataset["images"][0]["rows"].append(
            {
                "row_id": "r02",
                "product_name": {"raw": "Bonus Drop", "normalized": "Bonus Drop", "status": "verified"},
                "closing_qty": {"raw": 0, "normalized": 0, "status": "verified"},
            }
        )
        state = build_state(dataset)

        def loader(engine, document_id):
            self.assertEqual(document_id, "verified.jpg")
            if engine == "tesseract":
                return "tesseract", [{"product": "Cystone Tab"}, {"product": "Bonus Drop"}], "Cystone Tab 4\nBonus Drop"
            return (
                "paddleocr-vl-1.6",
                [{"product": "Cystone Tab"}, {"product": "Bonus Drop"}, {"product": "Other Item"}],
                "Cystone Tab 9\nBonus Drop",
            )

        result = evaluate_verified(dataset, state, loader)
        self.assertEqual(result["document_ids"], ["verified.jpg"])
        self.assertFalse(result["sufficient_for_181_images"])
        self.assertGreater(result["ai_proposed_rows_excluded"], 0)
        tess = result["engines"]["tesseract"]
        paddle = result["engines"]["paddleocr-vl"]
        self.assertEqual(tess["product_correct"], 2)
        self.assertEqual(tess["product_missing"], 0)
        self.assertEqual(tess["product_extra"], 0)
        self.assertEqual(tess["quantity_correct"], 1)
        self.assertEqual(tess["quantity_missing"], 1)
        self.assertEqual(tess["silent_wrong"], 0)
        self.assertEqual(paddle["product_extra"], 1)
        self.assertAlmostEqual(paddle["product_precision"], 2 / 3)
        self.assertEqual(paddle["product_recall"], 1)
        self.assertAlmostEqual(paddle["product_f1"], 0.8)
        self.assertEqual(paddle["quantity_incorrect"], 1)
        self.assertEqual(paddle["silent_wrong"], 1)
        self.assertEqual(paddle["quantity_missing"], 1)

    def test_corrected_review_is_scored_and_unchecked_quantity_is_not(self):
        dataset = _sample_dataset()
        state = build_state(dataset)
        row = state["images"][1]["rows"][0]
        row["review_status"] = "VERIFIED"
        row["reviewer_notes"] = "name matches the photo"
        calls = {"ids": []}

        def loader(engine, document_id):
            calls["ids"].append(document_id)
            return "tesseract" if engine == "tesseract" else "paddleocr-vl-1.6", [{"product": "Cystone Tab"}], "Cystone Tab 4"

        result = evaluate_verified(dataset, state, loader)
        human = [doc for doc in result["engines"]["tesseract"]["documents"] if doc["document_id"] == "proposed.png"]
        self.assertEqual(human[0]["product_correct"], 1)
        self.assertEqual(human[0]["quantity_scored"], 0)
        row["columns"][0]["score_column"] = True
        result = evaluate_verified(dataset, state, loader)
        human = [doc for doc in result["engines"]["tesseract"]["documents"] if doc["document_id"] == "proposed.png"]
        self.assertEqual(human[0]["quantity_correct"], 1)
        self.assertNotIn("failed.jpg", result["document_ids"])

    def test_fragment_of_a_labeled_name_is_not_an_extra_product(self):
        dataset = {"images": [_sample_dataset()["images"][0]]}
        state = build_state(dataset)

        def loader(engine, document_id):
            return "tesseract" if engine == "tesseract" else "paddleocr-vl-1.6", [
                {"product": "Cystone"},
                {"product": "Cystone Tab"},
            ], "Cystone Tab 4"

        result = evaluate_verified(dataset, state, loader)
        self.assertEqual(result["engines"]["tesseract"]["product_extra"], 0)
        self.assertEqual(result["engines"]["tesseract"]["product_correct"], 1)


class ReliabilityTests(unittest.TestCase):
    def test_deadline_does_not_wait_for_the_hung_call(self):
        finished = threading.Event()

        def hang():
            time.sleep(2)
            finished.set()

        started = time.perf_counter()
        with self.assertRaises(TimeoutError):
            run_with_deadline(hang, timeout=0.2, cancel=lambda: None)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertFalse(finished.is_set())

    def test_retry_limit_and_error_classes(self):
        calls = {"n": 0}

        def timed_out():
            calls["n"] += 1
            raise TimeoutError("Gemini request timed out after 180s")

        with self.assertRaises(ProposalAttemptError) as caught:
            call_with_retries(timed_out, sleep=lambda _seconds: None, rng=random.Random(1))
        self.assertEqual(calls["n"], 3)
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(caught.exception.failure_class, "timeout")
        calls["n"] = 0

        def rejected():
            calls["n"] += 1
            raise ValueError("vision response did not contain a JSON object")

        with self.assertRaises(ProposalAttemptError) as caught:
            call_with_retries(rejected, sleep=lambda _seconds: None)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(caught.exception.failure_class, "other")
        self.assertEqual(classify_request_error(OSError("[Errno 11001] getaddrinfo failed")), "dns")
        self.assertEqual(classify_request_error(ConnectionError("Server disconnected without sending a response.")), "connection")

    def test_atomic_save_replaces_only_complete_text(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            path = Path(tmp) / "ground_truth.json"
            path.write_text('{"old": true}', encoding="utf-8")
            with patch("scripts.za_181_proposal_reliability.os.replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    atomic_write_text(path, '{"new": true}')
            self.assertEqual(path.read_text(encoding="utf-8"), '{"old": true}')
            for leftover in Path(tmp).glob("*.tmp"):
                leftover.unlink()
            atomic_write_text(path, '{"new": true}')
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"new": True})
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_progress_save_writes_complete_json_without_touching_real_files(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            dataset = {
                "images": [
                    {
                        "document_id": "a.jpg",
                        "verification_status": "not_annotated",
                        "annotation_origin": "none",
                        "rows": [],
                        "source_file_exists": False,
                    }
                ]
            }
            with patch.object(workflow, "OUT_DIR", Path(tmp)):
                workflow._save_progress_only(dataset)
            saved = json.loads((Path(tmp) / "ground_truth.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["images"][0]["document_id"], "a.jpg")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_concurrency_defaults_to_one_and_skips_saved_work(self):
        previous = os.environ.pop("ZA_PROPOSAL_CONCURRENCY", None)
        try:
            self.assertEqual(proposal_concurrency(), 1)
        finally:
            if previous is not None:
                os.environ["ZA_PROPOSAL_CONCURRENCY"] = previous
        with self.assertRaises(ValueError):
            proposal_concurrency("9")
        self.assertTrue(proposal_should_skip(_sample_dataset()["images"][0]))
        self.assertTrue(proposal_should_skip(_sample_dataset()["images"][1]))
        self.assertFalse(proposal_should_skip(_sample_dataset()["images"][2]))

    def test_review_server_saves_only_inside_its_directory(self):
        with __import__("tempfile").TemporaryDirectory() as raw:
            directory = Path(raw) / "human_review"
            dataset = _sample_dataset()
            HumanReviewHandler.directory = directory
            HumanReviewHandler.dataset = dataset
            HumanReviewHandler.state = load_or_create_state(directory, dataset)
            server = ThreadingHTTPServer(("127.0.0.1", 0), HumanReviewHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                page = urllib.request.urlopen(f"http://127.0.0.1:{port}/")
                self.assertEqual(page.status, 200)
                payload = json.loads(json.dumps(HumanReviewHandler.state["images"][1]))
                payload["rows"][0]["review_status"] = "UNCERTAIN"
                payload["rows"][0]["reviewer_notes"] = "cannot read the row"
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/save",
                    data=json.dumps({"document_id": "proposed.png", "rows": payload["rows"]}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                saved = json.loads(urllib.request.urlopen(request).read().decode("utf-8"))
                self.assertTrue(saved["ok"])
                self.assertEqual(load_state(directory)["images"][1]["rows"][0]["review_status"], "UNCERTAIN")
                self.assertEqual(len(list((directory / "backups").glob("*.json"))), 1)
                self.assertFalse((Path(raw) / "ground_truth.json").exists())
                bad = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/save",
                    data=json.dumps({"document_id": "verified.jpg", "rows": []}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(bad)
                self.assertEqual(caught.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()

    def test_evaluate_command_refuses_to_rewrite_ground_truth(self):
        with self.assertRaises(SystemExit) as caught:
            workflow.main(["workflow", "evaluate"])
        self.assertIn("ground_truth.json", str(caught.exception))

    def test_saved_proposal_sheet_remains_pending(self):
        rows = load_review_csv(ROOT / "phase4" / "za_181_accuracy" / "ai_proposal_review.csv")
        self.assertEqual(len(rows), 1854)
        self.assertEqual(len({row["image_id"] for row in rows}), 51)
        self.assertTrue(all(row["review_status"] == "PENDING" for row in rows))


if __name__ == "__main__":
    unittest.main()
