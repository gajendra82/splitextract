"""Choose recovery evidence from the document. Does not score identity.

The identity profile decides which rows failed. This module only decides
what to show for those rows. It does not calculate stock, map unknown
headers onto canonical fields, or invent crop coordinates.

An unknown layout or an unsafe header returns no crop. Applying a box
from a different document would send the model the wrong cells.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

KIND_TEXT = "TEXT"
KIND_IMAGE_CROP = "IMAGE_CROP"
KIND_FULL_PAGE_IMAGE = "FULL_PAGE_IMAGE"
KIND_EXCEL_CELLS = "EXCEL_CELLS"
KIND_FULL_DOCUMENT_TEXT = "FULL_DOCUMENT_TEXT"
KIND_NONE = "NONE"

SAFETY_SAFE = "safe"
SAFETY_NO_SAFE_CROP = "NO_SAFE_CROP"
SAFETY_NO_SAFE_EVIDENCE = "NO_SAFE_EVIDENCE"
SAFETY_FULL_DOCUMENT_FALLBACK = "FULL_DOCUMENT_FALLBACK"

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
_EXCEL_SUFFIXES = {".xls", ".xlsx", ".xlsm"}
_TEXT_SUFFIXES = {".txt", ".htm", ".html"}
_WORD_SUFFIXES = {".doc", ".docx"}
_MIN_EMBEDDED_CHARS = 40
_MAX_PDF_PAGES = 8


@dataclass
class RecoveryEvidence:
    """What recovery may send for one target row. Bytes stay out of logs."""

    kind: str
    safety: str
    source_page: Optional[int] = None
    row_id: Optional[str] = None
    header_bounds: Optional[Dict[str, int]] = None
    crop_bounds: Optional[Dict[str, int]] = None
    fallback_reason: Optional[str] = None
    confidence: float = 0.0
    mime_type: Optional[str] = None
    text: Optional[str] = None
    png: Optional[bytes] = None
    headers: List[str] = field(default_factory=list)
    width: Optional[int] = None
    height: Optional[int] = None

    @property
    def is_safe(self) -> bool:
        return self.safety == SAFETY_SAFE and self.kind != KIND_NONE


def select_recovery_evidence(
    document: Any = None,
    failure_type: str = "",
    failing_rows: Optional[Sequence[Dict[str, Any]]] = None,
    identity_profile: Any = None,
    existing_evidence: Any = None,
    *,
    file_bytes: bytes = b"",
    filename: str = "",
    result: Optional[Dict[str, Any]] = None,
) -> List[RecoveryEvidence]:
    """Return one evidence object per target row.

    ``failing_rows`` must already be the identity profile's failed rows.
    This function does not decide which rows failed.
    """
    del result  # identity already selected the rows; values are not re-scored
    payload, name = _document_inputs(document, file_bytes, filename)
    rows = [row for row in (failing_rows or []) if isinstance(row, dict)]
    if failure_type and "stock" not in failure_type.casefold():
        # Row crops are only for stock-identity recovery. Other failures keep
        # the existing document evidence instead of a guessed table box.
        return [_fallback(row, "failure_does_not_use_row_evidence") for row in rows] or [
            _fallback(None, "failure_does_not_use_row_evidence")
        ]
    if identity_profile is not None and not getattr(identity_profile, "supports_recovery", True):
        # Unknown and do-not-score profiles have no stock recovery. A generic
        # crop would pretend a table geometry that was never established.
        return [
            _none(
                row,
                "identity_profile_does_not_support_recovery",
                SAFETY_NO_SAFE_EVIDENCE,
            )
            for row in rows
        ] or [_none(None, "identity_profile_does_not_support_recovery", SAFETY_NO_SAFE_EVIDENCE)]

    suffix = Path(name or "").suffix.lower()
    if not rows:
        return [_none(None, "no_target_rows", SAFETY_NO_SAFE_EVIDENCE)]
    if suffix in _EXCEL_SUFFIXES:
        return _excel_evidence(payload, suffix, rows)
    if suffix in _TEXT_SUFFIXES or suffix in _WORD_SUFFIXES:
        return _prose_evidence(payload, suffix, rows)
    if suffix == ".pdf":
        return _pdf_evidence(payload, rows, existing_evidence)
    if suffix in _IMAGE_SUFFIXES:
        return _image_evidence(payload, name, rows)
    # An unrecognized container has no geometry to recover from.
    return [_none(row, "unsupported_file_kind", SAFETY_NO_SAFE_EVIDENCE) for row in rows]


def _document_inputs(document: Any, file_bytes: bytes, filename: str) -> Tuple[bytes, str]:
    if isinstance(document, dict):
        payload = document.get("file_bytes") or file_bytes or b""
        name = str(document.get("filename") or filename or "")
        return payload, name
    return file_bytes or b"", filename or ""


def _none(row: Optional[Dict[str, Any]], reason: str, safety: str) -> RecoveryEvidence:
    return RecoveryEvidence(
        kind=KIND_NONE if safety == SAFETY_NO_SAFE_EVIDENCE else KIND_FULL_DOCUMENT_TEXT,
        safety=safety,
        row_id=_row_id(row),
        fallback_reason=reason,
        confidence=0.0,
    )


def _fallback(row: Optional[Dict[str, Any]], reason: str, *, page: Optional[int] = None) -> RecoveryEvidence:
    """Existing full-document evidence. No coordinates are attached."""
    return RecoveryEvidence(
        kind=KIND_FULL_PAGE_IMAGE,
        safety=SAFETY_FULL_DOCUMENT_FALLBACK,
        source_page=page,
        row_id=_row_id(row),
        fallback_reason=reason,
        confidence=0.0,
    )


def _row_id(row: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(row, dict):
        return None
    name = str(row.get("product_name") or "").strip()
    return name or None


def _image_evidence(
    file_bytes: bytes,
    filename: str,
    rows: Sequence[Dict[str, Any]],
) -> List[RecoveryEvidence]:
    located = _locate_images(file_bytes, filename, rows)
    if located is None:
        return [_fallback(row, "image_not_readable") for row in rows]
    evidence = []
    for row, crop in zip(rows, located):
        if crop is None:
            evidence.append(
                RecoveryEvidence(
                    kind=KIND_NONE,
                    safety=SAFETY_NO_SAFE_CROP,
                    source_page=1,
                    row_id=_row_id(row),
                    fallback_reason="header_or_row_not_located",
                    confidence=0.0,
                )
            )
            continue
        bounds = {
            "left": int(crop.left),
            "right": int(crop.right),
            "top": int(crop.source_top),
            "bottom": int(crop.source_bottom),
        }
        evidence.append(
            RecoveryEvidence(
                kind=KIND_IMAGE_CROP,
                safety=SAFETY_SAFE,
                source_page=1,
                row_id=crop.product_name or _row_id(row),
                header_bounds=bounds,
                crop_bounds=bounds,
                confidence=0.8,
                mime_type="image/png",
                png=crop.png,
                headers=list(crop.header_labels or ()),
                width=crop.width,
                height=crop.height,
            )
        )
    return evidence


def _locate_images(file_bytes: bytes, filename: str, rows: Sequence[Dict[str, Any]]):
    try:
        from services.stock_row_crops import locate_row_crops

        return locate_row_crops(file_bytes, filename, list(rows))
    except Exception:
        logger.info("RECOVERY_EVIDENCE image locate failed file=%s", filename, exc_info=True)
        return None


def _pdf_evidence(
    file_bytes: bytes,
    rows: Sequence[Dict[str, Any]],
    existing_evidence: Any,
) -> List[RecoveryEvidence]:
    pages = _pdf_pages(file_bytes, existing_evidence)
    if pages is None:
        return [_fallback(row, "pdf_not_readable") for row in rows]
    text_chars = sum(len(text.strip()) for _number, text in pages)
    if text_chars >= _MIN_EMBEDDED_CHARS:
        text_rows = [_pdf_text_row(pages, row) for row in rows]
        if all(item is not None and item["complete"] for item in text_rows):
            return [item["evidence"] for item in text_rows if item is not None]
        # Text is present but a target row is missing cells. Try a visual
        # crop of the page that mentions the row. Do not keep the partial text.
        return _pdf_visual_evidence(file_bytes, pages, rows, reason="pdf_text_incomplete")
    return _pdf_visual_evidence(file_bytes, pages, rows, reason="scanned_pdf")


def _pdf_text_row(pages: Sequence[Tuple[int, str]], row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = _row_id(row) or ""
    hits = []
    for number, text in pages:
        lines = _lines(text)
        matched = [index for index, line in enumerate(lines) if _name_in(line, name)]
        if len(matched) == 1:
            hits.append((number, lines, matched[0]))
    if len(hits) != 1:
        return None
    number, lines, row_index = hits[0]
    header_index = _header_index(lines, row_index)
    if header_index is None:
        return {
            "complete": False,
            "evidence": _fallback(row, "pdf_header_not_in_text", page=number),
        }
    header_cells = _cells(lines[header_index])
    row_cells = _cells(lines[row_index])
    neighbors = _neighbor_lines(lines, header_index, row_index)
    complete = len(header_cells) >= 2 and len(row_cells) >= max(2, len(header_cells) - 1)
    body = _format_text_block(number, header_cells, row_cells, neighbors)
    return {
        "complete": complete,
        "evidence": RecoveryEvidence(
            kind=KIND_TEXT,
            safety=SAFETY_SAFE if complete else SAFETY_FULL_DOCUMENT_FALLBACK,
            source_page=number,
            row_id=name,
            fallback_reason=None if complete else "pdf_text_incomplete",
            confidence=0.9 if complete else 0.0,
            mime_type="text/plain" if complete else None,
            text=body if complete else None,
            headers=list(header_cells),
        ),
    }


def _pdf_visual_evidence(
    file_bytes: bytes,
    pages: Sequence[Tuple[int, str]],
    rows: Sequence[Dict[str, Any]],
    *,
    reason: str,
) -> List[RecoveryEvidence]:
    rendered = _render_pdf_pages(file_bytes)
    if not rendered:
        return [_fallback(row, reason) for row in rows]
    by_page = {number: text for number, text in pages}
    evidence = []
    for row in rows:
        page_number = _page_for_row(by_page, _row_id(row) or "")
        image = rendered.get(page_number or 0)
        if page_number is None or image is None:
            # Several pages mention the row, or none do. Do not reuse page 1's box.
            evidence.append(_fallback(row, "pdf_row_page_ambiguous" if page_number is None else reason, page=page_number))
            continue
        located = _locate_images(image, "page-%s.png" % page_number, [row])
        crop = located[0] if located else None
        if crop is None:
            evidence.append(_fallback(row, "NO_SAFE_CROP", page=page_number))
            continue
        bounds = {
            "left": int(crop.left),
            "right": int(crop.right),
            "top": int(crop.source_top),
            "bottom": int(crop.source_bottom),
        }
        evidence.append(
            RecoveryEvidence(
                kind=KIND_IMAGE_CROP,
                safety=SAFETY_SAFE,
                source_page=page_number,
                row_id=crop.product_name or _row_id(row),
                header_bounds=bounds,
                crop_bounds=bounds,
                confidence=0.7,
                mime_type="image/png",
                png=crop.png,
                headers=list(crop.header_labels or ()),
                width=crop.width,
                height=crop.height,
            )
        )
    return evidence


def _page_for_row(pages: Dict[int, str], name: str) -> Optional[int]:
    if not name:
        return None
    hits = [number for number, text in pages.items() if _name_in(text, name)]
    if len(hits) == 1:
        return hits[0]
    if len(pages) == 1:
        return next(iter(pages))
    return None


def _pdf_pages(
    file_bytes: bytes,
    existing_evidence: Any,
) -> Optional[List[Tuple[int, str]]]:
    existing = _pages_from_existing(existing_evidence)
    if existing:
        return existing
    if not file_bytes:
        return None
    try:
        import fitz
    except Exception:
        logger.info("RECOVERY_EVIDENCE pymupdf unavailable", exc_info=True)
        return None
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception:
        logger.info("RECOVERY_EVIDENCE pdf could not be opened", exc_info=True)
        return None
    pages: List[Tuple[int, str]] = []
    try:
        for index, page in enumerate(doc):
            if index >= _MAX_PDF_PAGES:
                break
            pages.append((index + 1, page.get_text("text") or ""))
    except Exception:
        logger.info("RECOVERY_EVIDENCE pdf text unreadable", exc_info=True)
        return None
    finally:
        doc.close()
    return pages


def _render_pdf_pages(file_bytes: bytes) -> Dict[int, bytes]:
    if not file_bytes:
        return {}
    try:
        import fitz
    except Exception:
        return {}
    rendered: Dict[int, bytes] = {}
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception:
        return {}
    try:
        for index, page in enumerate(doc):
            if index >= _MAX_PDF_PAGES:
                break
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            rendered[index + 1] = pix.tobytes("png")
    except Exception:
        logger.info("RECOVERY_EVIDENCE pdf render failed", exc_info=True)
        return {}
    finally:
        doc.close()
    return rendered


def _pages_from_existing(existing_evidence: Any) -> Optional[List[Tuple[int, str]]]:
    pages = getattr(existing_evidence, "pages", None)
    if not pages:
        return None
    found = []
    for page in pages:
        text = str(getattr(page, "text", "") or "")
        if not text.strip():
            continue
        number = int(getattr(page, "page_number", len(found) + 1) or len(found) + 1)
        found.append((number, text))
    return found or None


def _excel_evidence(
    file_bytes: bytes,
    suffix: str,
    rows: Sequence[Dict[str, Any]],
) -> List[RecoveryEvidence]:
    sheet = _excel_sheet(file_bytes, suffix)
    if sheet is None:
        return [_fallback(row, "excel_not_readable") for row in rows]
    sheet_name, grid = sheet
    header_index = _grid_header_index(grid)
    if header_index is None:
        return [_fallback(row, "excel_header_not_found") for row in rows]
    header = _trim_cells(grid[header_index])
    evidence = []
    for row in rows:
        row_index = _grid_row_index(grid, _row_id(row) or "", header_index)
        if row_index is None:
            evidence.append(_fallback(row, "excel_row_not_found"))
            continue
        body = _format_sheet_block(sheet_name, header, grid, header_index, row_index)
        evidence.append(
            RecoveryEvidence(
                kind=KIND_EXCEL_CELLS,
                safety=SAFETY_SAFE,
                source_page=1,
                row_id=_row_id(row),
                confidence=0.95,
                mime_type=None,
                text=body,
                headers=list(header),
            )
        )
    return evidence


def _excel_sheet(file_bytes: bytes, suffix: str) -> Optional[Tuple[str, List[List[str]]]]:
    if not file_bytes:
        return None
    try:
        if suffix in {".xlsx", ".xlsm"}:
            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
            try:
                sheet = workbook.worksheets[0]
                grid = [
                    ["" if cell is None else str(cell).strip() for cell in row]
                    for row in sheet.iter_rows(max_row=80, max_col=24, values_only=True)
                ]
                return str(sheet.title or "Sheet"), grid
            finally:
                workbook.close()
        import xlrd

        book = xlrd.open_workbook(file_contents=file_bytes)
        sheet = book.sheet_by_index(0)
        grid = []
        for row_index in range(min(sheet.nrows, 80)):
            grid.append(
                [
                    str(sheet.cell_value(row_index, col)).strip()
                    for col in range(min(sheet.ncols, 24))
                ]
            )
        return str(sheet.name or "Sheet"), grid
    except Exception:
        logger.info("RECOVERY_EVIDENCE excel could not be read", exc_info=True)
        return None


def _prose_evidence(
    file_bytes: bytes,
    suffix: str,
    rows: Sequence[Dict[str, Any]],
) -> List[RecoveryEvidence]:
    text = _prose_text(file_bytes, suffix)
    if text is None:
        return [_fallback(row, "text_not_readable") for row in rows]
    lines = _lines(text)
    evidence = []
    for row in rows:
        name = _row_id(row) or ""
        matched = [index for index, line in enumerate(lines) if _name_in(line, name)]
        if len(matched) != 1:
            evidence.append(_fallback(row, "text_row_not_located"))
            continue
        row_index = matched[0]
        header_index = _header_index(lines, row_index)
        if header_index is None:
            evidence.append(_fallback(row, "text_header_not_located"))
            continue
        header_cells = _cells(lines[header_index])
        row_cells = _cells(lines[row_index])
        if len(header_cells) < 2 or len(row_cells) < 2:
            evidence.append(_fallback(row, "text_row_incomplete"))
            continue
        body = _format_text_block(
            1,
            header_cells,
            row_cells,
            _neighbor_lines(lines, header_index, row_index),
        )
        evidence.append(
            RecoveryEvidence(
                kind=KIND_TEXT,
                safety=SAFETY_SAFE,
                source_page=1,
                row_id=name,
                confidence=0.85,
                mime_type="text/plain",
                text=body,
                headers=list(header_cells),
            )
        )
    return evidence


def _prose_text(file_bytes: bytes, suffix: str) -> Optional[str]:
    if not file_bytes:
        return None
    if suffix in _TEXT_SUFFIXES:
        raw = file_bytes.decode("utf-8", errors="replace")
        if suffix in {".htm", ".html"}:
            return _html_to_text(raw)
        return raw
    if suffix == ".docx":
        return _docx_text(file_bytes)
    return None


def _docx_text(file_bytes: bytes) -> Optional[str]:
    try:
        from docx import Document

        document = Document(io.BytesIO(file_bytes))
    except Exception:
        logger.info("RECOVERY_EVIDENCE docx could not be read", exc_info=True)
        return None
    lines = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
    return "\n".join(lines) if lines else None


def _html_to_text(raw: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    text = re.sub(r"(?i)</tr>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", " | ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return text


def _grid_header_index(grid: Sequence[Sequence[str]]) -> Optional[int]:
    best = None
    best_score = 0
    for index, row in enumerate(grid[:25]):
        cells = _trim_cells(row)
        score = _cell_header_score(cells)
        if score > best_score:
            best = index
            best_score = score
    if best_score < 2:
        return None
    return best


def _grid_row_index(grid: Sequence[Sequence[str]], name: str, header_index: int) -> Optional[int]:
    hits = []
    for index, row in enumerate(grid):
        if index == header_index:
            continue
        if _name_in(" ".join(row), name):
            hits.append(index)
    if len(hits) != 1:
        return None
    return hits[0]


def _format_sheet_block(
    sheet_name: str,
    header: Sequence[str],
    grid: Sequence[Sequence[str]],
    header_index: int,
    row_index: int,
) -> str:
    lines = ["Sheet: %s" % sheet_name, "Header: %s" % " | ".join(header)]
    for index in (row_index - 1, row_index, row_index + 1):
        if index < 0 or index >= len(grid) or index == header_index:
            continue
        label = "Row" if index == row_index else "Context"
        lines.append("%s: %s" % (label, " | ".join(_trim_cells(grid[index]))))
    return "\n".join(lines)


def _format_text_block(
    page: int,
    header: Sequence[str],
    row: Sequence[str],
    neighbors: Sequence[str],
) -> str:
    lines = ["Page: %s" % page, "Header: %s" % " | ".join(header), "Row: %s" % " | ".join(row)]
    for neighbor in neighbors:
        lines.append("Context: %s" % neighbor)
    return "\n".join(lines)


def _neighbor_lines(lines: Sequence[str], header_index: int, row_index: int) -> List[str]:
    neighbors = []
    for index in (row_index - 1, row_index + 1):
        if index < 0 or index >= len(lines) or index == header_index:
            continue
        text = lines[index].strip()
        if text:
            neighbors.append(text)
    return neighbors


def _header_index(lines: Sequence[str], row_index: int) -> Optional[int]:
    best = None
    best_score = 0
    for index, line in enumerate(lines[:row_index]):
        score = _cell_header_score(_cells(line))
        if score > best_score:
            best = index
            best_score = score
    if best_score < 2:
        return None
    return best


def _cell_header_score(cells: Sequence[str]) -> int:
    words = [cell for cell in cells if re.search(r"[A-Za-z]", cell or "")]
    numbers = [cell for cell in cells if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", (cell or "").strip())]
    if len(words) < 2 or len(numbers) > len(words):
        return 0
    return len(words)


def _lines(text: str) -> List[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _cells(line: str) -> List[str]:
    if "|" in line:
        return [cell.strip() for cell in line.split("|") if cell.strip()]
    return [cell for cell in line.split() if cell]


def _trim_cells(row: Sequence[str]) -> List[str]:
    cells = [str(cell or "").strip() for cell in row]
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _name_in(text: str, name: str) -> bool:
    if not name or not text:
        return False
    compact = re.sub(r"[^a-z0-9]", "", text.casefold())
    tokens = [token for token in re.findall(r"[a-z0-9]+", name.casefold()) if len(token) >= 4]
    if not tokens:
        tokens = re.findall(r"[a-z0-9]+", name.casefold())
    if not tokens:
        return False
    return all(token in compact for token in tokens[:4])
