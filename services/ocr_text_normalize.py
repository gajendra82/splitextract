"""Lossless OCR whitespace normalization for GPT cost reduction."""

import re


def normalize_ocr_whitespace(text: str) -> str:
    """
    Normalize only whitespace — no invoice content is removed.

    - Collapse runs of spaces/tabs (not newlines) to a single space
    - Collapse 3+ consecutive blank lines to a single blank line
    - Strip trailing spaces/tabs on each line
    """
    if not text:
        return ""

    lines = []
    for line in text.splitlines():
        # Collapse repeated internal spaces/tabs; preserve line content.
        normalized_line = re.sub(r"[ \t]+", " ", line).rstrip(" \t")
        lines.append(normalized_line)

    normalized = "\n".join(lines)

    # Collapse excessive blank lines (3+ -> 1 blank line).
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)

    return normalized
