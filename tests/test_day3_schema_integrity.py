"""Day 3: predefined schema, dynamic JSON, API preservation, JSONB."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import unittest
from datetime import datetime

from pydantic import ValidationError

from services.hybrid_schema import (
    HybridSchemaError,
    SalesExtractionOutput,
    SalesLineFixed,
    accept_document,
    ensure_json_value,
    parse_recovery_number,
    result_from_document,
    sales_recovery_json_schema,
)
from services.recovery_runner import _parse_recovery_text
from services.schemas import SchemaValidationError, normalize_sales_statement, parse_optional_number


def _nested(depth: int):
    node = {"leaf": "end"}
    for _ in range(depth):
        node = {"child": node}
    return node


def _dynamic_payload():
    return {
        "note": "हिंदी / café — 10%",
        "flags": [True, False, None, "x", 0, 1.5],
        "empty_object": {},
        "empty_array": [],
        "nested": {"row": [{"qty": 2, "label": "α"}]},
        "active": False,
        "missing": None,
    }


class PredefinedSchemaTests(unittest.TestCase):
    def test_required_shape_keeps_null_and_explicit_zero(self):
        line = SalesLineFixed.model_validate(
            {
                "product_name": "SPEMAN",
                "opening_qty": None,
                "receipts_qty": "NA",
                "sales_qty": 0,
                "closing_qty": "0",
                "sales_value": "(12.5)",
            }
        )
        self.assertIsNone(line.opening_qty)
        self.assertIsNone(line.receipts_qty)
        self.assertEqual(line.sales_qty, 0.0)
        self.assertEqual(line.closing_qty, 0.0)
        self.assertEqual(line.sales_value, -12.5)
        self.assertIsNone(line.packing)

    def test_wrong_type_and_extra_field_are_rejected(self):
        with self.assertRaises(ValidationError):
            SalesLineFixed.model_validate({"opening_qty": "not-a-number"})
        with self.assertRaises(ValidationError):
            SalesLineFixed.model_validate({"opening_qty": True})
        with self.assertRaises(ValidationError):
            SalesLineFixed.model_validate({"unknown_column": 1})

    def test_non_finite_numbers_are_rejected(self):
        with self.assertRaises(SchemaValidationError):
            parse_optional_number(float("nan"), field="opening_qty")
        with self.assertRaises(SchemaValidationError):
            parse_optional_number("inf", field="sales_qty")
        with self.assertRaises(HybridSchemaError):
            parse_recovery_number("NaN", "opening_qty")
        with self.assertRaises(ValidationError):
            SalesLineFixed.model_validate({"opening_qty": "NaN"})

    def test_invalid_llm_json_does_not_become_a_zero_row(self):
        self.assertIsNone(_parse_recovery_text("{"))
        self.assertIsNone(_parse_recovery_text('{"line_items":[{"fixed":{"opening_qty":"bad"}}]}'))
        parsed = _parse_recovery_text(
            '```json\n{"line_items":[{"fixed":{"product_name":"SPEMAN","opening_qty":null,'
            '"sales_qty":0},"dynamic":{"note":"kept"}}]}\n```'
        )
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.line_items[0].fixed.opening_qty)
        self.assertEqual(parsed.line_items[0].fixed.sales_qty, 0.0)
        self.assertEqual(parsed.line_items[0].dynamic["note"], "kept")

    def test_projection_reports_non_json_dynamic_values(self):
        statement = {
            "line_items": [
                {
                    "product_name": "SPEMAN",
                    "opening_qty": 1,
                    "sales_qty": "not-a-number",
                    "extra": {
                        "note": "kept",
                        "when": datetime(2026, 8, 1),
                        "nested": {"ok": True},
                    },
                }
            ],
            "totals": {"sales_value": float("nan"), "extra": {"promo": "α"}},
        }
        projected = normalize_sales_statement(statement)
        item = projected["line_items"][0]
        self.assertEqual(item["opening_qty"], 1.0)
        self.assertIsNone(item["sales_qty"])
        self.assertEqual(item["dynamic"]["note"], "kept")
        self.assertEqual(item["dynamic"]["nested"], {"ok": True})
        self.assertNotIn("when", item["dynamic"])
        self.assertIsNone(projected["totals"]["sales_value"])
        self.assertEqual(projected["totals"]["dynamic"]["promo"], "α")
        joined = " ".join(projected["metadata"]["schema_errors"])
        self.assertIn("not-a-number", joined)
        self.assertIn("when", joined)
        self.assertEqual(statement["line_items"][0]["extra"]["when"], datetime(2026, 8, 1))
        json.dumps(projected)


class DynamicJsonTests(unittest.TestCase):
    def test_unknown_structure_round_trips_unchanged(self):
        payload = _dynamic_payload()
        model = SalesExtractionOutput.model_validate(
            {
                "line_items": [
                    {
                        "fixed": {"product_name": "SPEMAN", "packing": "60s"},
                        "dynamic": payload,
                    }
                ],
                "dynamic": {"batch": payload},
            }
        )
        dumped = model.model_dump(mode="json")
        self.assertEqual(dumped["line_items"][0]["dynamic"], payload)
        self.assertEqual(dumped["dynamic"]["batch"], payload)
        self.assertEqual(dumped["line_items"][0]["fixed"]["packing"], "60s")
        again = json.loads(model.model_dump_json())
        self.assertEqual(again["line_items"][0]["dynamic"], payload)

    def test_deep_nesting_is_bounded(self):
        accepted = _nested(31)
        self.assertEqual(ensure_json_value(accepted), accepted)
        with self.assertRaises(HybridSchemaError):
            ensure_json_value(_nested(32))

    def test_recovery_schema_allows_nested_dynamic_objects(self):
        # Gemini schema stays shallow (additionalProperties: true). Nested
        # dynamic values are still accepted by Pydantic on parse.
        schema = sales_recovery_json_schema()
        dynamic = schema["$defs"]["SalesLineOutput"]["properties"]["dynamic"]
        self.assertEqual(dynamic.get("type"), "object")
        self.assertIs(dynamic.get("additionalProperties"), True)
        nested = {"rows": [{"n": 1, "meta": {"ok": True}}]}
        model = SalesExtractionOutput.model_validate(
            {"line_items": [{"fixed": {"product_name": "SPEMAN"}, "dynamic": nested}]}
        )
        self.assertEqual(model.line_items[0].dynamic["rows"][0]["meta"]["ok"], True)

    def test_large_dynamic_payload_serializes(self):
        blob = {"rows": [{"n": index, "t": "α" * 20} for index in range(2000)]}
        started = time.perf_counter()
        model = SalesExtractionOutput.model_validate(
            {"line_items": [{"fixed": {"product_name": "SPEMAN"}, "dynamic": blob}]}
        )
        raw = model.model_dump_json()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 2.0)
        self.assertEqual(json.loads(raw)["line_items"][0]["dynamic"]["rows"][1999]["n"], 1999)


class ApiPreservationTests(unittest.TestCase):
    def test_unknown_line_and_statement_fields_survive_the_api_response(self):
        from app import build_split_extract_response_from_sales_statement

        payload = _dynamic_payload()
        sales = {
            "stockist_name": "LUCKY",
            "company_name": "ZYDUS",
            "report_title": "STOCK",
            "line_items": [
                {
                    "product_name": "SPEMAN",
                    "product_code": "SP1",
                    "packing": "60s",
                    "sales_qty": 0,
                    "opening_qty": None,
                    "extra": {
                        **payload,
                        "opening_qty": 0,
                        "layout": "not_a_known_layout",
                    },
                }
            ],
            "totals": {
                "sales_value": 0,
                "extra": {
                    "extraction_method": "unit",
                    "promo": payload,
                },
            },
        }
        response = build_split_extract_response_from_sales_statement(
            sales_result=sales,
            source_filename="statement.xlsx",
            use_blob_storage=False,
        )
        encoded = json.dumps(response, ensure_ascii=False)
        restored = json.loads(encoded)
        item = restored["invoices"][0]["extracted_data"]["data"]["line_items"]["items"][0]
        fields = item["additional_fields"]
        self.assertEqual(fields["opening_qty"], 0)
        self.assertEqual(fields["packing"], "60s")
        self.assertEqual(fields["note"], payload["note"])
        self.assertEqual(fields["flags"], payload["flags"])
        self.assertEqual(fields["empty_object"], {})
        self.assertEqual(fields["empty_array"], [])
        self.assertEqual(fields["nested"], payload["nested"])
        self.assertIs(fields["active"], False)
        self.assertIsNone(fields["missing"])
        self.assertEqual(fields["layout"], "not_a_known_layout")
        meta = restored["summary"]["sales_statement_metadata"]
        self.assertEqual(meta["extraction_method"], "unit")
        self.assertEqual(meta["dynamic"]["promo"], payload)
        self.assertNotIn("extraction_method", meta["dynamic"])


class DocumentScenarioTests(unittest.TestCase):
    def test_predefined_document_validates_known_fields_only(self):
        payload = {
            "product_name": "SPEMAN",
            "packing": "60s",
            "opening_qty": None,
            "sales_qty": 0,
            "closing_qty": "0",
        }
        envelope = accept_document(payload)
        self.assertEqual(envelope["schema_class"], "predefined")
        self.assertEqual(envelope["dynamic"], {})
        self.assertFalse(envelope["verified"])
        fixed = envelope["fixed"]["line_items"][0]["fixed"]
        self.assertEqual(fixed["product_name"], "SPEMAN")
        self.assertEqual(fixed["packing"], "60s")
        self.assertEqual(fixed["sales_qty"], 0.0)
        self.assertEqual(fixed["closing_qty"], 0.0)
        self.assertNotIn("opening_qty", fixed)
        response = self._api(payload)
        item = response["invoices"][0]["extracted_data"]["data"]["line_items"]["items"][0]
        self.assertEqual(item["product_description"], "SPEMAN")
        self.assertEqual(item["additional_fields"]["packing"], "60s")
        self.assertEqual(item["additional_fields"]["closing_qty"], 0.0)
        self.assertNotIn("note", item["additional_fields"])

    def test_hybrid_keeps_unknown_json_and_rejects_bad_numbers(self):
        payload = {
            "product_name": "SPEMAN",
            "sales_qty": "not-a-number",
            "closing_qty": 4,
            "note": "हिंदी",
            "flags": [True, None, 1.5],
            "nested": {"empty": {}, "rows": []},
        }
        evidence = {"status": "checked", "compared_rows": 1, "reader": "ocr_text"}
        envelope = accept_document(payload, source_evidence=evidence)
        self.assertEqual(envelope["schema_class"], "hybrid")
        self.assertFalse(envelope["verified"])
        self.assertEqual(envelope["verification_status"], "UNRESOLVED")
        self.assertEqual(envelope["source_evidence"], evidence)
        self.assertEqual(envelope["dynamic"]["note"], "हिंदी")
        self.assertEqual(envelope["dynamic"]["flags"], [True, None, 1.5])
        self.assertEqual(envelope["dynamic"]["nested"], {"empty": {}, "rows": []})
        self.assertEqual(envelope["document"], payload)
        fixed = envelope["fixed"]["line_items"][0]["fixed"]
        self.assertNotIn("sales_qty", fixed)
        self.assertEqual(fixed["closing_qty"], 4.0)
        self.assertTrue(envelope["schema_errors"])
        response = self._api(payload, evidence)
        fields = response["invoices"][0]["extracted_data"]["data"]["line_items"]["items"][0]["additional_fields"]
        self.assertEqual(fields["note"], "हिंदी")
        self.assertEqual(fields["flags"], [True, None, 1.5])
        self.assertEqual(fields["closing_qty"], 4.0)
        meta = response["summary"]["sales_statement_metadata"]["dynamic"]
        self.assertEqual(meta["verification"]["verified"], False)
        self.assertNotEqual(meta["verification"]["status"], "VERIFIED")
        self.assertEqual(meta["source_grounding"], evidence)

    def test_fully_dynamic_document_is_preserved_without_fabricated_fields(self):
        payload = {
            "region": "west",
            "rows": [{"sku": "α", "remark": "हिंदी", "qty_text": "see note"}],
            "flags": [False, None, "0"],
            "empty": {},
        }
        evidence = {"status": "checked", "compared_rows": 0, "snippet": "region west"}
        envelope = accept_document(payload, source_evidence=evidence)
        self.assertEqual(envelope["schema_class"], "fully_dynamic")
        self.assertIsNone(envelope["fixed"])
        self.assertEqual(envelope["document"], payload)
        self.assertFalse(envelope["verified"])
        response = self._api(payload, evidence)
        self.assertEqual(response["schema_class"], "fully_dynamic")
        self.assertEqual(response["dynamic"], payload)
        self.assertEqual(response["invoices"], [])
        self.assertFalse(response["verification"]["verified"])
        self.assertEqual(response["verification"]["status"], "UNRESOLVED")
        self.assertEqual(response["source_evidence"], evidence)
        encoded = json.dumps(response)
        self.assertNotIn("SPEMAN", encoded)
        self.assertNotIn("product_description", encoded)

    def test_empty_and_malformed_documents(self):
        empty = accept_document({})
        self.assertEqual(empty["schema_class"], "fully_dynamic")
        self.assertEqual(empty["document"], {})
        self.assertIsNone(empty["fixed"])
        listed = accept_document([])
        self.assertEqual(listed["document"], [])
        response = self._api({})
        self.assertEqual(response["dynamic"], {})
        self.assertEqual(response["invoices"], [])
        self.assertIsNone(_parse_recovery_text("{"))
        self.assertIsNone(_parse_recovery_text(""))
        with self.assertRaises(HybridSchemaError):
            accept_document({"note": float("nan")})

    def test_nested_sales_name_does_not_force_the_sales_schema(self):
        payload = {"payload": {"sales_qty": "not a column"}, "ok": True}
        envelope = accept_document(payload)
        self.assertEqual(envelope["schema_class"], "fully_dynamic")
        self.assertEqual(envelope["document"], payload)
        self.assertIsNone(envelope["fixed"])

    def _api(self, payload, evidence=None):
        from app import build_split_extract_response_from_sales_statement

        sales = result_from_document(payload, source_evidence=evidence)
        return build_split_extract_response_from_sales_statement(
            sales_result=sales,
            source_filename="dynamic.json",
            use_blob_storage=False,
        )


def _docker_ready() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        probe = subprocess.run(
            [docker, "info"],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


class PostgresJsonbTests(unittest.TestCase):
    container = "zydus-day3-jsonb"

    @classmethod
    def setUpClass(cls):
        if not _docker_ready():
            raise unittest.SkipTest(
                "Docker daemon is not running; PostgreSQL JSONB was not tested"
            )
        docker = shutil.which("docker")
        subprocess.run([docker, "rm", "-f", cls.container], capture_output=True, text=True)
        started = subprocess.run(
            [
                docker,
                "run",
                "-d",
                "--name",
                cls.container,
                "-e",
                "POSTGRES_PASSWORD=day3",
                "-e",
                "POSTGRES_DB=zydus",
                "postgres:16-alpine",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if started.returncode != 0:
            raise unittest.SkipTest(started.stderr.strip() or "postgres container did not start")
        for _ in range(30):
            ready = subprocess.run(
                [docker, "exec", cls.container, "pg_isready", "-U", "postgres"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if ready.returncode == 0:
                probe = subprocess.run(
                    [docker, "exec", cls.container, "psql", "-U", "postgres", "-d", "zydus", "-tA", "-c", "SELECT 1"],
                    capture_output=True,
                    timeout=20,
                )
                if probe.returncode == 0 and probe.stdout.strip() == b"1":
                    return
            time.sleep(1)
        raise unittest.SkipTest("postgres did not become ready")

    def _jsonb_round_trip(self, row_id: str, document):
        raw = json.dumps(document, ensure_ascii=False)
        sql = (
            "CREATE TABLE IF NOT EXISTS extraction_dynamic (id text PRIMARY KEY, payload jsonb NOT NULL);"
            "INSERT INTO extraction_dynamic (id, payload) VALUES ('"
            + row_id
            + "', $json$"
            + raw
            + "$json$::jsonb) ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload;"
            "SELECT payload::text FROM extraction_dynamic WHERE id = '"
            + row_id
            + "';"
        )
        docker = shutil.which("docker")
        stored = None
        for _ in range(5):
            stored = subprocess.run(
                [
                    docker,
                    "exec",
                    "-i",
                    self.container,
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "zydus",
                    "-tA",
                    "-v",
                    "ON_ERROR_STOP=1",
                ],
                input=sql.encode("utf-8"),
                capture_output=True,
                timeout=30,
            )
            if stored.returncode == 0:
                break
            time.sleep(1)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.returncode, 0, stored.stderr.decode("utf-8", "replace"))
        return json.loads(stored.stdout.decode("utf-8").strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        docker = shutil.which("docker")
        if docker:
            subprocess.run([docker, "rm", "-f", cls.container], capture_output=True, text=True)

    def test_api_json_round_trips_through_jsonb(self):
        from app import build_split_extract_response_from_sales_statement

        payload = _dynamic_payload()
        sales = {
            "report_title": "STOCK",
            "line_items": [
                {
                    "product_name": "SPEMAN",
                    "sales_qty": 1.5,
                    "extra": payload,
                }
            ],
            "totals": {"extra": {"extraction_method": "unit", "promo": payload}},
        }
        response = build_split_extract_response_from_sales_statement(
            sales_result=sales,
            source_filename="statement.xlsx",
            use_blob_storage=False,
        )
        document = response["summary"]["sales_statement_metadata"]["dynamic"]["promo"]
        self.assertEqual(document, payload)
        restored = self._jsonb_round_trip("promo", document)
        self.assertEqual(restored, payload)
        self.assertEqual(restored["note"], "हिंदी / café — 10%")
        self.assertEqual(restored["flags"][0], True)
        self.assertIsNone(restored["missing"])
        self.assertEqual(restored["nested"]["row"][0]["qty"], 2)

    def test_three_document_classes_round_trip_through_jsonb(self):
        from app import build_split_extract_response_from_sales_statement

        def api(payload):
            return build_split_extract_response_from_sales_statement(
                sales_result=result_from_document(payload),
                source_filename="dynamic.json",
                use_blob_storage=False,
            )

        predefined = api({"product_name": "SPEMAN", "packing": "60s", "sales_qty": 0, "closing_qty": 4})
        predefined_item = predefined["invoices"][0]["extracted_data"]["data"]["line_items"]["items"][0]
        hybrid = api({"product_name": "SPEMAN", "closing_qty": 4, "note": "हिंदी", "flags": [None, False]})
        hybrid_fields = hybrid["invoices"][0]["extracted_data"]["data"]["line_items"]["items"][0]["additional_fields"]
        dynamic = api({"rows": [{"sku": "α"}], "empty": [], "note": None})
        documents = {
            "predefined": {
                "product_description": predefined_item["product_description"],
                "packing": predefined_item["additional_fields"]["packing"],
                "sales_qty": predefined_item["additional_fields"]["sales_qty"],
                "closing_qty": predefined_item["additional_fields"]["closing_qty"],
            },
            "hybrid": {
                "note": hybrid_fields["note"],
                "flags": hybrid_fields["flags"],
                "closing_qty": hybrid_fields["closing_qty"],
            },
            "fully_dynamic": dynamic["dynamic"],
        }
        for class_name, document in documents.items():
            restored = self._jsonb_round_trip(class_name, document)
            self.assertEqual(restored, document, class_name)


if __name__ == "__main__":
    unittest.main()
