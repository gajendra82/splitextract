# Vertex 400 INVALID_ARGUMENT — Phase 2 recovery

## Captured failing response (credentials redacted)

```json
{
  "error": {
    "code": 400,
    "message": "Request contains an invalid argument.",
    "status": "INVALID_ARGUMENT"
  }
}
```

Endpoint: `POST https://aiplatform.googleapis.com/v1beta1/projects/invoice-ai-509710/locations/global/publishers/google/models/gemini-2.5-flash-lite:generateContent`

## Failing request metadata

| Field | Value |
|---|---|
| model | `gemini-2.5-flash-lite` |
| location | `global` |
| responseMimeType | `application/json` |
| SDK config key | `response_json_schema` (not `response_schema`) |
| schema title | `SalesExtractionOutput` |
| schema bytes (failing) | ~29937 |
| schema `anyOf` count (failing) | 144 |
| MIME / image parts | text OCR evidence (or document parts); no credential material logged |

## Root cause

`sales_recovery_json_schema()` replaced each `dynamic` property with a **depth-6 nested `anyOf` tree** under `additionalProperties`. Vertex rejected that schema with a generic `400 INVALID_ARGUMENT`.

Bisect (`scripts/_probe_phase2_vertex400.py` → `vertex400_bisect.json`):

| Variant | Result |
|---|---|
| raw pydantic + deep dynamic anyOf | **400** |
| strip `default` only | **400** |
| strip defaults + `dynamic.additionalProperties: true` | **200** |
| strip defaults + leaf types | **200** |
| hand-minimal schema | **200** |

## Fix

Use a shallow Gemini-facing dynamic object:

```json
{ "type": "object", "additionalProperties": true }
```

Pydantic still validates nested dynamic JSON on parse. Phase 2 remains disabled by default (`PHASE2_RECOVERY_ENABLED` defaults to `false`).

## Live confirmation after fix

`scripts/_verify_fixed_schema_live.py` against `gemini-2.5-flash-lite`:

- HTTP 200, `finish_reason=STOP`
- Valid `SalesExtractionOutput` JSON parsed by Pydantic
- Example fixed row: `TEST TAB` opening=10 receipts=0 sales=2 closing=8
- Usage sample: prompt≈450, candidates≈216 tokens
