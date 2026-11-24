"""Recovery verification against trusted expected results only.

The parser output, Gemini output, and an accepted Phase 2 result are not
ground truth. Low-confidence or empty independent reads stay unverified.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple


def is_trusted_ground_truth(ground: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(ground, dict):
        return False
    if ground.get("confidence") != "high":
        return False
    if ground.get("partial"):
        return False
    if not ground.get("line_items"):
        return False
    if ground.get("format") in {None, "", "Unknown layout"}:
        return False
    return True


def load_stored_ground_truth(
    filename: str,
    directories: Sequence[Path],
) -> Optional[Dict[str, Any]]:
    stem = Path(filename).stem
    for directory in directories:
        candidate = Path(directory) / f"{stem}.json"
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if is_trusted_ground_truth(payload):
            return payload
    return None


def build_trusted_pdf_ground_truth(path: Path, filename: str) -> Optional[Dict[str, Any]]:
    """Use the existing independent PDF reader. Untrusted reads are dropped."""
    if path.suffix.lower() != ".pdf":
        return None
    from qa.ground_truth import build_ground_truth

    ground = build_ground_truth(str(path), filename)
    if is_trusted_ground_truth(ground):
        return ground
    return None


def verify_against_ground_truth(
    ground: Optional[Dict[str, Any]],
    extracted: Optional[Dict[str, Any]],
) -> str:
    """Return ``correct``, ``wrong``, or ``UNVERIFIED``."""
    if not is_trusted_ground_truth(ground) or not isinstance(extracted, dict):
        return "UNVERIFIED"
    from qa.compare import compare_statements

    compared = compare_statements(ground, extracted)
    status = str(compared.get("status") or "")
    if status == "PASS":
        return "correct"
    if status == "FAIL":
        return "wrong"
    return "UNVERIFIED"


def resolve_ground_truth(
    path: Path,
    *,
    stored_directories: Sequence[Path] = (),
    allow_pdf_build: bool = True,
    requires_ocr: bool = False,
) -> Tuple[Optional[Dict[str, Any]], str]:
    stored = load_stored_ground_truth(path.name, stored_directories)
    if stored is not None:
        return stored, "existing_qa_ground_truth"
    if allow_pdf_build and path.suffix.lower() == ".pdf" and not requires_ocr:
        built = build_trusted_pdf_ground_truth(path, path.name)
        if built is not None:
            return built, "independent_pdf_read"
    return None, "none"
