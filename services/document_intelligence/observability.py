"""Extraction observability record for debugging and evaluation."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional


class PipelineTimer:
    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.marks: Dict[str, float] = {}

    def mark(self, name: str) -> None:
        self.marks[name] = round(time.perf_counter() - self.started, 4)

    def timings(self) -> Dict[str, float]:
        out = dict(self.marks)
        out["total"] = round(time.perf_counter() - self.started, 4)
        return out


def build_observability(
    *,
    document_id: str,
    document_type: str,
    router_decision: Dict[str, Any],
    evidence_sources: list,
    ocr_engines_used: list,
    ocr_disagreement: bool,
    layout_strategy: str,
    extraction_strategy: Dict[str, Any],
    parser_used: Optional[str],
    mapping_decisions: Optional[Dict[str, Any]],
    validation_results: Dict[str, Any],
    recovery_attempts: Optional[Dict[str, Any]],
    final_decision: Dict[str, Any],
    failure_taxonomy: Any,
    timings: Dict[str, float],
) -> Dict[str, Any]:
    return {
        "document_id": document_id,
        "document_type": document_type,
        "router_decision": router_decision,
        "evidence_sources": evidence_sources,
        "ocr_engines_used": list(ocr_engines_used or []),
        "ocr_disagreement": bool(ocr_disagreement),
        "layout_strategy": layout_strategy,
        "extraction_strategy": extraction_strategy,
        "parser_used": parser_used,
        "mapping_decisions": mapping_decisions or {},
        "validation_results": validation_results,
        "recovery_attempts": recovery_attempts or {},
        "final_decision": final_decision,
        "failure_taxonomy": failure_taxonomy,
        "timings": timings,
    }
