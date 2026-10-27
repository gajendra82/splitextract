"""Bounded sales recovery after Phase 1 validation.

Works on a copy. The parser dict is replaced only when the candidate
passes validate_sales_result. Provider failures do not start another
extraction attempt.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pydantic import ValidationError

from services.failure_taxonomy import FailureType, classify_failure
from services.hybrid_schema import (
    HybridSchemaError,
    SalesExtractionOutput,
    sales_recovery_json_schema,
)
from services.recovery_policy import RecoveryPolicy, recovery_policy

logger = logging.getLogger(__name__)

_STOCK_FIELDS = (
    "opening_qty",
    "receipts_qty",
    "sales_qty",
    "closing_qty",
)
GeminiCaller = Callable[[Dict[str, Any]], str]


class RecoveryProviderError(Exception):
    """Gemini quota already retried and still failed. Do not recover again."""

    def __init__(self, code: str):
        self.code = str(code or "provider")
        super().__init__(self.code)


def phase2_recovery_enabled() -> bool:
    import os

    return os.getenv("PHASE2_RECOVERY_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def apply_phase2_recovery(
    result: Dict[str, Any],
    *,
    file_bytes: bytes = b"",
    filename: str = "",
    gemini_caller: Optional[GeminiCaller] = None,
    provider_error: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a copy. ``result`` line items are not modified."""
    if not isinstance(result, dict):
        return result
    phase1 = result.get("phase1") if isinstance(result.get("phase1"), dict) else {}
    validation = phase1.get("validation") if isinstance(phase1.get("validation"), dict) else {}
    route = phase1.get("route") if isinstance(phase1.get("route"), dict) else {}
    method = str(validation.get("parser") or _method(result) or "")

    if provider_error:
        failure = FailureType.PROVIDER_ERROR
        policy = recovery_policy(failure)
        logger.info(
            "PHASE2_RECOVERY document=%s failure=%s strategy=%s attempt=0 result=SKIPPED",
            filename or result.get("source_file") or "",
            failure.value,
            policy.strategy,
        )
        return _annotate(result, status="unresolved", failure=failure, policy=policy, attempts=0)

    if validation.get("accepted") is True:
        logger.info(
            "PHASE2_RECOVERY document=%s failure=none strategy=none attempt=0 result=NOT_NEEDED",
            filename or result.get("source_file") or "",
        )
        return _annotate(result, status="not_needed", failure=None, policy=None, attempts=0)

    failure = classify_failure(validation, route=route, extraction_method=method)
    if failure is None:
        return _annotate(result, status="not_needed", failure=None, policy=None, attempts=0)
    policy = recovery_policy(failure)
    if not policy.call_gemini or policy.max_attempts < 1:
        logger.info(
            "PHASE2_RECOVERY document=%s failure=%s strategy=%s attempt=0 result=SKIPPED",
            filename or result.get("source_file") or "",
            failure.value,
            policy.strategy,
        )
        return _annotate(result, status="unresolved", failure=failure, policy=policy, attempts=0)

    if policy.strategy == "targeted_stock_extraction":
        from services.stock_identity_profiles import select_profile

        profile = select_profile(result)
        if not profile.supports_recovery:
            logger.info(
                "PHASE2_RECOVERY document=%s failure=%s strategy=%s attempt=0 result=SKIPPED profile=%s",
                filename or result.get("source_file") or "",
                failure.value,
                policy.strategy,
                profile.profile_id,
            )
            return _annotate(
                result,
                status="unresolved",
                failure=failure,
                policy=policy,
                attempts=0,
            )

    caller = gemini_caller or _live_gemini_caller
    attempts = 0
    recovery_log: List[Dict[str, Any]] = []
    original_values = _original_qty_snapshot(result)
    payloads = build_targeted_payloads(
        result,
        strategy=policy.strategy,
        file_bytes=file_bytes,
        filename=filename,
    )
    for attempt in range(1, policy.max_attempts + 1):
        attempts = attempt
        started = time.perf_counter()
        targeted = [
            str(item.get("product_name") or "")
            for item in (result.get("line_items") or [])
            if isinstance(item, dict) and _row_fails_identity(result, item)
        ]
        chunks: List[str] = []
        try:
            for index, payload in enumerate(payloads, start=1):
                gen_cfg = payload.get("generationConfig") or {}
                schema = gen_cfg.get("responseJsonSchema") or {}
                logger.info(
                    "PHASE2_RECOVERY request document=%s attempt=%s crop=%s/%s responseMimeType=%s schema_sent=%s schema_title=%s targeted_count=%s targeted=%s",
                    filename or result.get("source_file") or "",
                    attempt,
                    index,
                    len(payloads),
                    gen_cfg.get("responseMimeType"),
                    bool(schema),
                    schema.get("title") if isinstance(schema, dict) else None,
                    len(targeted),
                    " | ".join(targeted),
                )
                chunks.append(
                    _recovery_text_for_payload(
                        payload,
                        caller(payload),
                    )
                )
                one = _parse_recovery_text(chunks[-1])
                if one is None:
                    logger.info(
                        "PHASE2_RECOVERY crop_result document=%s attempt=%s crop=%s result=FAIL text_length=%s",
                        filename or result.get("source_file") or "",
                        attempt,
                        index,
                        len(chunks[-1] or ""),
                    )
                else:
                    logger.info(
                        "PHASE2_RECOVERY crop_result document=%s attempt=%s crop=%s result=PASS returned=%s",
                        filename or result.get("source_file") or "",
                        attempt,
                        index,
                        " | ".join(_returned_rows(one)),
                    )
            text = _combine_recovery_texts(chunks)
        except RecoveryProviderError as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "PHASE2_RECOVERY document=%s failure=%s strategy=%s attempt=%s result=PROVIDER_%s duration_ms=%s",
                filename or result.get("source_file") or "",
                FailureType.PROVIDER_ERROR.value,
                policy.strategy,
                attempt,
                exc.code,
                duration_ms,
            )
            return _annotate(
                result,
                status="unresolved",
                failure=FailureType.PROVIDER_ERROR,
                policy=recovery_policy(FailureType.PROVIDER_ERROR),
                attempts=attempt - 1,
                recovery_log=recovery_log,
                original_values=original_values,
            )
        parsed = _parse_recovery_text(text)
        if parsed is None:
            logger.info(
                "PHASE2_RECOVERY schema_validation document=%s attempt=%s result=FAIL text_length=%s",
                filename or result.get("source_file") or "",
                attempt,
                len(text or ""),
            )
        else:
            returned = _returned_rows(parsed)
            logger.info(
                "PHASE2_RECOVERY schema_validation document=%s attempt=%s result=PASS text_length=%s rows=%s returned=%s",
                filename or result.get("source_file") or "",
                attempt,
                len(text or ""),
                len(parsed.line_items),
                " | ".join(returned),
            )
        candidate = _candidate_from_text(result, text, policy.strategy)
        accepted = False
        if candidate is not None:
            from services.source_grounding import apply_source_grounding

            apply_source_grounding(candidate, file_bytes, filename)
            accepted = _candidate_accepted(candidate)
        duration_ms = int((time.perf_counter() - started) * 1000)
        recovery_log.append(
            {
                "attempt": attempt,
                "accepted": accepted,
                "parsed": parsed is not None,
                "returned": _returned_rows(parsed) if parsed is not None else [],
            }
        )
        logger.info(
            "PHASE2_RECOVERY document=%s failure=%s strategy=%s attempt=%s result=%s duration_ms=%s",
            filename or result.get("source_file") or "",
            failure.value,
            policy.strategy,
            attempt,
            "SUCCESS" if accepted else "FAIL",
            duration_ms,
        )
        if accepted and candidate is not None:
            return _annotate(
                candidate,
                status="recovered",
                failure=failure,
                policy=policy,
                attempts=attempt,
                recovery_log=recovery_log,
                original_values=original_values,
            )
    return _annotate(
        result,
        status="unresolved",
        failure=failure,
        policy=policy,
        attempts=attempts,
        recovery_log=recovery_log,
        original_values=original_values,
    )


