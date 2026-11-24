"""DocumentIntelligencePipeline — Phase 4 orchestration foundation.

Preserves the existing parser cascade, Phase 1 validation, and Phase 2
recovery. Adds route → evidence → layout → strategy → verify → decide.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from services.document_intelligence.confidence import build_confidence
from services.document_intelligence.decisions import decide_final_status
from services.document_intelligence.observability import (
    PipelineTimer,
    build_observability,
)
from services.document_intelligence.validation_stack import build_validation_stack
from services.document_intelligence.verification import independently_verify
from services.document_router import DocumentRoute, route_document
from services.evidence import DocumentEvidence, evidence_for_route
from services.extraction.parser_registry import ParserRegistry, default_registry
from services.layout.detector import LayoutUnderstanding, understand_layout
from services.ocr.orchestrator import OCROrchestrator

logger = logging.getLogger(__name__)


class DocumentIntelligencePipeline:
    """Central orchestration. Does not replace named parsers."""

    def __init__(
        self,
        *,
        registry: Optional[ParserRegistry] = None,
        ocr: Optional["OCROrchestrator"] = None,
    ) -> None:
        self.registry = registry or default_registry()
        # Reuse the process OCR singleton so VL/Paddle workers are not
        # constructed twice for the same request (pipeline + image probes).
        if ocr is not None:
            self.ocr = ocr
        else:
            from services.ocr.runtime import get_orchestrator

            self.ocr = get_orchestrator()

    def run(self, file_bytes: bytes, filename: str) -> Dict[str, Any]:
        from services import sales_statement_extractor as sse

        timer = PipelineTimer()
        name = filename or "upload"

        # 1) Route
        route = route_document(
            file_bytes,
            name,
            requested_product="sales_statement",
            collect_page_stats=True,
        )
        timer.mark("route")

        # 2) Evidence (lightweight; no OCR for digital native PDFs)
        evidence = evidence_for_route(
            file_bytes,
            name,
            route,
            retain_tokens=False,
            retain_text=False,
        )
        timer.mark("evidence")

        # 3) OCR policy — short-circuit when native text is enough.
        # For OCR-required inputs, run the primary engine (PaddleOCR-VL by
        # default) and attach evidence. Do not silently mark fallback verified.
        ocr_bundle: Dict[str, Any] = {
            "policy": self.ocr.settings.__dict__,
            "invoked_here": False,
        }
        ocr_disagreement = False
        unverified_ocr_fallback = False
        engines_used: list = []
        ocr_evidence_summary: Optional[Dict[str, Any]] = None
        # Full evidence (text + tables) for Phase1 cell/column gates.
        # Summary strips page text and is API-safe only.
        ocr_evidence_for_validation: Optional[Dict[str, Any]] = None
        repaired_ocr_texts: list = []
        repaired_cells_all: list = []
        repaired_table_htmls: list = []
        page_bundles: list = []
        preprocess_meta_all: list = []
        low_confidence_all: list = []
        ocr_recovery_meta_all: list = []
        ocr_page_timing_all: list = []
        ocr_timed_out = False
        if _should_run_ocr(route):
            from services.ocr.runtime import document_evidence_from_bundle

            page_images = _ocr_page_images(file_bytes, name, route)
            for page_no, image_bytes in page_images:
                bundle, page_meta = _recognize_page_resilient(
                    self.ocr,
                    image_bytes,
                    page_no=page_no,
                )
                if page_meta.get("timed_out"):
                    ocr_timed_out = True
                ocr_page_timing_all.append(
                    {
                        "page": page_no,
                        "duration_ms": page_meta.get("duration_ms"),
                        "engine": page_meta.get("ocr_engine"),
                        "timed_out": bool(page_meta.get("timed_out")),
                        "cache_hit": bool(page_meta.get("cache_hit")),
                        "outcome": page_meta.get("outcome"),
                        "text_length": page_meta.get("text_length"),
                        "recovery": (page_meta.get("ocr_recovery") or {}).get(
                            "accepted_engine"
                        ),
                    }
                )
                if page_meta.get("preprocess"):
                    preprocess_meta_all.append(page_meta["preprocess"])
                if page_meta.get("ocr_recovery"):
                    ocr_recovery_meta_all.append(
                        {"page": page_no, **dict(page_meta["ocr_recovery"])}
                    )
                if page_meta.get("low_confidence_cells"):
                    low_confidence_all.extend(page_meta["low_confidence_cells"])
                # Optional evidence-gated secondary OCR (OCR_SECONDARY_REPAIR;
                # default OFF). Never invents quantities from stock equations.
                page_cells = _maybe_secondary_repair_bundle(
                    bundle,
                    document_id=str(getattr(evidence, "document_id", None) or name),
                    # Always crop from the original page image (not preprocessed /
                    # zoomed), so secondary-repair bboxes stay aligned.
                    image_bytes=image_bytes,
                    file_bytes=file_bytes,
                    file_kind=getattr(route, "file_kind", None),
                    page_index=max(0, int(page_no) - 1),
                )
                if page_cells:
                    repaired_cells_all.extend(page_cells)
                if getattr(bundle, "primary", None) and bundle.primary.text:
                    repaired_ocr_texts.append(bundle.primary.text)
                table_html = _table_html_from_ocr_bundle(bundle)
                if table_html:
                    repaired_table_htmls.append(table_html)
                page_bundles.append(bundle)
                engines_used.extend(bundle.engines_used)
                ocr_disagreement = ocr_disagreement or bundle.disagreement
                unverified_ocr_fallback = (
                    unverified_ocr_fallback or bundle.unverified_fallback
                )
            if page_bundles:
                first = page_bundles[0]
                doc_ev = document_evidence_from_bundle(
                    first, document_id=evidence.document_id
                )
                # Merge subsequent pages into the typed evidence document.
                for extra in page_bundles[1:]:
                    more = document_evidence_from_bundle(extra)
                    doc_ev.pages.extend(more.pages)
                    doc_ev.unverified_fallback = (
                        doc_ev.unverified_fallback or more.unverified_fallback
                    )
                ocr_evidence_for_validation = doc_ev.to_dict()
                ocr_evidence_summary = doc_ev.summary()
                ocr_bundle = {
                    "policy": self.ocr.settings.__dict__,
                    "invoked_here": True,
                    "reason": "ocr_required_primary_engine",
                    "engines_used": list(dict.fromkeys(engines_used)),
                    "disagreement": ocr_disagreement,
                    "unverified_fallback": unverified_ocr_fallback,
                    "page_count": len(page_bundles),
                    "status": first.metadata.get("status"),
                    "error": first.metadata.get("error"),
                    "timed_out": _bundles_timed_out(page_bundles) or ocr_timed_out,
                    "evidence": ocr_evidence_summary,
                    "page_timing": ocr_page_timing_all,
                    "ocr_recovery": ocr_recovery_meta_all,
                }
            else:
                ocr_bundle = {
                    "policy": self.ocr.settings.__dict__,
                    "invoked_here": False,
                    "reason": "ocr_required_but_no_page_images",
                    "unverified_fallback": False,
                }
                engines_used = [self.ocr.settings.primary_engine]
        else:
            ocr_bundle["reason"] = "native_text_short_circuit"
        timer.mark("ocr_policy")

        # 4) Layout understanding
        layout = understand_layout(route, evidence)
        timer.mark("layout")

        # 5) Strategy selection (named parsers preferred)
        strategy = self.registry.select(route, layout)
        timer.mark("strategy")

        # 6) Extraction — existing cascade (load-bearing).
        # When DI already produced OCR text / VL table HTML, reuse it so extract
        # prefers HTML table parse over Gemini (secondary repair optional).
        from services.ocr.secondary_repair import (
            overlay_secondary_repairs_onto_result,
            secondary_repair_enabled,
        )
        from services.ocr.ocr_text_override import ocr_recovery_state, ocr_text_override
        from services.vl_html_table import extract_vl_table_html

        combined_ocr = "\n\n".join(t for t in repaired_ocr_texts if t)
        combined_table_html = "\n\n".join(t for t in repaired_table_htmls if t)
        if not combined_table_html:
            combined_table_html = extract_vl_table_html(combined_ocr)
        # Prefer full OCR text (banner + table) so identity/period survive.
        # Table-HTML-only overrides drop prefatory stockist/company lines.
        if combined_ocr and (
            "<table" in combined_ocr.lower() or not combined_table_html
        ):
            override_payload = combined_ocr
        else:
            override_payload = combined_table_html or combined_ocr
        recovery_accepted = any(
            isinstance(m, dict) and m.get("accepted_engine")
            for m in ocr_recovery_meta_all
        )
        recovery_state = {
            "attempted": bool(ocr_recovery_meta_all),
            "accepted": recovery_accepted,
            "pages": ocr_recovery_meta_all,
            "primary_timed_out": bool(ocr_timed_out and not recovery_accepted),
            "block_ungrounded_vision": bool(
                ocr_recovery_meta_all and not recovery_accepted and not override_payload
            ),
        }
        with ocr_recovery_state(recovery_state):
            if override_payload:
                with ocr_text_override(override_payload):
                    result = sse.extract_sales_statement_core(file_bytes, name)
                if (
                    secondary_repair_enabled()
                    and isinstance(result, dict)
                    and repaired_cells_all
                ):
                    result = overlay_secondary_repairs_onto_result(
                        result, repaired_cells_all
                    )
            else:
                result = sse.extract_sales_statement_core(file_bytes, name)
        timer.mark("extraction")

        if isinstance(result, dict):
            if preprocess_meta_all or ocr_recovery_meta_all:
                extra = result.setdefault("totals", {}).setdefault("extra", {})
                if isinstance(extra, dict):
                    if preprocess_meta_all:
                        extra["page_preprocess"] = preprocess_meta_all
                    if ocr_recovery_meta_all:
                        extra["ocr_recovery"] = ocr_recovery_meta_all
                        if recovery_accepted:
                            extra["ocr_source"] = next(
                                (
                                    m.get("accepted_engine")
                                    for m in ocr_recovery_meta_all
                                    if m.get("accepted_engine")
                                ),
                                None,
                            )
            if low_confidence_all:
                extra = result.setdefault("totals", {}).setdefault("extra", {})
                if isinstance(extra, dict):
                    extra["low_confidence_cells"] = low_confidence_all[:50]
                    for cell in low_confidence_all:
                        cell["status"] = "UNRESOLVED_CELL"
            method = str(
                ((result.get("totals") or {}).get("extra") or {}).get(
                    "extraction_method"
                )
                or result.get("extraction_method")
                or ""
            )
            if combined_table_html and method == "gemini_extraction_fallback":
                logger.warning(
                    "pipeline hard guard: table_html present but method=%s file=%s "
                    "(soft-fail; leaving extract for validators)",
                    method,
                    name,
                )
                extra = result.setdefault("totals", {}).setdefault("extra", {})
                if isinstance(extra, dict):
                    extra["pipeline_guard"] = "gemini_with_table_html_present"

        if isinstance(result, dict):
            from services.source_grounding import apply_source_grounding

            try:
                ran_ocr = _should_run_ocr(route)
                apply_source_grounding(
                    result,
                    file_bytes,
                    name,
                    ocr_text=(
                        (result.get("totals") or {}).get("extra", {}).get("ocr_table_html")
                        or (combined_ocr if ran_ocr else None)
                    ),
                )
                if ran_ocr:
                    result["_grounding_ocr_attempted"] = True
                    result["_grounding_ocr_text"] = combined_ocr
            except Exception:
                logger.warning("source grounding skipped for %s", name, exc_info=True)
            else:
                if combined_ocr and "_grounding_ocr_text" not in result:
                    result["_grounding_ocr_text"] = combined_ocr

        if isinstance(result, dict) and ocr_evidence_summary is not None:
            # Keep API payload small; validators receive full evidence below.
            result["ocr_evidence"] = ocr_evidence_summary

        # After Gemini-as-OCR / SmartRouter success, stamp table evidence onto
        # ocr_evidence so EMPTY_OCR_PAGE gates see the recovered HTML.
        if isinstance(result, dict):
            _stamp_ocr_evidence_from_extraction(
                result,
                ocr_evidence_for_validation,
                ocr_evidence_summary,
            )

        # 7) Phase 1 attach (reuse routed evidence)
        result = sse.attach_phase1_architecture(
            result,
            file_bytes,
            name,
            route=route,
            evidence=evidence,
        )
        timer.mark("phase1")

        phase1 = result.get("phase1") if isinstance(result, dict) else None
        validation = (phase1 or {}).get("validation") or {}
        # Re-validate with full OCR evidence so EMPTY_OCR_PAGE / table gates
        # see real page text (summary strips it).
        if isinstance(result, dict) and ocr_evidence_for_validation is not None:
            from services.base_validator import validate_sales_result

            column_headers, column_cells = _geometry_from_ocr_evidence(
                ocr_evidence_for_validation
            )
            validation = validate_sales_result(
                result,
                source_metadata={
                    "requires_ocr": True,
                    "ocr_evidence": ocr_evidence_for_validation,
                    "unverified_ocr_fallback": unverified_ocr_fallback,
                    "ocr_disagreement": ocr_disagreement,
                    "column_headers": column_headers or None,
                    "column_cells": column_cells or None,
                },
            )
            if isinstance(phase1, dict):
                phase1["validation"] = validation
                result["phase1"] = phase1

        # 8) Validation stack view
        stack = build_validation_stack(
            result,
            phase1_validation=validation,
            ocr_disagreement=ocr_disagreement,
            unverified_ocr_fallback=unverified_ocr_fallback,
        )
        timer.mark("validation_stack")

        # 9) Independent verification
        verification = independently_verify(
            result,
            validation=validation,
            ocr_disagreement=ocr_disagreement,
            unverified_ocr_fallback=unverified_ocr_fallback,
        )
        timer.mark("verification")

        # 10) Targeted recovery (existing Phase 2)
        result = sse.maybe_apply_phase2_recovery(result, file_bytes, name)
        # Re-apply evidence-gated secondary repairs after recovery so Phase 2
        # cannot silently wipe corrected quantities.
        if (
            secondary_repair_enabled()
            and isinstance(result, dict)
            and repaired_cells_all
        ):
            result = overlay_secondary_repairs_onto_result(result, repaired_cells_all)
        timer.mark("recovery")

        recovery_meta = {}
        if isinstance(result, dict):
            recovery_meta = result.get("phase2") or result.get("recovery") or {}
            # Re-read validation after recovery may have updated phase1/phase2
            phase1 = result.get("phase1") or phase1 or {}
            validation = phase1.get("validation") or validation
            recovered = bool(
                recovery_meta.get("accepted")
                or recovery_meta.get("applied")
                or str(recovery_meta.get("status") or "").lower() == "recovered"
            )
            if recovered:
                verification = independently_verify(
                    result,
                    validation=validation,
                    ocr_disagreement=ocr_disagreement,
                    unverified_ocr_fallback=unverified_ocr_fallback,
                )
                stack = build_validation_stack(
                    result,
                    phase1_validation=validation,
                    ocr_disagreement=ocr_disagreement,
                    unverified_ocr_fallback=unverified_ocr_fallback,
                )

        # Successful VL HTML parse is not a provider failure when table text
        # exists (provider_error_from_ocr short-circuits on non-empty ocr_text).
        # Do not rewrite OCR timed_out/status — reliability metrics must stay honest.
        method_now = ""
        if isinstance(result, dict):
            method_now = str(
                ((result.get("totals") or {}).get("extra") or {}).get(
                    "extraction_method"
                )
                or result.get("extraction_method")
                or ""
            )
        if method_now == "vl_html_table_parse":
            recovered_text = (
                combined_ocr
                or str(
                    ((result.get("totals") or {}).get("extra") or {}).get(
                        "ocr_table_html"
                    )
                    or ""
                )
                or "vl_html_table_parse"
            )
        else:
            recovered_text = combined_ocr
        provider_error = _provider_error(result) or provider_error_from_ocr(
            ocr_bundle,
            ocr_text=recovered_text,
        )
        extraction_error = _extraction_error(result)

        decision = decide_final_status(
            validation=validation,
            verification=verification,
            recovery=recovery_meta if isinstance(recovery_meta, dict) else {},
            extraction_error=extraction_error,
            provider_error=provider_error,
            ocr_disagreement=ocr_disagreement,
        )
        timer.mark("decision")

        evidence_conf = 0.8 if not route.requires_ocr else 0.55
        if route.file_kind in {"xlsx", "xls"}:
            evidence_conf = 0.85
        confidence = build_confidence(
            layout_confidence=layout.confidence,
            mapping_confidence=0.7 if strategy.get("known_parser_preferred") else 0.35,
            ocr_confidence=None,
            evidence_confidence=evidence_conf,
            schema_ok=bool(validation.get("accepted")),
            business_ok=bool(verification.get("checks", {}).get("stock_identity_ok")),
            cross_engine_agreement=None if not ocr_disagreement else False,
            known_parser=bool(strategy.get("known_parser_preferred")),
        )

        observability = build_observability(
            document_id=evidence.document_id,
            document_type=route.file_kind or "unknown",
            router_decision=route.to_dict(),
            evidence_sources=[evidence.extraction_engine or "native"],
            ocr_engines_used=engines_used,
            ocr_disagreement=ocr_disagreement,
            layout_strategy=layout.strategy,
            extraction_strategy=strategy,
            parser_used=(strategy.get("selected") or {}).get("name"),
            mapping_decisions={"layout_scoped": True},
            validation_results=stack,
            recovery_attempts=recovery_meta if isinstance(recovery_meta, dict) else {},
            final_decision=decision,
            failure_taxonomy=validation.get("primary_failure")
            or validation.get("failure_type"),
            timings=timer.timings(),
        )

        if isinstance(result, dict):
            result["document_intelligence"] = {
                "version": "phase4",
                "layout": layout.to_dict(),
                "strategy": strategy,
                "ocr": ocr_bundle,
                "validation_stack": stack,
                "verification": verification,
                "confidence": confidence,
                "decision": decision,
                "observability": observability,
            }
            # Backward-compatible alias
            result["phase4"] = result["document_intelligence"]
            result.pop("_grounding_ocr_text", None)
            result.pop("_grounding_ocr_attempted", None)
        return result

    def recognize_image(
        self,
        image_bytes: bytes,
        *,
        page: int = 1,
        force_secondary: bool = False,
        low_confidence: bool = False,
    ):
        """Public OCR entry for tests and recovery callers."""
        return self.ocr.recognize(
            image_bytes,
            page=page,
            force_secondary=force_secondary,
            low_confidence=low_confidence,
        )


def _maybe_secondary_repair_bundle(
    bundle: Any,
    *,
    document_id: str,
    image_bytes: bytes,
    file_bytes: Optional[bytes] = None,
    file_kind: Optional[str] = None,
    page_index: int = 0,
) -> list:
    """Apply flagged secondary OCR repairs to primary page text in-place.

    Returns parsed/repaired table cells for downstream extraction overlay.
    PDFs are re-rasterized at secondary-repair DPI (2.5×) from original bytes
    so repairs match canary evidence; page ``image_bytes`` alone may be 2.0×.
    """
    try:
        from services.ocr.secondary_repair import maybe_apply_secondary_repairs
    except Exception:
        return []
    primary = getattr(bundle, "primary", None)
    if primary is None or not getattr(primary, "text", None):
        return []
    original_text = primary.text
    try:
        pdf_bytes = file_bytes if (file_kind == "pdf" and file_bytes) else None
        text2, cells, report = maybe_apply_secondary_repairs(
            document_id,
            primary.text,
            image_bytes=None if pdf_bytes else image_bytes,
            pdf_bytes=pdf_bytes,
            page_index=page_index,
        )
    except Exception as exc:
        # Preserve original extraction text on any repair failure.
        primary.text = original_text
        meta = getattr(bundle, "metadata", None)
        if isinstance(meta, dict):
            meta["secondary_repair_error"] = str(exc)[:200]
        return []
    if text2 != original_text:
        primary.text = text2
    # Observability: repair type + field deltas only (no OCR line/raw snippets).
    public_report = report.to_dict(redact_snippets=True)
    meta = getattr(primary, "metadata", None)
    if isinstance(meta, dict):
        meta["secondary_repair"] = public_report
    bundle_meta = getattr(bundle, "metadata", None)
    if isinstance(bundle_meta, dict):
        bundle_meta["secondary_repair"] = public_report
    return list(cells or [])


def _geometry_from_ocr_evidence(
    ocr_evidence: Dict[str, Any],
) -> tuple:
    """Build synthetic column spans from VL HTML tables (col index → x).

    VL often lacks per-cell bboxes; column index still lets geometry gates
    detect ambiguous/shifted header ownership without inventing values.
    """
    from services.ground_truth.html_table import parse_html_tables

    pages = ocr_evidence.get("pages") if isinstance(ocr_evidence, dict) else None
    if not isinstance(pages, list):
        return [], []
    texts = []
    for page in pages:
        if isinstance(page, dict) and page.get("text"):
            texts.append(str(page.get("text") or ""))
        tables = page.get("tables") if isinstance(page, dict) else None
        if isinstance(tables, list):
            for table in tables:
                if isinstance(table, dict) and table.get("html"):
                    texts.append(str(table.get("html")))
                elif isinstance(table, str) and "<table" in table.lower():
                    texts.append(table)
    blob = "\n".join(texts)
    if "<table" not in blob.lower():
        return [], []
    parsed = parse_html_tables(blob)
    headers = []
    cells = []
    for item in parsed:
        col = item.get("col_index")
        if col is None:
            continue
        try:
            c = int(col)
        except (TypeError, ValueError):
            continue
        x0, x1 = float(c * 10), float(c * 10 + 9)
        if item.get("is_header"):
            headers.append(
                {
                    "text": item.get("header") or item.get("value") or "",
                    "bbox": [x0, 0.0, x1, 10.0],
                    "column_index": c,
                }
            )
        else:
            cells.append(
                {
                    "text": item.get("value") or "",
                    "bbox": [x0, 20.0, x1, 30.0],
                    "row_index": item.get("row_index"),
                    "col_index": c,
                }
            )
    return headers, cells


def _table_html_from_ocr_bundle(bundle: Any) -> str:
    """Pull VL ``table_html`` from OCR result metadata / text (OCR step unchanged)."""
    from services.vl_html_table import extract_vl_table_html

    primary = getattr(bundle, "primary", None)
    if primary is None:
        return ""
    parts: list = []
    meta = getattr(primary, "metadata", None) or {}
    tables = meta.get("tables") if isinstance(meta, dict) else None
    items = tables.get("items") if isinstance(tables, dict) else None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("html", "table_html", "pred_html", "text"):
                raw = item.get(key)
                if isinstance(raw, str) and "<table" in raw.lower():
                    parts.append(raw.strip())
                    break
    if parts:
        return "\n\n".join(parts)
    return extract_vl_table_html(getattr(primary, "text", "") or "")


def _per_page_timeout_s() -> float:
    from services.ocr.timeout_policy import ocr_timeout_policy

    return ocr_timeout_policy().effective_page_timeout_s


def _primary_engine_name(ocr: Any) -> str:
    return str(
        getattr(getattr(ocr, "settings", None), "primary_engine", None) or "paddleocr-vl"
    )


def _timeout_bundle(ocr: Any, page_no: int, timeout_s: float, exc: Exception):
    from services.ocr.base import OCRResult
    from services.ocr.orchestrator import OCRBundle

    engine = _primary_engine_name(ocr)
    return OCRBundle(
        primary=OCRResult(
            text="",
            page=page_no,
            timed_out=True,
            timeout_s=timeout_s,
            engine=engine,
            metadata={"status": "timeout", "error": str(exc), "page": page_no},
        ),
        metadata={"status": "timeout", "error": str(exc), "page": page_no},
        engines_used=[engine],
    )


def _recognize_page_resilient(
    ocr: Any,
    image_bytes: bytes,
    *,
    page_no: int,
) -> tuple:
    """Preprocess + downscale + per-page timeout + limited retry + page cache."""
    import time

    from services.ocr.bounded import OcrExecutionTimeout, call_bounded
    from services.ocr.page_cache import get_cached_page, page_cache_key, put_cached_page
    from services.ocr.page_preprocess import prepare_for_ocr, preprocess_page_image
    from services.ocr.page_retry import (
        invert_image_bytes,
        low_confidence_cells,
        max_page_retries,
        page_retry_enabled,
        should_retry_page,
        zoom_table_region,
    )
    from services.ocr.timeout_policy import (
        describe_policy,
        kill_primary_vl_on_timeout,
        ocr_timeout_policy,
    )

    processed, prep_meta = preprocess_page_image(image_bytes, page=page_no)
    # Downscale for CPU OCR speed (~150 DPI) right before inference.
    processed = prepare_for_ocr(processed)
    policy = ocr_timeout_policy()
    timeout_s = policy.effective_page_timeout_s
    engine_name = _primary_engine_name(ocr)
    cache_key = page_cache_key(processed, engine=engine_name, page=page_no)
    page_meta: Dict[str, Any] = {
        "preprocess": prep_meta,
        "image_bytes": processed,
        "retries": [],
        "timeout_policy": describe_policy(policy),
        "cache_key": cache_key[:48],
        "cache_hit": False,
    }

    t0 = time.perf_counter()
    cached = get_cached_page(cache_key)
    if cached is not None:
        duration_ms = (time.perf_counter() - t0) * 1000.0
        text_len = len(str(getattr(getattr(cached, "primary", None), "text", "") or ""))
        page_meta["cache_hit"] = True
        page_meta["duration_ms"] = round(duration_ms, 1)
        page_meta["ocr_engine"] = str(
            getattr(getattr(cached, "primary", None), "engine", None) or engine_name
        )
        page_meta["timed_out"] = False
        page_meta["retry_count"] = 0
        page_meta["outcome"] = "success"
        page_meta["text_length"] = text_len
        page_meta["low_confidence_cells"] = low_confidence_cells(cached)
        logger.info(
            "ocr_page_done page=%s engine=%s duration_ms=%.1f cache_hit=1 timed_out=0 "
            "outcome=success recovery=none text_len=%s",
            page_no,
            page_meta["ocr_engine"],
            duration_ms,
            text_len,
        )
        return cached, page_meta

    timed_out = False
    timeout_retry_used = 0
    bundle = None
    while True:
        try:
            # Primary only — do not fold fallback into the same hard deadline.
            # Timeout/empty recovery runs Tess → Gemini transcription separately.
            bundle = call_bounded(
                lambda: ocr.recognize(processed, page=page_no, use_fallback=False),
                timeout_s=timeout_s,
                label=f"ocr_page_{page_no}",
            )
            timed_out = False
            break
        except OcrExecutionTimeout as exc:
            logger.warning(
                "ocr page=%s timed out after %ss (killing VL serve if running)",
                page_no,
                timeout_s,
            )
            timed_out = True
            killed = kill_primary_vl_on_timeout(label=f"ocr_page_{page_no}")
            page_meta["retries"].append(
                {
                    "reason": "timeout",
                    "error": str(exc),
                    "serve_killed": bool(killed),
                    "timeout_s": timeout_s,
                }
            )
            bundle = _timeout_bundle(ocr, page_no, timeout_s, exc)
            if (
                policy.retry_on_timeout
                and timeout_retry_used < policy.max_timeout_retries
            ):
                timeout_retry_used += 1
                page_meta["retries"].append(
                    {
                        "reason": "timeout_restart",
                        "attempt": timeout_retry_used,
                    }
                )
                logger.info(
                    "ocr page=%s timeout_restart attempt=%s/%s",
                    page_no,
                    timeout_retry_used,
                    policy.max_timeout_retries,
                )
                continue
            break

    retry_count = int(timeout_retry_used)
    retry_budget = max_page_retries()
    # Soft retries only when primary returned something weak (not hard timeout).
    if (
        page_retry_enabled()
        and not timed_out
        and should_retry_page(bundle)
        and retry_budget > 0
    ):
        for label, transform in (
            ("invert", invert_image_bytes),
            ("zoom2x", zoom_table_region),
        ):
            if retry_count - timeout_retry_used >= retry_budget:
                break
            try:
                alt = prepare_for_ocr(transform(processed))
                retry_bundle = call_bounded(
                    lambda img=alt: ocr.recognize(
                        img, page=page_no, use_fallback=False
                    ),
                    timeout_s=timeout_s,
                    label=f"ocr_page_{page_no}_{label}",
                )
                retry_count += 1
                page_meta["retries"].append({"reason": label, "ok": True})
                if not should_retry_page(retry_bundle):
                    bundle = retry_bundle
                    page_meta["image_bytes"] = alt
                    timed_out = False
                    break
            except OcrExecutionTimeout as exc:
                retry_count += 1
                kill_primary_vl_on_timeout(label=f"ocr_page_{page_no}_{label}")
                page_meta["retries"].append(
                    {"reason": label, "error": str(exc), "serve_killed": True}
                )
                logger.info("ocr page=%s retry=%s timed out: %s", page_no, label, exc)
            except Exception as exc:
                retry_count += 1
                page_meta["retries"].append({"reason": label, "error": str(exc)})
                logger.info("ocr page=%s retry=%s failed: %s", page_no, label, exc)

    # Structured recovery: Tesseract → Gemini transcription (not vision extract).
    primary = getattr(bundle, "primary", None)
    primary_text = str(getattr(primary, "text", "") or "").strip()
    need_recovery = bool(
        timed_out
        or not primary_text
        or str((getattr(primary, "metadata", None) or {}).get("status") or "")
        in {"timeout", "error", "ocr_error"}
    )
    recovery_engine = None
    if need_recovery:
        from services.ocr.recovery_chain import recover_unusable_ocr

        recovered, recovery_meta = recover_unusable_ocr(
            ocr,
            processed,
            page_no=page_no,
            failed_primary=primary,
            fail_reason="timeout" if timed_out else "empty_or_unusable",
        )
        page_meta["ocr_recovery"] = recovery_meta
        if recovery_meta.get("accepted_engine"):
            bundle = recovered
            recovery_engine = recovery_meta.get("accepted_engine")
            # Recovery produced text — clear hard-timeout flag for downstream
            # extract, but keep provenance on the page meta / result.
            timed_out = False
            page_meta["retries"].append(
                {
                    "reason": "ocr_recovery",
                    "engine": recovery_engine,
                    "ok": True,
                }
            )
        else:
            page_meta["retries"].append(
                {
                    "reason": "ocr_recovery",
                    "ok": False,
                    "log": recovery_meta.get("recovery_log"),
                }
            )
            logger.warning(
                "ocr_page_recovery_failed page=%s attempts=%s",
                page_no,
                recovery_meta.get("recovery_log"),
            )

    primary = getattr(bundle, "primary", None)
    if primary is not None:
        recovered_ok = bool((page_meta.get("ocr_recovery") or {}).get("accepted_engine"))
        primary.timed_out = bool(
            (timed_out or getattr(primary, "timed_out", False)) and not recovered_ok
        )
        primary.timeout_s = (
            timeout_s if primary.timed_out else getattr(primary, "timeout_s", None)
        )
        primary.retry_count = int(retry_count)
        meta = getattr(primary, "metadata", None)
        if isinstance(meta, dict):
            meta["timed_out"] = primary.timed_out
            meta["timeout_s"] = primary.timeout_s
            meta["retry_count"] = primary.retry_count
            if primary.timed_out:
                meta["status"] = "timeout"
            if recovered_ok:
                meta["status"] = meta.get("status") or "recovered"

    # Never cache empty/timeout/error as success (put_cached_page enforces).
    if bundle is not None and not timed_out:
        put_cached_page(cache_key, bundle)

    duration_ms = (time.perf_counter() - t0) * 1000.0
    final_engine = str(
        getattr(primary, "engine", None) or recovery_engine or engine_name
    )
    text_len = len(str(getattr(primary, "text", "") or ""))
    still_timed_out = timed_out and not bool(
        (page_meta.get("ocr_recovery") or {}).get("accepted_engine")
    )
    # Incomplete OCR must not be logged as success.
    outcome = "success"
    if still_timed_out:
        outcome = "timeout"
    elif need_recovery and not recovery_engine:
        outcome = "failed"
    elif recovery_engine:
        outcome = "recovered"
    elif text_len < 20:
        outcome = "incomplete"

    page_meta["low_confidence_cells"] = low_confidence_cells(bundle)
    page_meta["retry_count"] = retry_count
    page_meta["timed_out"] = still_timed_out
    page_meta["duration_ms"] = round(duration_ms, 1)
    page_meta["ocr_engine"] = final_engine
    page_meta["outcome"] = outcome
    page_meta["text_length"] = text_len
    logger.info(
        "ocr_page_done page=%s engine=%s duration_ms=%.1f cache_hit=0 timed_out=%s "
        "outcome=%s recovery=%s text_len=%s retries=%s",
        page_no,
        final_engine,
        duration_ms,
        int(still_timed_out),
        outcome,
        recovery_engine or "none",
        text_len,
        retry_count,
    )
    return bundle, page_meta


def _stamp_ocr_evidence_from_extraction(
    result: Dict[str, Any],
    ocr_evidence_for_validation: Optional[Dict[str, Any]],
    ocr_evidence_summary: Optional[Dict[str, Any]],
) -> None:
    """Mark OCR evidence pages as table-available after VL HTML extract."""
    from services.source_evidence import has_valid_source_evidence

    if not has_valid_source_evidence(extraction_result=result):
        return
    extra = ((result.get("totals") or {}).get("extra") or {})
    html = str(extra.get("ocr_table_html") or "")
    text_len = len(html.strip())
    for evidence in (ocr_evidence_for_validation, ocr_evidence_summary, result.get("ocr_evidence")):
        if not isinstance(evidence, dict):
            continue
        pages = evidence.get("pages")
        if not isinstance(pages, list):
            continue
        for page in pages:
            if not isinstance(page, dict):
                continue
            # Do not rewrite OCR page status to success. Extraction having
            # source evidence is separate from OCR completeness — empty or
            # timed-out OCR must stay visible for reliability metrics.
            if page.get("status") in {"timeout", "error"} or page.get("error"):
                page["extraction_has_source_evidence"] = True
            tables = page.get("tables")
            if not isinstance(tables, dict):
                tables = {}
                page["tables"] = tables
            tables["available"] = True
            tables["count"] = max(int(tables.get("count") or 0), 1)
            if text_len and int(page.get("text_length") or 0) < text_len:
                page["text_length"] = text_len
        evidence["ocr_source"] = extra.get("ocr_source") or evidence.get("ocr_source")


def _should_run_ocr(route: DocumentRoute) -> bool:
    if route.file_kind == "image":
        return True
    if route.file_kind == "pdf" and route.requires_ocr:
        return True
    return False


def _ocr_page_images(
    file_bytes: bytes,
    filename: str,
    route: DocumentRoute,
) -> list:
    """Rasterize OCR-required pages. Digital/native paths are not rasterized."""
    images = []
    if route.file_kind == "image":
        images.append((1, file_bytes))
        return images
    if route.file_kind != "pdf":
        return images
    try:
        import fitz
    except ImportError:
        return images
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception:
        return images
    try:
        max_pages = min(len(doc), 20)
        for index in range(max_pages):
            page = doc.load_page(index)
            # Mirror router embedded-text cutoff: skip native-text pages.
            text = (page.get_text("text") or "").strip()
            if len(text) >= 40 and not route.requires_ocr:
                continue
            if len(text) >= 40 and route.pdf_kind == "digital":
                continue
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
            images.append((index + 1, pix.tobytes("png")))
    finally:
        doc.close()
    return images


def _bundles_timed_out(page_bundles: list) -> bool:
    for bundle in page_bundles:
        meta = getattr(bundle, "metadata", None) or {}
        if "timeout" in str(meta.get("error") or "").casefold():
            return True
        for result in (
            getattr(bundle, "primary", None),
            getattr(bundle, "secondary", None),
        ):
            if result is None:
                continue
            error = str((getattr(result, "metadata", None) or {}).get("error") or "")
            if "timeout" in error.casefold():
                return True
    return False


def provider_error_from_ocr(
    ocr_bundle: Optional[Dict[str, Any]],
    *,
    ocr_text: str = "",
) -> Optional[str]:
    """A required OCR call that timed out with no text is a provider error.

    Fallback text is not a timeout success. Those quantities stay unverified
    unless source columns or OCR text exist.
    """
    if not isinstance(ocr_bundle, dict) or not ocr_bundle.get("invoked_here"):
        return None
    if ocr_bundle.get("unverified_fallback"):
        return None
    if str(ocr_text or "").strip():
        return None
    error = str(ocr_bundle.get("error") or "")
    status = str(ocr_bundle.get("status") or "")
    if ocr_bundle.get("timed_out") or "timeout" in error.casefold() or "timeout" in status.casefold():
        return "ocr_timeout"
    return None


def _provider_error(result: Any) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    phase2 = result.get("phase2") or {}
    if isinstance(phase2, dict):
        status = str(phase2.get("status") or phase2.get("error") or "")
        if "PROVIDER" in status.upper():
            return status or "provider_error"
        if phase2.get("provider_error"):
            return str(phase2.get("provider_error"))
    err = result.get("error") or result.get("provider_error")
    if err and "provider" in str(err).lower():
        return str(err)
    return None


def _extraction_error(result: Any) -> Optional[str]:
    if not isinstance(result, dict):
        return "non_dict_result"
    if result.get("extraction_failed"):
        return str(result.get("extraction_failed"))
    status = str(result.get("status") or "")
    if status.upper() in {"EXTRACTION_ERROR", "EXTRACT_FAILED"}:
        return status
    return None
