"""Ground-truth schema for source-verified OCR evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

FIELD_TYPES = {
    "text",
    "numeric",
    "date",
    "batch",
    "header",
    "table_cell",
    "quantity",
    "price",
    "percentage",
    "hsn_gst",
    "other",
}

HIGH_PRIORITY_TYPES = {
    "numeric",
    "quantity",
    "price",
    "date",
    "batch",
    "percentage",
    "hsn_gst",
    "header",
    "table_cell",
}


@dataclass
class GroundTruthField:
    field_id: str
    field_type: str
    printed_header: Optional[str]
    expected_raw: str
    expected_normalized: Optional[Any] = None
    page: int = 1
    product_key: Optional[str] = None
    bbox: Optional[List[float]] = None
    bbox_verified: bool = False
    verified: bool = True
    source_verified: bool = True
    priority: str = "high"
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GroundTruthDocument:
    document_id: str
    page: int
    source_file: str
    source_type: str
    source_verified: bool
    fields: List[GroundTruthField] = field(default_factory=list)
    verification_method: str = "source_visual_inspection"
    provenance: str = "SOURCE_VERIFIED"
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "page": self.page,
            "source": {
                "file": self.source_file,
                "source_type": self.source_type,
                "source_verified": self.source_verified,
            },
            "verification_method": self.verification_method,
            "provenance": self.provenance,
            "fields": [f.to_dict() for f in self.fields],
            "notes": list(self.notes),
        }


def validate_ground_truth_document(payload: Dict[str, Any]) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return False, ["payload_not_object"]
    if not payload.get("document_id"):
        errors.append("missing_document_id")
    source = payload.get("source") or {}
    if not isinstance(source, dict):
        errors.append("source_not_object")
    else:
        if source.get("source_verified") is not True and payload.get("provenance") != "SOURCE_VERIFIED":
            # Allow unverified drafts, but mark them.
            pass
        if not source.get("file") and not payload.get("source_file"):
            errors.append("missing_source_file")
    fields = payload.get("fields")
    if not isinstance(fields, list):
        errors.append("fields_not_list")
        return (len(errors) == 0), errors
    for index, field_obj in enumerate(fields):
        if not isinstance(field_obj, dict):
            errors.append(f"field_{index}_not_object")
            continue
        if not field_obj.get("field_id"):
            errors.append(f"field_{index}_missing_field_id")
        ftype = field_obj.get("field_type") or "other"
        if ftype not in FIELD_TYPES:
            errors.append(f"field_{index}_bad_type:{ftype}")
        if field_obj.get("expected_raw") is None:
            errors.append(f"field_{index}_missing_expected_raw")
        if field_obj.get("source_verified") is False and field_obj.get("verified") is True:
            errors.append(f"field_{index}_verified_without_source_flag")
        if field_obj.get("bbox") is not None:
            bbox = field_obj.get("bbox")
            if not (
                isinstance(bbox, (list, tuple))
                and len(bbox) == 4
                and all(isinstance(v, (int, float)) for v in bbox)
            ):
                errors.append(f"field_{index}_bad_bbox")
            elif not field_obj.get("bbox_verified", False):
                # Invented boxes are forbidden; unverified bbox must be null.
                errors.append(f"field_{index}_bbox_without_bbox_verified")
    return (len(errors) == 0), errors


def only_source_verified_fields(
    payload: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Reject model/parser-derived fields even if present in a file."""
    out: List[Dict[str, Any]] = []
    source = payload.get("source") or {}
    doc_ok = bool(source.get("source_verified")) or payload.get("provenance") == "SOURCE_VERIFIED"
    for field_obj in payload.get("fields") or []:
        if not isinstance(field_obj, dict):
            continue
        if field_obj.get("source_verified") is False:
            continue
        if field_obj.get("verified") is False:
            continue
        if field_obj.get("provenance") in {"MODEL_DERIVED", "PARSER_DERIVED"}:
            continue
        if not doc_ok and field_obj.get("source_verified") is not True:
            continue
        out.append(field_obj)
    return out


def field(
    field_id: str,
    field_type: str,
    printed_header: str,
    expected_raw: str,
    *,
    page: int = 1,
    product_key: Optional[str] = None,
    expected_normalized: Any = None,
    priority: str = "high",
    notes: Optional[str] = None,
) -> GroundTruthField:
    return GroundTruthField(
        field_id=field_id,
        field_type=field_type,
        printed_header=printed_header,
        expected_raw=str(expected_raw),
        expected_normalized=expected_normalized,
        page=page,
        product_key=product_key,
        bbox=None,
        bbox_verified=False,
        verified=True,
        source_verified=True,
        priority=priority,
        notes=notes,
    )