def build_targeted_payloads(
    result: Dict[str, Any],
    *,
    strategy: str,
    file_bytes: bytes = b"",
    filename: str = "",
) -> List[Dict[str, Any]]:
    """One request per safe evidence object. Otherwise the existing full document."""
    if strategy != "targeted_stock_extraction":
        return [
            build_recovery_payload(
                result, strategy=strategy, file_bytes=file_bytes, filename=filename
            )
        ]
    failing = _failing_rows(result)
    evidences = _recovery_evidence(result, file_bytes, filename, failing)
    safe = [item for item in evidences if item.is_safe]
    if failing and len(safe) == len(failing) and all(item.kind == "IMAGE_CROP" for item in safe):
        return _image_crop_payloads(result, safe)
    if failing and len(safe) == len(failing) and all(
        item.kind in {"TEXT", "EXCEL_CELLS", "FULL_DOCUMENT_TEXT"} for item in safe
    ):
        logger.info(
            "PHASE2_RECOVERY evidence=%s count=%s file=%s",
            safe[0].kind,
            len(safe),
            filename,
        )
        return [_text_evidence_payload(result, strategy, item) for item in safe]
    logger.info("PHASE2_RECOVERY evidence=full_document file=%s", filename)
    return [
        build_recovery_payload(
            result,
            strategy=strategy,
            file_bytes=file_bytes,
            filename=filename,
            use_row_crops=False,
        )
    ]


