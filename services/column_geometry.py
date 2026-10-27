"""Geometry-aware column mapping. Plausibility alone never assigns a quantity.

Uses header spatial positions, cell alignment, and layout-scoped ColumnMapping
profiles. Ambiguous mappings stay unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from services.column_mapping import (
    ColumnMapping,
    header_key,
    header_map_for_evidence,
    mappings_for_profile,
)
from services.focused_row_request import CANONICAL_FIELDS
from services.stock_identity_profiles import select_profile

CRITICAL_QTY_FIELDS = frozenset(
    {
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "sales_scheme_qty",
        "stock_out_qty",
        "closing_qty",
    }
)


@dataclass(frozen=True)
class HeaderSpan:
    text: str
    x0: float
    x1: float
    y0: float = 0.0
    y1: float = 0.0
    column_index: Optional[int] = None

    @property
    def mid_x(self) -> float:
        return (self.x0 + self.x1) / 2.0


@dataclass(frozen=True)
class CellSpan:
    text: str
    x0: float
    x1: float
    y0: float = 0.0
    y1: float = 0.0
    row_index: Optional[int] = None
    col_index: Optional[int] = None

    @property
    def mid_x(self) -> float:
        return (self.x0 + self.x1) / 2.0


@dataclass
class ColumnAssignment:
    cell_text: str
    canonical_field: Optional[str]
    printed_header: Optional[str]
    status: str  # mapped | unresolved | ambiguous | unmapped_preserved
    reason: str
    overlap_ratio: Optional[float] = None
    cell_bbox: Optional[Tuple[float, float, float, float]] = None
    header_bbox: Optional[Tuple[float, float, float, float]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cell_text": self.cell_text,
            "canonical_field": self.canonical_field,
            "printed_header": self.printed_header,
            "status": self.status,
            "reason": self.reason,
            "overlap_ratio": self.overlap_ratio,
            "cell_bbox": list(self.cell_bbox) if self.cell_bbox else None,
            "header_bbox": list(self.header_bbox) if self.header_bbox else None,
        }


@dataclass
class ColumnMappingReport:
    assignments: List[ColumnAssignment] = field(default_factory=list)
    unresolved_headers: List[str] = field(default_factory=list)
    ambiguous_fields: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    layout_id: str = ""
    mapping_confidence: float = 0.0

    @property
    def has_column_confusion(self) -> bool:
        return bool(
            self.ambiguous_fields
            or any(
                a.status in {"unresolved", "ambiguous"}
                and a.canonical_field in CRITICAL_QTY_FIELDS
                for a in self.assignments
            )
            or "column_shift" in self.issues
            or "overlapping_cells" in self.issues
            or "inconsistent_column_positions" in self.issues
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assignments": [a.to_dict() for a in self.assignments],
            "unresolved_headers": list(self.unresolved_headers),
            "ambiguous_fields": list(self.ambiguous_fields),
            "issues": list(self.issues),
            "layout_id": self.layout_id,
            "mapping_confidence": self.mapping_confidence,
            "has_column_confusion": self.has_column_confusion,
        }


def detect_column_issues(
    headers: Sequence[HeaderSpan],
    cells: Sequence[CellSpan],
) -> List[str]:
    """Detect shifted / overlapping / inconsistent geometry without inventing maps."""
    issues: List[str] = []
    if not headers:
        issues.append("missing_headers")
        return issues

    # Header order should be left-to-right by mid_x.
    ordered = sorted(headers, key=lambda h: h.mid_x)
    for left, right in zip(ordered, ordered[1:]):
        if right.x0 < left.x0 - 1e-6 and right.mid_x < left.mid_x:
            issues.append("inconsistent_column_positions")
            break
        overlap = min(left.x1, right.x1) - max(left.x0, right.x0)
        width = max(1e-6, min(left.x1 - left.x0, right.x1 - right.x0))
        if overlap / width > 0.45:
            issues.append("overlapping_headers")
            break

    # Cells that heavily overlap two header bands are ambiguous.
    for cell in cells:
        hits = [
            h
            for h in headers
            if _x_overlap_ratio(cell.x0, cell.x1, h.x0, h.x1) >= 0.25
        ]
        if len(hits) >= 2:
            issues.append("overlapping_cells")
            break

    # Row shape: unexpected column count jumps across adjacent cells in a row.
    by_row: Dict[int, List[CellSpan]] = {}
    for cell in cells:
        if cell.row_index is None:
            continue
        by_row.setdefault(int(cell.row_index), []).append(cell)
    counts = [len(v) for v in by_row.values()]
    if counts and max(counts) - min(counts) >= 2:
        issues.append("unexpected_row_shapes")

    if headers and cells:
        # Shift heuristic: many cells sit left of the first header or right of last.
        first = min(h.x0 for h in headers)
        last = max(h.x1 for h in headers)
        outside = sum(
            1 for c in cells if c.x1 < first - 2 or c.x0 > last + 2
        )
        if cells and outside / len(cells) > 0.35:
            issues.append("column_shift")
    return _unique(issues)


def map_cells_to_headers(
    headers: Sequence[HeaderSpan],
    cells: Sequence[CellSpan],
    mappings: Sequence[ColumnMapping],
    *,
    min_overlap: float = 0.35,
) -> ColumnMappingReport:
    """Map cells using spatial overlap + layout-scoped aliases. No numeric guessing."""
    issues = detect_column_issues(headers, cells)
    header_texts = [h.text for h in headers]
    alias_map = header_map_for_evidence(mappings, header_texts)
    unresolved_headers = [
        h.text
        for h in headers
        if header_key(h.text) not in {header_key(k) for k in alias_map}
    ]

    field_to_headers: Dict[str, List[HeaderSpan]] = {}
    for header in headers:
        field = alias_map.get(header.text)
        if not field:
            # Try alias match against mapping objects directly.
            for mapping in mappings:
                names = (mapping.printed_header,) + tuple(mapping.aliases or ())
                if any(header_key(n) == header_key(header.text) for n in names):
                    field = mapping.canonical_field
                    break
        if field:
            field_to_headers.setdefault(field, []).append(header)

    ambiguous_fields = [
        field for field, spans in field_to_headers.items() if len(spans) > 1
    ]

    assignments: List[ColumnAssignment] = []
    for cell in cells:
        candidates: List[Tuple[HeaderSpan, float, str]] = []
        for header in headers:
            field = None
            for mapping in mappings:
                names = (mapping.printed_header,) + tuple(mapping.aliases or ())
                if any(header_key(n) == header_key(header.text) for n in names):
                    field = mapping.canonical_field
                    break
            if field is None:
                field = alias_map.get(header.text)
            if field is None:
                continue
            overlap = _x_overlap_ratio(cell.x0, cell.x1, header.x0, header.x1)
            if overlap >= min_overlap:
                candidates.append((header, overlap, field))

        if not candidates:
            assignments.append(
                ColumnAssignment(
                    cell_text=cell.text,
                    canonical_field=None,
                    printed_header=None,
                    status="unmapped_preserved",
                    reason="no_spatial_header_hit",
                    cell_bbox=(cell.x0, cell.y0, cell.x1, cell.y1),
                )
            )
            continue

        candidates.sort(key=lambda item: item[1], reverse=True)
        best_header, best_overlap, best_field = candidates[0]
        near = [
            c for c in candidates if abs(c[1] - best_overlap) <= 0.08 and c[2] != best_field
        ]
        if best_field in ambiguous_fields or near:
            assignments.append(
                ColumnAssignment(
                    cell_text=cell.text,
                    canonical_field=None,
                    printed_header=best_header.text,
                    status="ambiguous",
                    reason="ambiguous_header_alignment",
                    overlap_ratio=best_overlap,
                    cell_bbox=(cell.x0, cell.y0, cell.x1, cell.y1),
                    header_bbox=(
                        best_header.x0,
                        best_header.y0,
                        best_header.x1,
                        best_header.y1,
                    ),
                )
            )
            continue

        assignments.append(
            ColumnAssignment(
                cell_text=cell.text,
                canonical_field=best_field,
                printed_header=best_header.text,
                status="mapped",
                reason="spatial_header_alignment",
                overlap_ratio=best_overlap,
                cell_bbox=(cell.x0, cell.y0, cell.x1, cell.y1),
                header_bbox=(
                    best_header.x0,
                    best_header.y0,
                    best_header.x1,
                    best_header.y1,
                ),
            )
        )

    mapped_critical = sum(
        1
        for a in assignments
        if a.status == "mapped" and a.canonical_field in CRITICAL_QTY_FIELDS
    )
    critical_needed = len(CRITICAL_QTY_FIELDS & {m.canonical_field for m in mappings})
    confidence = 0.0
    if critical_needed:
        confidence = mapped_critical / float(critical_needed)
    elif assignments:
        confidence = sum(1 for a in assignments if a.status == "mapped") / len(
            assignments
        )

    layout_id = ""
    if mappings:
        layout_id = str(mappings[0].layout_id or "")

    return ColumnMappingReport(
        assignments=assignments,
        unresolved_headers=unresolved_headers,
        ambiguous_fields=ambiguous_fields,
        issues=issues,
        layout_id=layout_id,
        mapping_confidence=round(confidence, 4),
    )


def column_report_for_result(
    result: Dict[str, Any],
    *,
    headers: Optional[Sequence[Any]] = None,
    cells: Optional[Sequence[Any]] = None,
) -> ColumnMappingReport:
    """Build a report from parser result + optional geometry spans."""
    profile = select_profile(result)
    mappings = mappings_for_profile(result, profile)
    header_spans = _coerce_headers(headers)
    cell_spans = _coerce_cells(cells)
    if not header_spans and not cell_spans:
        # Alias-only check: duplicate canonical ownership without geometry.
        alias = header_map_for_evidence(
            mappings,
            _headers_from_result(result),
        )
        unresolved = [
            h
            for h in _headers_from_result(result)
            if header_key(h) not in {header_key(k) for k in alias}
        ]
        issues = []
        if not mappings and profile.profile_id in {"unknown", "not_scored"}:
            issues.append("unknown_layout_no_mapping")
        return ColumnMappingReport(
            unresolved_headers=unresolved,
            issues=issues,
            layout_id=str(getattr(profile, "profile_id", "") or ""),
            mapping_confidence=1.0 if alias and not unresolved else 0.0,
        )
    return map_cells_to_headers(header_spans, cell_spans, mappings)


def reject_plausibility_only_assignment(
    *,
    canonical_field: str,
    source: str,
) -> bool:
    """True when a critical qty would be assigned without spatial/header evidence."""
    if canonical_field not in CRITICAL_QTY_FIELDS:
        return False
    return source in {
        "plausibility",
        "numeric_guess",
        "stock_equation",
        "adjacent_row",
        "inferred",
    }


def _x_overlap_ratio(a0: float, a1: float, b0: float, b1: float) -> float:
    overlap = min(a1, b1) - max(a0, b0)
    if overlap <= 0:
        return 0.0
    width = max(1e-6, min(a1 - a0, b1 - b0))
    return overlap / width


def _coerce_headers(headers: Optional[Sequence[Any]]) -> List[HeaderSpan]:
    out: List[HeaderSpan] = []
    for index, item in enumerate(headers or []):
        if isinstance(item, HeaderSpan):
            out.append(item)
            continue
        if isinstance(item, dict):
            bbox = item.get("bbox") or item.get("box")
            if not bbox or len(bbox) < 4:
                continue
            out.append(
                HeaderSpan(
                    text=str(item.get("text") or item.get("header") or ""),
                    x0=float(bbox[0]),
                    y0=float(bbox[1]),
                    x1=float(bbox[2]),
                    y1=float(bbox[3]),
                    column_index=item.get("column_index", index),
                )
            )
    return out


def _coerce_cells(cells: Optional[Sequence[Any]]) -> List[CellSpan]:
    out: List[CellSpan] = []
    for index, item in enumerate(cells or []):
        if isinstance(item, CellSpan):
            out.append(item)
            continue
        if isinstance(item, dict):
            bbox = item.get("bbox") or item.get("box")
            if not bbox or len(bbox) < 4:
                continue
            out.append(
                CellSpan(
                    text=str(item.get("text") or item.get("value") or ""),
                    x0=float(bbox[0]),
                    y0=float(bbox[1]),
                    x1=float(bbox[2]),
                    y1=float(bbox[3]),
                    row_index=item.get("row_index"),
                    col_index=item.get("col_index", index),
                )
            )
    return out


def _headers_from_result(result: Dict[str, Any]) -> List[str]:
    extra = {}
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    if isinstance(totals.get("extra"), dict):
        extra = totals["extra"]
    raw = extra.get("headers") or extra.get("column_headers") or []
    if isinstance(raw, list):
        return [str(h) for h in raw if str(h).strip()]
    mapping = extra.get("column_mapping")
    if isinstance(mapping, dict):
        headers = []
        for key, value in mapping.items():
            if isinstance(value, dict) and value.get("header"):
                headers.append(str(value["header"]))
            elif isinstance(value, str) and key in CANONICAL_FIELDS:
                headers.append(value)
        return headers
    return []


def _unique(values: Iterable[str]) -> List[str]:
    seen: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen
