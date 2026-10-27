"""Document layout understanding. Structural signals, not business extraction."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from services.document_router import DocumentRoute
from services.evidence import DocumentEvidence
from services.layout.column_detector import detect_columns
from services.layout.row_detector import detect_rows
from services.layout.table_detector import detect_tables


@dataclass
class LayoutUnderstanding:
    layout_id: str
    strategy: str
    headers: List[str] = field(default_factory=list)
    columns: List[Dict[str, Any]] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    tables: List[Dict[str, Any]] = field(default_factory=list)
    sections: List[str] = field(default_factory=list)
    confidence: float = 0.0
    known_layout: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def understand_layout(
    route: DocumentRoute,
    evidence: DocumentEvidence,
    *,
    known_layout_hint: Optional[str] = None,
) -> LayoutUnderstanding:
    """Derive structural signals from route + evidence.

    Known named parsers remain preferred; this is the structural fallback
    for unknown layouts and observability for known ones.
    """
    tables = detect_tables(evidence)
    columns = detect_columns(evidence, tables)
    rows = detect_rows(evidence, tables)
    headers = [str(col.get("header") or "") for col in columns if col.get("header")]
    if known_layout_hint:
        return LayoutUnderstanding(
            layout_id=known_layout_hint,
            strategy="known_layout",
            headers=headers,
            columns=columns,
            rows=rows,
            tables=tables,
            sections=["known"],
            confidence=0.9,
            known_layout=True,
            metadata={
                "file_kind": route.file_kind,
                "pdf_kind": route.pdf_kind,
                "requires_ocr": route.requires_ocr,
            },
        )

    if route.file_kind in {"xlsx", "xls"}:
        strategy = "spreadsheet_cells"
        confidence = 0.75
        layout_id = "spreadsheet"
    elif route.file_kind == "pdf" and not route.requires_ocr:
        strategy = "digital_pdf_native"
        confidence = 0.7 if evidence.pages else 0.4
        layout_id = "digital_pdf"
    elif route.file_kind == "image" or route.requires_ocr:
        strategy = "visual_bbox"
        confidence = 0.45
        layout_id = "visual"
    else:
        strategy = "generic_text"
        confidence = 0.35
        layout_id = "generic"

    return LayoutUnderstanding(
        layout_id=layout_id,
        strategy=strategy,
        headers=headers,
        columns=columns,
        rows=rows,
        tables=tables,
        sections=["body"] if rows or tables else [],
        confidence=confidence,
        known_layout=False,
        metadata={
            "file_kind": route.file_kind,
            "pdf_kind": route.pdf_kind,
            "requires_ocr": route.requires_ocr,
            "page_count": len(route.page_stats)
            or evidence.metadata.get("page_count")
            or len(evidence.pages),
            "token_pages": sum(1 for page in evidence.pages if page.tokens),
        },
    )