def build_recovery_payload(
    result: Dict[str, Any],
    *,
    strategy: str,
    file_bytes: bytes = b"",
    filename: str = "",
    use_row_crops: bool = True,
) -> Dict[str, Any]:
    """Phase 2 payload. Existing invoice and fallback callers do not use this."""
    prompt = _prompt_for(result, strategy)
    parts: List[Dict[str, Any]] = [{"text": prompt}]
    if strategy == "targeted_stock_extraction" and use_row_crops:
        parts.extend(_targeted_stock_evidence(result, file_bytes, filename))
    else:
        parts.extend(_safe_document_parts(file_bytes, filename))
    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": _recovery_generation_config(),
    }


def _recovery_generation_config() -> Dict[str, Any]:
    return {
        "temperature": 0,
        "maxOutputTokens": 8192,
        "responseMimeType": "application/json",
        "responseJsonSchema": sales_recovery_json_schema(),
    }


def merge_stock_recovery(
    original: Dict[str, Any],
    recovered: SalesExtractionOutput,
) -> Dict[str, Any]:
    """Copy stock qty fields onto a new statement. Names and packing stay."""
    candidate = copy.deepcopy(original)
    rows = [item for item in (candidate.get("line_items") or []) if isinstance(item, dict)]
    used = set()
    for recovered_line in recovered.line_items:
        index = _match_row(rows, recovered_line.fixed.product_name, used)
        if index is None:
            continue
        used.add(index)
        target = rows[index]
        extra = target.get("extra")
        if not isinstance(extra, dict):
            extra = {}
            target["extra"] = extra
        changed: Dict[str, Any] = {}
        for field in _STOCK_FIELDS + (
            "opening_value",
            "receipts_value",
            "sales_value",
            "closing_value",
        ):
            value = getattr(recovered_line.fixed, field)
            if value is None or target.get(field) == value:
                continue
            changed[field] = target.get(field)
            target[field] = value
        if changed:
            previous = extra.get("recovery_original")
            if not isinstance(previous, dict):
                previous = {}
            previous.update(changed)
            extra["recovery_original"] = previous
        if recovered_line.dynamic:
            _add_new_dynamic(extra, recovered_line.dynamic)
    if isinstance(recovered.dynamic, dict) and recovered.dynamic:
        totals = candidate.setdefault("totals", {})
        if not isinstance(totals, dict):
            totals = {}
            candidate["totals"] = totals
        extra = totals.get("extra")
        if not isinstance(extra, dict):
            extra = {}
            totals["extra"] = extra
        _add_new_dynamic(extra, recovered.dynamic)
    return _revalidate_identity(candidate)


