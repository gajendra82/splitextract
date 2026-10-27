"""Source-ground-truth evaluation utilities for OCR evidence.

Only SOURCE_VERIFIED fields may be used as ground truth.
"""

from services.ground_truth.classify import classify_reference_source
from services.ground_truth.matching import match_field, normalize_numeric, normalize_text
from services.ground_truth.metrics import aggregate_metrics, compute_iou
from services.ground_truth.schema import (
    GroundTruthDocument,
    GroundTruthField,
    validate_ground_truth_document,
)

__all__ = [
    "classify_reference_source",
    "match_field",
    "normalize_numeric",
    "normalize_text",
    "aggregate_metrics",
    "compute_iou",
    "GroundTruthDocument",
    "GroundTruthField",
    "validate_ground_truth_document",
]
