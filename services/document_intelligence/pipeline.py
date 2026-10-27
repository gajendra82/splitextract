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
from services.ocr.orchestrator import OCROrchestrator, ocr_settings

logger = logging.getLogger(__name__)


class DocumentIntelligencePipeline:
    """Central orchestration. Does not replace named parsers."""

    def __init__(
        self,
        *,
        registry: Optional[ParserRegistry] = None,
        ocr: Optional[OCROrchestrator] = None,
    ) -> None:
        self.registry = registry or default_registry()
        self.ocr = ocr or OCROrchestrator(settings=ocr_settings())

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
        if _should_run_ocr(route):
            from services.ocr.runtime import document_evidence_from_bundle

            page_images = _ocr_page_images(file_bytes, name, route)
            page_bundles = []
            for page_no, image_bytes in page_images:
                bundle = self.ocr.recognize(image_bytes, page=page_no)
                # Optional evidence-gated secondary OCR (OCR_SECONDARY_REPAIR;
                # default OFF). Never invents quantities from stock equations.
                page_cells = _maybe_secondary_repair_bundle(
                    bundle,
                    document_id=str(getattr(evidence, "document_id", None) or name),
                    image_bytes=image_bytes,
                    file_bytes=file_bytes,
                    file_kind=getattr(route, "file_kind", None),
                    page_index=max(0, int(page_no) - 1),
                )
                if page_cells:
                    repaired_cells_all.extend(page_cells)
                if getattr(bundle, "primary", None) and bundle.primary.text:
                    repaired_ocr_texts.append(bundle.primary.text)
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
                    "evidence": ocr_evidence_summary,
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
        # When secondary repair is enabled and DI already produced OCR text,
        # reuse it so core parsers see repaired HTML instead of re-OCRing.
        from services.ocr.secondary_repair import (
            overlay_secondary_repairs_onto_result,
            secondary_repair_enabled,
        )
        from services.ocr.ocr_text_override import ocr_text_override

        combined_ocr = "\n\n".join(t for t in repaired_ocr_texts if t)
        if secondary_repair_enabled() and combined_ocr:
            with ocr_text_override(combined_ocr):
                result = sse.extract_sales_statement_core(file_bytes, name)
            if isinstance(result, dict) and repaired_cells_all:
                result = overlay_secondary_repairs_onto_result(
                    result, repaired_cells_all
                )
        else:
            result = sse.extract_sales_statement_core(file_bytes, name)
        timer.mark("extraction")

        if isinstance(result, dict):
            from services.source_grounding import apply_source_grounding

            try:
                apply_source_grounding(result, file_bytes, name)
            except Exception:
                logger.warning("source grounding skipped for %s", name, exc_info=True)

        if isinstance(result, dict) and ocr_evidence_summary is not None:
            # Keep API payload small; validators receive full evidence below.
            result["ocr_evidence"] = ocr_evidence_summary

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
        stack = build_validation_stack(result, phase1_validation=validation)
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
                    result, phase1_validation=validation
                )

        provider_error = _provider_error(result)
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