def _candidate_from_text(
    original: Dict[str, Any],
    text: str,
    strategy: str,
) -> Optional[Dict[str, Any]]:
    parsed = _parse_recovery_text(text)
    if parsed is None:
        return None
    if strategy == "targeted_stock_extraction":
        return merge_stock_recovery(original, parsed)
    return _merge_returned_numbers(original, parsed)


def _merge_returned_numbers(
    original: Dict[str, Any],
    recovered: SalesExtractionOutput,
) -> Dict[str, Any]:
    """Fill only numeric fields the model actually returned. Do not rename products."""
    candidate = copy.deepcopy(original)
    rows = [item for item in (candidate.get("line_items") or []) if isinstance(item, dict)]
    number_fields = _STOCK_FIELDS + (
        "opening_value",
        "receipts_value",
        "sales_value",
        "closing_value",
    )
    used = set()
    for recovered_line in recovered.line_items:
        index = _match_row(rows, recovered_line.fixed.product_name, used)
        if index is None:
            continue
        used.add(index)
        target = rows[index]
        for field in number_fields:
            value = getattr(recovered_line.fixed, field)
            if value is not None:
                target[field] = value
        if recovered_line.dynamic:
            extra = target.get("extra") if isinstance(target.get("extra"), dict) else {}
            _add_new_dynamic(extra, recovered_line.dynamic)
            target["extra"] = extra
    return _revalidate_identity(candidate)


def _add_new_dynamic(extra: Dict[str, Any], incoming: Dict[str, Any]) -> None:
    """Add a dynamic key only when the row does not already have it.

    An existing value, including zero, is kept. Null does not erase it.
    """
    for key, value in incoming.items():
        if key in extra or value is None:
            continue
        extra[key] = value


def _candidate_accepted(candidate: Dict[str, Any]) -> bool:
    from services.base_validator import validate_sales_result
    from services.schemas import normalize_sales_statement

    validation = validate_sales_result(candidate)
    phase1 = candidate.get("phase1") if isinstance(candidate.get("phase1"), dict) else {}
    phase1 = dict(phase1)
    phase1["normalized"] = normalize_sales_statement(candidate)
    phase1["validation"] = validation
    candidate["phase1"] = phase1
    return bool(validation.get("accepted"))


def _revalidate_identity(candidate: Dict[str, Any]) -> Dict[str, Any]:
    from services.sales_statement_extractor import _apply_stock_identity_validation

    return _apply_stock_identity_validation(candidate)


def _returned_rows(parsed: SalesExtractionOutput) -> List[str]:
    returned = []
    for line in parsed.line_items:
        fixed = line.fixed
        returned.append(
            "{name} opening={opening} receipts={receipts} sales={sales} stock_out={stock_out} closing={closing}".format(
                name=fixed.product_name or "",
                opening=fixed.opening_qty,
                receipts=fixed.receipts_qty,
                sales=fixed.sales_qty,
                stock_out=(line.dynamic or {}).get("stock_out_qty"),
                closing=fixed.closing_qty,
            )
        )
    return returned


def _combine_recovery_texts(chunks: List[str]) -> str:
    if len(chunks) <= 1:
        return chunks[0] if chunks else ""
    items = []
    for chunk in chunks:
        parsed = _parse_recovery_text(chunk)
        if parsed is None:
            continue
        for line in parsed.line_items:
            items.append(line.model_dump(mode="json"))
    if not items:
        return ""
    return json.dumps({"line_items": items})


def _parse_recovery_text(text: str) -> Optional[SalesExtractionOutput]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[: -3].strip()
    if not cleaned:
        return None
    try:
        return SalesExtractionOutput.model_validate_json(cleaned)
    except (ValidationError, HybridSchemaError, ValueError):
        return None


