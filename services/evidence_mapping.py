"""Align evidence headers with a layout's trusted column mappings.

Exact header text, an alias that layout already stored, or a column index
that layout already stored can promote a canonical field. String similarity
cannot. This module does not calculate quantities or choose an identity profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from services.column_mapping import ColumnMapping, header_key

SOURCE_EXACT = "exact_normalized"
SOURCE_ALIAS = "explicit_alias"
SOURCE_INDEX = "source_column_index"
NO_SAFE_MATCH = "NO_SAFE_MATCH"

_CLOSED_PROFILES = {"unknown", "not_scored"}


@dataclass(frozen=True)
class EvidenceColumnMatch:
    evidence_header: str
    canonical_field: str
    trusted_header: str
    source: str
    confidence: float
    column_index: Optional[int] = None


@dataclass(frozen=True)
class EvidenceMappingResult:
    matched: Dict[str, str]
    matches: Tuple[EvidenceColumnMatch, ...]
    unmatched_headers: Tuple[str, ...]
    alignment_source: str
    confidence: float
    ambiguous: Tuple[str, ...]
    rejection_reasons: Tuple[str, ...]
    promotion_allowed: bool


def _empty(headers: Sequence[str], *reasons: str) -> EvidenceMappingResult:
    unmatched = tuple(str(header).strip() for header in headers if str(header or "").strip())
    return EvidenceMappingResult(
        matched={},
        matches=(),
        unmatched_headers=unmatched,
        alignment_source=NO_SAFE_MATCH,
        confidence=0.0,
        ambiguous=(),
        rejection_reasons=tuple(reasons) or (NO_SAFE_MATCH,),
        promotion_allowed=False,
    )


def _profile_closed(profile: Any) -> bool:
    if profile is None:
        return True
    if not getattr(profile, "supports_recovery", False):
        return True
    return str(getattr(profile, "profile_id", "") or "") in _CLOSED_PROFILES


def _text_match(header: str, item: ColumnMapping) -> Optional[str]:
    wanted = header_key(header)
    if not wanted:
        return None
    if header_key(item.printed_header) == wanted:
        return SOURCE_EXACT
    if any(header_key(alias) == wanted for alias in item.aliases or ()):
        return SOURCE_ALIAS
    return None


def _geometry_index(entry: Any) -> Optional[int]:
    if not isinstance(entry, dict):
        return None
    raw = entry.get("column_index")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _geometry_header(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("header") or entry.get("text") or "").strip()


def align_evidence_columns(
    profile: Any,
    trusted_mappings: Sequence[ColumnMapping],
    evidence_headers: Sequence[Any],
    evidence_geometry: Sequence[Any] = None,
    evidence_metadata: Any = None,
) -> EvidenceMappingResult:
    """Return the safe mapping subset present on this evidence.

    ``evidence_metadata`` is ignored. Image width and filename are not inputs.
    ``evidence_geometry`` may supply a layout column index the profile already
    stored. An OCR token offset is not a column index, and this function does
    not invent one from header order.
    """
    del evidence_metadata
    headers = [str(header).strip() for header in evidence_headers or [] if str(header or "").strip()]
    if _profile_closed(profile):
        return _empty(headers, "profile_not_recoverable")
    mappings = tuple(item for item in trusted_mappings or () if isinstance(item, ColumnMapping))
    if not mappings:
        return _empty(headers, "no_trusted_mappings")

    candidates = []
    claimed_headers = set()
    ambiguous = []
    for header in headers:
        hits = []
        for item in mappings:
            source = _text_match(header, item)
            if source is None:
                continue
            hits.append((header, item, source, 1.0))
        fields = {item.canonical_field for _header, item, _source, _confidence in hits}
        if len(fields) > 1:
            ambiguous.extend(sorted(fields))
            continue
        if len(fields) != 1:
            continue
        candidates.append(hits[0])
        claimed_headers.add(header_key(header))

    index_owners: Dict[int, list] = {}
    for item in mappings:
        if item.column_index is None:
            continue
        index_owners.setdefault(item.column_index, []).append(item)

    for entry in evidence_geometry or ():
        header = _geometry_header(entry)
        index = _geometry_index(entry)
        if not header or index is None or header not in headers:
            continue
        if header_key(header) in claimed_headers:
            continue
        owners = index_owners.get(index) or []
        fields = {item.canonical_field for item in owners}
        if len(owners) != 1 or len(fields) != 1:
            continue
        candidates.append((header, owners[0], SOURCE_INDEX, 0.9))
        claimed_headers.add(header_key(header))

    by_field: Dict[str, list] = {}
    by_header: Dict[str, list] = {}
    for header, item, source, confidence in candidates:
        by_field.setdefault(item.canonical_field, []).append((header, item, source, confidence))
        by_header.setdefault(header_key(header), []).append((header, item, source, confidence))

    blocked_fields = set()
    for field, found in by_field.items():
        keys = {header_key(header) for header, _item, _source, _confidence in found}
        if len(keys) > 1:
            blocked_fields.add(field)
            ambiguous.append(field)
    blocked_headers = set()
    for key, found in by_header.items():
        fields = {item.canonical_field for _header, item, _source, _confidence in found}
        if len(fields) > 1:
            blocked_headers.add(key)
            ambiguous.extend(sorted(fields))

    matches = []
    matched: Dict[str, str] = {}
    for header in headers:
        found = [
            item
            for item in candidates
            if header_key(item[0]) == header_key(header)
            and item[1].canonical_field not in blocked_fields
            and header_key(header) not in blocked_headers
        ]
        if len({item[1].canonical_field for item in found}) != 1:
            continue
        _header, mapping, source, confidence = found[0]
        if header in matched:
            continue
        matches.append(
            EvidenceColumnMatch(
                evidence_header=header,
                canonical_field=mapping.canonical_field,
                trusted_header=mapping.printed_header,
                source=source,
                confidence=confidence,
                column_index=mapping.column_index,
            )
        )
        matched[header] = mapping.canonical_field

    unmatched = tuple(header for header in headers if header not in matched)
    reasons = []
    if ambiguous:
        reasons.append("ambiguous_mapping")
    if unmatched:
        reasons.append(NO_SAFE_MATCH)
    sources = tuple(dict.fromkeys(match.source for match in matches))
    if not matches:
        alignment_source = NO_SAFE_MATCH
        confidence = 0.0
    elif len(sources) == 1:
        alignment_source = sources[0]
        confidence = min(match.confidence for match in matches)
    else:
        alignment_source = "mixed"
        confidence = min(match.confidence for match in matches)
    return EvidenceMappingResult(
        matched=matched,
        matches=tuple(matches),
        unmatched_headers=unmatched,
        alignment_source=alignment_source,
        confidence=confidence,
        ambiguous=tuple(dict.fromkeys(ambiguous)),
        rejection_reasons=tuple(reasons),
        promotion_allowed=bool(matched),
    )