def _prompt_for(result: Dict[str, Any], strategy: str) -> str:
    if strategy == "targeted_stock_extraction":
        return _stock_prompt(result)
    text = (
        "Re-read this secondary sales statement. Return JSON matching the schema. "
        "Use null when a value is not printed. Use 0 only when the document prints zero. "
        "Do not invent products.\n"
        + _row_block(result, failing_only=False)
    )
    context = _validation_context(result)
    if context:
        text += "\n" + context
    mapping = _source_prompt_mapping(result)
    if mapping:
        from services.focused_row_request import extraction_contract

        text = extraction_contract(mapping) + "\n" + text
    return text


def _mapping_for_headers(result: Dict[str, Any], headers: Any) -> Dict[str, str]:
    """Safe labels for headers on this evidence. Alignment lives outside this runner."""
    from services.evidence_mapping import align_evidence_columns
    from services.stock_identity_profiles import select_profile

    profile = select_profile(result)
    aligned = align_evidence_columns(profile, profile.column_mappings, headers or [])
    logger.info(
        "PHASE2_RECOVERY evidence_alignment source=%s matched=%s unmatched=%s",
        aligned.alignment_source,
        len(aligned.matched),
        len(aligned.unmatched_headers),
    )
    if aligned.matched:
        return aligned.matched
    if profile.profile_id == "swil_receipt_issue":
        from services.column_mapping import header_key

        present = {header_key(header) for header in (headers or [])}
        mapping = _swil_column_mapping()
        if not present:
            return mapping
        return {
            header: field
            for header, field in mapping.items()
            if header_key(header) in present
        }
    return {}


def _swil_column_mapping() -> Dict[str, str]:
    """Printed Swil headers. Total Qty is not mapped to sales."""
    return {
        "Opening Qty": "opening_qty",
        "Receipt Qty": "receipts_qty",
        "Issue/Sales Qty": "sales_qty",
        "Closing Qty": "closing_qty",
    }


def _stock_prompt(result: Dict[str, Any], only_product: Optional[str] = None) -> str:
    from services.focused_row_request import extraction_contract

    mapping = None
    if (
        _method(result) == "swil_receipt_pur_value_vision"
        or _layout(result) == "swil_receipt_pur_value"
    ):
        mapping = _swil_column_mapping()
    return (
        extraction_contract(mapping)
        + "\n"
        + _targeted_names(result, only_product)
        + _validation_context(result, only_product)
    )


def _validation_context(result: Dict[str, Any], only_product: Optional[str] = None) -> str:
    """Headers, extracted values, and the exact error. The model still reads the page."""
    wanted = _norm(only_product)
    lines = []
    swil = _method(result) == "swil_receipt_pur_value_vision" or _layout(result) == "swil_receipt_pur_value"
    for item in _context_rows(result):
        name = str(item.get("product_name") or "")
        if wanted and _norm(name) != wanted:
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        total = extra.get("total_stock")
        if total in (None, ""):
            total = extra.get("total_stock_qty")
        lines.append(
            "{name} | extracted opening={opening} receipt={receipts} total={total} issue={sales} closing={closing} | expected_closing={expected} | validation_error={error}".format(
                name=name,
                opening=_shown(item.get("opening_qty")),
                receipts=_shown(item.get("receipts_qty")),
                total=_shown(total),
                sales=_shown(item.get("sales_qty")),
                closing=_shown(item.get("closing_qty")),
                expected=_shown(extra.get("expected_closing")),
                error=_row_error(extra),
            )
        )
    if not lines:
        return ""
    header = ""
    if swil:
        header = (
            "\nPrinted headers: PRODUCT NAME | PACKING | Opening Qty | Opening Value | "
            "Receipt Qty | Receipt/Pur Value | Total Qty | Issue/Sales Qty | "
            "Issue/Sales Value | Closing Qty | Closing Value\n"
            "Total Qty is opening + receipt, not sales. Issue/Sales Qty is the issue column. "
            "Do not subtract scheme or stock-out for this layout.\n"
        )
    return (
        header
        + "\nValidation context for the attached page or row image. "
        "Read the printed cell. Do not calculate a replacement quantity.\n"
        + "\n".join(lines)
        + "\nReturn corrected fields only. Put evidence (printed header and the text you read), "
        "confidence from 0 to 1, and a brief explanation in dynamic. "
        "Use null when the cell is blank or unreadable."
    )


def _layout(result: Dict[str, Any]) -> str:
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    return str(extra.get("layout") or "")


def _original_qty_snapshot(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    snapshot = []
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        snapshot.append(
            {
                "product_name": item.get("product_name"),
                "opening_qty": item.get("opening_qty"),
                "receipts_qty": item.get("receipts_qty"),
                "sales_qty": item.get("sales_qty"),
                "closing_qty": item.get("closing_qty"),
                "total_stock": extra.get("total_stock"),
                "validation_error": extra.get("validation_error"),
            }
        )
    return snapshot


def _row_block(result: Dict[str, Any], *, failing_only: bool) -> str:
    lines = []
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        if failing_only and not _row_fails_identity(result, item):
            continue
        lines.append(
            "{name} | opening={opening} receipts={receipts} sales={sales} closing={closing}".format(
                name=item.get("product_name") or "",
                opening=_shown(item.get("opening_qty")),
                receipts=_shown(item.get("receipts_qty")),
                sales=_shown(item.get("sales_qty")),
                closing=_shown(item.get("closing_qty")),
            )
        )
    if not lines:
        return "No product rows were extracted."
    return "Rows:\n" + "\n".join(lines)


def _failing_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    from services.stock_identity_profiles import failed_rows

    return failed_rows(result)


def _context_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Identity failures plus rows whose values disagree with the source file."""
    rows: List[Dict[str, Any]] = []
    seen = set()
    for item in list(_failing_rows(result)) + _source_discrepancy_rows(result):
        key = id(item)
        if key in seen:
            continue
        seen.add(key)
        rows.append(item)
    return rows


def _source_discrepancy_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    seen = set()
    buckets = []
    if isinstance(result.get("line_items"), list):
        buckets.append(result["line_items"])
    for statement in result.get("statements") or []:
        if isinstance(statement, dict) and isinstance(statement.get("line_items"), list):
            buckets.append(statement["line_items"])
    for bucket in buckets:
        for item in bucket:
            if not isinstance(item, dict) or id(item) in seen:
                continue
            seen.add(id(item))
            extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            if extra.get("source_discrepancy") is True:
                rows.append(item)
    return rows


def _source_prompt_mapping(result: Dict[str, Any]) -> Dict[str, str]:
    """Printed headers already stored on a discrepant row. Nothing is inferred."""
    from services.focused_row_request import CANONICAL_FIELDS

    mapping: Dict[str, str] = {}
    for item in _source_discrepancy_rows(result):
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        columns = extra.get("source_columns") if isinstance(extra.get("source_columns"), dict) else {}
        for field, header in columns.items():
            label = str(header or "").strip()
            if label and field in CANONICAL_FIELDS and label not in mapping:
                mapping[label] = field
    return mapping


def _row_error(extra: Dict[str, Any]) -> str:
    parts = []
    validation_error = extra.get("validation_error")
    source_error = extra.get("source_validation_error")
    if validation_error:
        parts.append(str(validation_error))
    if source_error and str(source_error) not in parts:
        parts.append(str(source_error))
    return "; ".join(parts) or "stock identity failed"


def _targeted_names(result: Dict[str, Any], only_product: Optional[str]) -> str:
    names = []
    wanted = _norm(only_product)
    for item in _context_rows(result):
        name = str(item.get("product_name") or "")
        if wanted and _norm(name) != wanted:
            continue
        names.append(name)
    if not names:
        return "No product rows were extracted."
    return "Product rows:\n" + "\n".join(names)


def _row_fails_identity(result: Dict[str, Any], item: Dict[str, Any]) -> bool:
    """Row failure comes from the same profile the validator selected."""
    from services.stock_identity_profiles import row_fails

    return row_fails(result, item)


def _shown(value: Any) -> str:
    if value is None:
        return "null"
    return str(value)


def _match_row(
    rows: List[Dict[str, Any]],
    product_name: Optional[str],
    used: set,
) -> Optional[int]:
    wanted = _norm(product_name)
    if wanted:
        for index, row in enumerate(rows):
            if index in used:
                continue
            if _norm(row.get("product_name")) == wanted:
                return index
    unused = [index for index in range(len(rows)) if index not in used]
    if len(unused) == 1 and len(rows) == 1:
        return unused[0]
    return None


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _method(result: Dict[str, Any]) -> str:
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    return str(extra.get("extraction_method") or "")


def _annotate(
    result: Dict[str, Any],
    *,
    status: str,
    failure: Optional[FailureType],
    policy: Optional[RecoveryPolicy],
    attempts: int,
    recovery_log: Optional[List[Dict[str, Any]]] = None,
    original_values: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    marked = copy.deepcopy(result)
    # Keep status as the Phase 2 source of truth. Also expose applied/accepted
    # so Phase 4 decide_final_status can emit RECOVERED_AND_VERIFIED.
    recovered = status == "recovered"
    marked["phase2"] = {
        "status": status,
        "failure_type": failure.value if failure else None,
        "strategy": policy.strategy if policy else None,
        "attempts": attempts,
        "applied": recovered,
        "accepted": recovered,
        "recovery_log": list(recovery_log or []),
        "original_values": list(original_values or []),
        "manual_review": "UNRESOLVED" if status == "unresolved" else None,
    }
    return marked


def _recovery_evidence(
    result: Dict[str, Any],
    file_bytes: bytes,
    filename: str,
    rows: List[Dict[str, Any]],
):
    """Ask the evidence selector. Rows already come from the identity profile."""
    from services.evidence_selector import select_recovery_evidence
    from services.stock_identity_profiles import select_profile

    phase1 = result.get("phase1") if isinstance(result.get("phase1"), dict) else {}
    return select_recovery_evidence(
        file_bytes=file_bytes,
        filename=filename,
        failure_type="targeted_stock_extraction",
        failing_rows=rows,
        identity_profile=select_profile(result),
        existing_evidence=phase1.get("evidence"),
    )


def _image_crop_payloads(result: Dict[str, Any], evidences) -> List[Dict[str, Any]]:
    from services.focused_row_request import build_focused_row_payload, request_fingerprint

    logger.info("PHASE2_RECOVERY evidence=IMAGE_CROP count=%s", len(evidences))
    payloads = []
    for evidence in evidences:
        mapping = _mapping_for_headers(result, evidence.headers)
        logger.info(
            "PHASE2_RECOVERY column_mapping product=%s mapped=%s",
            evidence.row_id or "",
            " | ".join("%s=%s" % item for item in mapping.items()) or "none",
        )
        payload = build_focused_row_payload(
            evidence.png,
            column_mapping=mapping,
            headers=evidence.headers,
            row_label=evidence.row_id,
        )
        text_part = payload["contents"][0]["parts"][0]
        text_part["text"] = (text_part.get("text") or "") + _validation_context(
            result, evidence.row_id
        )
        fingerprint = request_fingerprint(
            payload,
            product=evidence.row_id or "",
            width=evidence.width or 0,
            height=evidence.height or 0,
        )
        logger.info(
            "PHASE2_RECOVERY crop_request product=%s crop_width=%s crop_height=%s mime=%s crop_sha256=%s prompt_sha256=%s schema_sha256=%s model=%s",
            fingerprint["product"],
            fingerprint["crop_width"],
            fingerprint["crop_height"],
            fingerprint["mime"],
            fingerprint["crop_sha256"],
            fingerprint["prompt_sha256"],
            fingerprint["schema_sha256"],
            fingerprint["model"],
        )
        payloads.append(payload)
    return payloads


def _text_evidence_payload(
    result: Dict[str, Any],
    strategy: str,
    evidence,
) -> Dict[str, Any]:
    """Reader contract plus the selector's text or cells. No crop logic."""
    from services.focused_row_request import extraction_contract

    if strategy == "targeted_stock_extraction":
        mapping = _mapping_for_headers(result, getattr(evidence, "headers", None))
        logger.info(
            "PHASE2_RECOVERY column_mapping product=%s mapped=%s",
            getattr(evidence, "row_id", "") or "",
            " | ".join("%s=%s" % item for item in mapping.items()) or "none",
        )
        prompt = extraction_contract(mapping, getattr(evidence, "headers", None))
        prompt += "\n" + _targeted_names(result, evidence.row_id)
        prompt += _validation_context(result, evidence.row_id)
    else:
        prompt = _prompt_for(result, strategy)
    parts: List[Dict[str, Any]] = [{"text": prompt}]
    if evidence.text:
        parts.append({"text": evidence.text})
    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": _recovery_generation_config(),
    }


def _recovery_text_for_payload(
    payload: Dict[str, Any],
    text: str,
) -> str:
    """Project every recovery response through the same printed-header guard."""
    from services.focused_row_request import mapping_declared_in_prompt
    from services.recovery_projection import project_sales_text

    prompt = ""
    contents = payload.get("contents") or []
    if contents and isinstance(contents[0], dict):
        parts = contents[0].get("parts") or []
        if parts and isinstance(parts[0], dict):
            prompt = parts[0].get("text") or ""
    mapping = mapping_declared_in_prompt(prompt)
    schema = (payload.get("generationConfig") or {}).get("responseJsonSchema") or {}
    if isinstance(schema, dict) and schema.get("title") == "SemanticRow":
        wrapped = _semantic_row_as_sales_json(text, mapping)
        if wrapped is not None:
            return wrapped
    return project_sales_text(text, mapping)


def _semantic_row_as_sales_json(
    text: str,
    column_mapping: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Map one read row onto SalesExtractionOutput. Nulls stay null."""
    from services.focused_row_request import project_semantic_row
    from services.recovery_projection import unwrap_model_text

    try:
        data = json.loads(unwrap_model_text(text) or "null")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "line_items" in data:
        return None
    return json.dumps(project_semantic_row(data, column_mapping))


def _targeted_stock_evidence(
    result: Dict[str, Any],
    file_bytes: bytes,
    filename: str,
) -> List[Dict[str, Any]]:
    """Safe selector evidence. Anything unsafe keeps the existing document parts."""
    rows = _failing_rows(result)
    evidences = _recovery_evidence(result, file_bytes, filename, rows)
    if not evidences or any(not item.is_safe for item in evidences):
        return _safe_document_parts(file_bytes, filename)
    parts: List[Dict[str, Any]] = []
    for evidence in evidences:
        if evidence.kind == "IMAGE_CROP" and evidence.png:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": evidence.mime_type or "image/png",
                        "data": evidence.png,
                    }
                }
            )
        elif evidence.text:
            parts.append({"text": evidence.text})
    if not parts:
        return _safe_document_parts(file_bytes, filename)
    return parts


def _safe_document_parts(file_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    ext = Path(filename or "").suffix.lower()
    if not file_bytes or not ext:
        return []
    try:
        from services.gemini_extraction_fallback import _document_parts

        return _document_parts(file_bytes, ext) or []
    except Exception:
        logger.info("PHASE2_RECOVERY document parts unavailable file=%s", filename)
        return []


def _live_gemini_caller(payload: Dict[str, Any]) -> str:
    from app import call_gemini_with_quota, get_current_model_config
    from services.sales_statement_extractor import _gemini_response_text

    model_config = get_current_model_config()
    schema = (payload.get("generationConfig") or {}).get("responseJsonSchema") or {}
    model_name = model_config["name"]
    if isinstance(schema, dict) and schema.get("title") in {"SemanticRow", "PrintedColumns"}:
        from services.focused_row_request import FOCUSED_ROW_MODEL

        model_name = FOCUSED_ROW_MODEL
    response = call_gemini_with_quota(
        model=model_name,
        payload=payload,
        timeout=int(model_config.get("timeout") or 60),
        request_type="vision",
    )
    if not response:
        raise RecoveryProviderError("unavailable")
    return _gemini_response_text(response)
