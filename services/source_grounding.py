"""Compare extracted quantities with the source file.

A balanced stock equation is not proof. This re-reads words or cells and
records disagreements. It does not invent values and does not replace them.
Blank source cells are not treated as a printed zero.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree

from services.source_document import (
    assemble_document,
    cell_record,
    empty_document,
    limit_rows,
    table_record,
)

logger = logging.getLogger(__name__)

_QTY_FIELDS = ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
_EXTRA_QTY_FIELDS = (
    "total_stock",
    "shortage_qty",
    "expiry_breakage",
    "near_expiry_qty",
)
_HEADER_FIELDS = {
    "opbal": "opening_qty",
    "opening": "opening_qty",
    "openingqty": "opening_qty",
    "openingstock": "opening_qty",
    "op": "opening_qty",
    "ostk": "opening_qty",
    "opstk": "opening_qty",
    "opstock": "opening_qty",
    "receipt": "receipts_qty",
    "receipts": "receipts_qty",
    "receiptqty": "receipts_qty",
    "recqty": "receipts_qty",
    "purc": "receipts_qty",
    "purchase": "receipts_qty",
    "purqty": "receipts_qty",
    "issue": "sales_qty",
    "issueqty": "sales_qty",
    "issqty": "sales_qty",
    "sale": "sales_qty",
    "saleqty": "sales_qty",
    "closing": "closing_qty",
    "closingqty": "closing_qty",
    "closingstock": "closing_qty",
    "closqty": "closing_qty",
    "clqty": "closing_qty",
    "qoh": "closing_qty",
    "closestk": "closing_qty",
    "closstock": "closing_qty",
    "closestock": "closing_qty",
    "productname": "product_name",
    "product": "product_name",
    "itemname": "product_name",
    "packing": "packing",
}
_NUMBER_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
_TOTAL_RE = re.compile(r"^(?:grand\s*)?totals?\b", re.I)
_VALUE_HEADER_RE = re.compile(r"value|amount|amt|rate|mrp|free|scheme", re.I)
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def apply_source_grounding(
    result: Dict[str, Any],
    file_bytes: bytes,
    filename: str = "",
    ocr_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Stamp source disagreements onto ``result``. Quantities are left as extracted."""
    if not isinstance(result, dict):
        return result
    if ocr_text is None and result.get("_grounding_ocr_attempted"):
        stored = result.get("_grounding_ocr_text")
        ocr_text = stored if isinstance(stored, str) else ""
    elif ocr_text is None:
        stored = result.get("_grounding_ocr_text")
        ocr_text = stored if isinstance(stored, str) else None

    # VL HTML: ground against row/cell text first (avoids cross-row hits).
    extra = ((result.get("totals") or {}).get("extra") or {})
    method = str(extra.get("extraction_method") or "")
    if method == "vl_html_table_parse":
        try:
            from services.row_grounding import apply_row_scoped_grounding

            apply_row_scoped_grounding(
                result,
                table_html=str(extra.get("ocr_table_html") or ocr_text or ""),
            )
            logger.info(
                "source_grounding file=%s scope=row/cell method=%s",
                filename,
                method,
            )
        except Exception:
            logger.debug("row_scoped grounding skipped", exc_info=True)

    # Prefer table HTML as grounding OCR text when Paddle pages timed out empty.
    ground_text = ocr_text
    table_grounded = method == "vl_html_table_parse"
    if table_grounded:
        html = str(extra.get("ocr_table_html") or "").strip()
        if html and (not ground_text or not str(ground_text).strip()):
            ground_text = html

    report = _safe_report(result, file_bytes or b"", filename or "", ground_text)
    produced = report.pop("ocr_text", None)
    if report.get("ocr_invoked"):
        result["_grounding_ocr_attempted"] = True
        result["_grounding_ocr_text"] = produced if isinstance(produced, str) else ""
    _store(result, report)
    if report.get("status") != "checked":
        return result
    # Preserve row-scoped discrepancy stamps when already present.
    if not table_grounded:
        _clear_source_flags(result)
        _stamp(result, report)
    else:
        # Merge page report without wiping row-scope findings.
        row_report = extra.get("row_grounding") if isinstance(extra, dict) else None
        if not (isinstance(row_report, dict) and row_report.get("compared_rows")):
            _clear_source_flags(result)
            _stamp(result, report)
    _store(result, report)
    logger.info(
        "source_grounding file=%s reader=%s compared=%s discrepancies=%s blank_as_zero=%s",
        filename,
        report.get("reader"),
        report.get("compared_rows"),
        report.get("discrepancy_count"),
        report.get("blank_read_as_zero"),
    )
    return result


def _safe_report(
    result: Dict[str, Any],
    file_bytes: bytes,
    filename: str,
    ocr_text: Optional[str],
) -> Dict[str, Any]:
    try:
        return _build_report(result, file_bytes, filename, ocr_text)
    except Exception:
        logger.warning("source grounding failed for %s", filename, exc_info=True)
        return _insufficient("reader_error")


def _build_report(
    result: Dict[str, Any],
    file_bytes: bytes,
    filename: str,
    ocr_text: Optional[str],
) -> Dict[str, Any]:
    items = _result_items(result)
    if not items:
        return _insufficient("no_line_items")
    if not file_bytes and not _usable_text(ocr_text):
        return _insufficient("no_source_bytes")
    ext = _extension(filename, file_bytes)
    observed, evidence = _observe_with_evidence(file_bytes, ext, ocr_text)
    if not observed:
        opened = bool(evidence.get("opened"))
        blocks = (ext in _IMAGE_EXTENSIONS and _looks_like_image(file_bytes, ext)) or (
            ext == ".pdf" and opened and evidence.get("native_chars", 0) < _NATIVE_TEXT_MIN
        )
        return _insufficient(
            "no_source_table",
            reader=evidence.get("reader") or ext or "unknown",
            blocks_verification=blocks,
            ocr_invoked=bool(evidence.get("ocr_invoked")),
            ocr_failed=bool(evidence.get("ocr_failed")),
            ocr_text=evidence.get("ocr_text") or "",
            source_document=evidence.get("source_document")
            or empty_document(
                _format_name(ext),
                evidence.get("reader"),
                "no_source_table",
                blocks_verification=blocks,
            ),
        )
    reader, source_items, align = observed
    report = _compare(items, source_items, reader=reader, align=align)
    report["ocr_invoked"] = bool(evidence.get("ocr_invoked"))
    report["ocr_failed"] = bool(evidence.get("ocr_failed"))
    report["blocks_verification"] = False
    report["source_document"] = evidence.get("source_document") or empty_document(
        _format_name(ext), reader, "source_table_not_retained"
    )
    if evidence.get("ocr_invoked"):
        report["ocr_text"] = evidence.get("ocr_text") or ""
    return report


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_NATIVE_TEXT_MIN = 40


def _usable_text(value: Optional[str]) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _observe_with_evidence(
    file_bytes: bytes,
    ext: str,
    ocr_text: Optional[str],
) -> Tuple[Optional[Tuple[str, List[Dict[str, Any]], str]], Dict[str, Any]]:
    """Read a source table. OCR runs once, and only when no usable text exists."""
    evidence: Dict[str, Any] = {
        "ocr_invoked": False,
        "ocr_failed": False,
        "native_chars": 0,
        "reader": None,
        "ocr_text": "",
    }
    holder: Dict[str, Any] = {}
    if ext == ".pdf":
        observed, native_chars, opened, document = _observe_pdf(file_bytes)
        evidence["native_chars"] = native_chars
        evidence["opened"] = opened
        if observed:
            evidence["reader"] = observed[0]
            evidence["source_document"] = document
            return observed, evidence
        allow_ocr = opened and native_chars < _NATIVE_TEXT_MIN
        text, invoked, failed = _ocr_text_for_source(
            file_bytes, ext, ocr_text, allow_invoke=allow_ocr
        )
        evidence.update(ocr_invoked=invoked, ocr_failed=failed, ocr_text=text, reader="ocr_text")
        observed = _observe_ocr_text(text, reader="ocr_text", holder=holder)
        evidence["source_document"] = holder.get("document")
        return observed, evidence
    if ext in _IMAGE_EXTENSIONS:
        text, invoked, failed = _ocr_text_for_source(
            file_bytes, ext, ocr_text, allow_invoke=_looks_like_image(file_bytes, ext)
        )
        evidence.update(ocr_invoked=invoked, ocr_failed=failed, ocr_text=text, reader="ocr_text")
        observed = _observe_ocr_text(text, reader="ocr_text", holder=holder)
        evidence["source_document"] = holder.get("document")
        return observed, evidence
    observed = _observe(file_bytes, ext, ocr_text, holder=holder)
    if observed:
        evidence["reader"] = observed[0]
    evidence["source_document"] = holder.get("document")
    return observed, evidence


def _ocr_text_for_source(
    file_bytes: bytes,
    ext: str,
    ocr_text: Optional[str],
    *,
    allow_invoke: bool,
) -> Tuple[str, bool, bool]:
    """Return text, whether this call invoked OCR, and whether that text is unusable.

    An empty string means the caller already ran OCR. None means it has not.
    """
    if _usable_text(ocr_text):
        return str(ocr_text), False, False
    if ocr_text == "":
        return "", False, True
    if not allow_invoke or not file_bytes:
        return "", False, False
    try:
        text = _ocr_file_once(file_bytes, ext)
    except Exception:
        logger.warning("source grounding OCR failed", exc_info=True)
        return "", True, True
    return text, True, not _usable_text(text)


def _ocr_file_once(file_bytes: bytes, ext: str) -> str:
    """One pass through the existing OCR orchestrator. No second engine."""
    from services.ocr.orchestrator import OCROrchestrator

    orchestrator = OCROrchestrator()
    pages: List[Tuple[int, bytes]] = []
    if ext == ".pdf":
        import fitz

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            for index, page in enumerate(doc, start=1):
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                pages.append((index, pixmap.tobytes("png")))
        finally:
            doc.close()
    else:
        pages.append((1, file_bytes))
    texts: List[str] = []
    for page_no, image in pages:
        bundle = orchestrator.recognize(image, page=page_no)
        primary = getattr(bundle, "primary", None)
        text = getattr(primary, "text", None) if primary is not None else None
        if _usable_text(text):
            texts.append(str(text))
    return "\n\n".join(texts)


def _observe(
    file_bytes: bytes,
    ext: str,
    ocr_text: Optional[str] = None,
    holder: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, List[Dict[str, Any]], str]]:
    if ext == ".pdf":
        observed, _native, _opened, document = _observe_pdf(file_bytes)
        _publish(holder, document)
        if observed:
            return observed
        return _observe_ocr_text(ocr_text, reader="ocr_text", holder=holder)
    if ext in {".xlsx", ".xlsm", ".xls"}:
        rows, document = _spreadsheet_rows(file_bytes, ext)
        _publish(holder, document)
        if rows:
            return "spreadsheet_cells", rows, "name"
        return None
    if ext == ".docx":
        matrix = _docx_matrix(file_bytes)
        rows = _rows_from_matrix(matrix) if matrix else None
        if rows:
            _publish(holder, _document_for_matrix(matrix, "docx_table", "docx"))
            return "docx_table", rows, "name"
        text = _docx_paragraph_text(file_bytes)
        sale_rows = _sale_closing_observations(text)
        if sale_rows:
            return "docx_paragraphs", sale_rows, "name"
        if text.strip():
            return _observe_text_bytes(text.encode("utf-8"), ".txt", holder=holder)
        return None
    if ext in {".txt", ".htm", ".html"}:
        return _observe_text_bytes(file_bytes, ext, holder=holder)
    if ext == ".doc":
        return _observe_legacy_doc(file_bytes, holder=holder)
    if ext in _IMAGE_EXTENSIONS:
        return _observe_ocr_text(ocr_text, reader="ocr_text", holder=holder)
    if ocr_text:
        return _observe_ocr_text(ocr_text, reader="ocr_text", holder=holder)
    return None


def _looks_like_image(file_bytes: bytes, ext: str) -> bool:
    if not file_bytes:
        return False
    if ext == ".png":
        return file_bytes.startswith(b"\x89PNG")
    if ext in {".jpg", ".jpeg"}:
        return file_bytes.startswith(b"\xff\xd8")
    if ext == ".bmp":
        return file_bytes.startswith(b"BM")
    if ext in {".tif", ".tiff"}:
        return file_bytes.startswith(b"II*\x00") or file_bytes.startswith(b"MM\x00*")
    if ext == ".webp":
        return file_bytes.startswith(b"RIFF") and b"WEBP" in file_bytes[:16]
    return False


def _observe_pdf(
    file_bytes: bytes,
) -> Tuple[Optional[Tuple[str, List[Dict[str, Any]], str]], int, bool]:
    import fitz

    from services.sales_statement_extractor import _parse_swil_opbal_lastpurc_from_pages

    doc = None
    native_chars = 0
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        texts = [(page.get_text("text") or "") for page in doc]
        native_chars = sum(len(text.strip()) for text in texts)
        words = [page.get_text("words") or [] for page in doc]
        independent, document = _independent_pdf_rows(doc)
        if independent:
            return ("pdf_header_positions", independent, "name"), native_chars, True, document
        parsed = _parse_swil_opbal_lastpurc_from_pages(texts, words, "source.pdf")
        if isinstance(parsed, dict):
            rows = [item for item in (parsed.get("line_items") or []) if isinstance(item, dict)]
            if rows:
                return (
                    ("pdf_word_positions", rows, "index"),
                    native_chars,
                    True,
                    _document_from_observations(rows, "pdf_word_positions", "pdf"),
                )
    except Exception:
        return None, native_chars, False, None
    finally:
        if doc is not None:
            doc.close()
    return None, native_chars, True, None


def medivision_scm_observations(doc) -> List[Dict[str, Any]]:
    """Op / Purc / Scm / S.R. / Sale / Scm / Add / Less / Cl qty.

    The sale-side scheme is a separate column. Closing is the first Cl,
    not Cl val. This layout only.
    """
    rows: List[Dict[str, Any]] = []
    for page_index, page in enumerate(doc):
        clusters = _cluster_words(page.get_text("words") or [])
        columns = _medivision_scm_columns(clusters)
        if not columns:
            continue
        opening_x = dict(columns)["opening_qty"]
        for _y, tokens in clusters:
            labels = {token.casefold().rstrip(".") for token, _x0, _x1 in tokens}
            if labels <= {"qty", "val"}:
                continue
            if {"op", "purc", "sale"}.issubset(labels):
                continue
            assigned: Dict[str, str] = {}
            name_parts: List[str] = []
            pack_parts: List[str] = []
            for token, x0, x1 in tokens:
                center = (x0 + x1) / 2.0
                if center < opening_x - 55:
                    if center >= opening_x - 110:
                        pack_parts.append(token)
                    else:
                        name_parts.append(token)
                    continue
                field = _nearest_medivision_column(center, columns)
                compact = token.replace(",", "")
                if field and field not in assigned and _NUMBER_RE.fullmatch(compact):
                    assigned[field] = compact
            name = " ".join(name_parts).strip()
            if not name or _TOTAL_RE.match(name):
                continue
            if not any(assigned.get(field) for field in _QTY_FIELDS):
                continue
            obs: Dict[str, Any] = {
                "product_name": name,
                "source_page": page_index + 1,
            }
            if pack_parts:
                obs["packing"] = " ".join(pack_parts).strip()
            obs.update(assigned)
            rows.append(obs)
    return rows


def _medivision_scm_columns(
    clusters: Sequence[Tuple[float, List[Tuple[str, float, float]]]]
) -> List[Tuple[str, float]]:
    for _y, tokens in clusters:
        words = [(token.casefold().rstrip("."), (x0 + x1) / 2.0) for token, x0, x1 in tokens]
        labels = {word for word, _center in words}
        if not {"op", "purc", "sale", "cl"} <= labels:
            continue
        if "s.r" not in labels and "sr" not in labels and "add" not in labels:
            continue
        columns: List[Tuple[str, float]] = []
        scheme = 0
        closing = 0
        for word, center in words:
            field = None
            if word == "op":
                field = "opening_qty"
            elif word == "purc":
                field = "receipts_qty"
            elif word in {"s.r", "sr"}:
                field = "sales_return_qty"
            elif word == "sale":
                field = "sales_qty"
            elif word == "scm":
                scheme += 1
                field = "scheme_in_qty" if scheme == 1 else "scheme_out_qty"
            elif word == "add":
                field = "add_qty"
            elif word == "less":
                field = "less_qty"
            elif word == "sa":
                field = "sales_value"
            elif word == "cl":
                closing += 1
                field = "closing_qty" if closing == 1 else "closing_value"
            if field:
                columns.append((field, center))
        if {"opening_qty", "receipts_qty", "sales_qty", "closing_qty"} <= {
            field for field, _center in columns
        }:
            return columns
    return []


def _nearest_medivision_column(
    x: float, columns: Sequence[Tuple[str, float]]
) -> Optional[str]:
    field, center = min(columns, key=lambda item: abs(x - item[1]))
    if abs(x - center) > 28:
        return None
    return field


def _independent_pdf_rows(doc) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Read quantities from the nearest printed header, including unlabeled columns.

    Numbers to the left of the first quantity header stay in the product name.
    This does not call the statement parser.
    """
    medivision = medivision_scm_observations(doc)
    if medivision:
        return medivision, None
    found: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []
    for page_index, page in enumerate(doc):
        clusters = _cluster_words(page.get_text("words") or [])
        header: List[Tuple[Optional[str], float, float, str]] = []
        header_y = None
        best_known = 0
        for y, tokens in clusters:
            columns: List[Tuple[Optional[str], float, float, str]] = []
            known = 0
            for token, x0, x1 in tokens:
                field = _header_field(token)
                if field not in _QTY_FIELDS and field != "packing":
                    field = None
                if field in _QTY_FIELDS:
                    known += 1
                columns.append((field, (x0 + x1) / 2.0, x0, token))
            if known >= 2 and known > best_known:
                best_known = known
                header = columns
                header_y = y
        if not header or header_y is None:
            continue
        qty_columns = [item for item in header if item[0] in _QTY_FIELDS]
        pack_columns = [item for item in header if item[0] == "packing"]
        qty_left = min(item[2] for item in qty_columns)
        name_right = min(item[2] for item in pack_columns) if pack_columns else qty_left
        labels = {item[0]: item[3] for item in qty_columns if item[0]}
        header_cells = [
            cell_record(
                item[3],
                row=0,
                column=index,
                header=item[3],
                field=item[0],
                page=page_index + 1,
                coordinates={"x0": item[2], "y": header_y},
            )
            for index, item in enumerate(header)
        ]
        page_rows: List[Dict[str, Any]] = []
        for y, tokens in clusters:
            if abs(y - header_y) <= 8:
                continue
            obs: Dict[str, Any] = {"source_columns": dict(labels), "source_page": page_index + 1}
            name_parts: List[str] = []
            pack_parts: List[str] = []
            cells: List[Dict[str, Any]] = []
            for token, x0, x1 in tokens:
                numeric = bool(_NUMBER_RE.fullmatch(token.replace(" ", "").replace(",", "")))
                if x0 < name_right - 2 and not (numeric and x0 >= qty_left - 4):
                    name_parts.append(token)
                    continue
                if numeric and x0 >= qty_left - 4:
                    field = _nearest_labeled(x1, header)
                    if field and field not in obs:
                        obs[field] = token
                        cells.append(
                            cell_record(
                                token,
                                row=len(page_rows) + 1,
                                column=len(cells),
                                header=labels.get(field),
                                field=field,
                                page=page_index + 1,
                                coordinates={"x0": x0, "x1": x1, "y": y},
                            )
                        )
                    continue
                if pack_columns and name_right - 2 <= x0 < qty_left - 2:
                    pack_parts.append(token)
            name = " ".join(name_parts).strip()
            if not name or _TOTAL_RE.match(name):
                continue
            if pack_parts:
                obs["packing"] = " ".join(pack_parts).strip()
            for field in labels:
                if field not in obs:
                    obs[field] = None
                    cells.append(
                        cell_record(
                            None,
                            row=len(page_rows) + 1,
                            column=len(cells),
                            header=labels.get(field),
                            field=field,
                            page=page_index + 1,
                        )
                    )
            if not any(obs.get(field) not in (None, "") for field in _QTY_FIELDS):
                continue
            obs["product_name"] = name
            cells.insert(
                0,
                cell_record(
                    name,
                    row=len(page_rows) + 1,
                    column=0,
                    header="product_name",
                    field="product_name",
                    page=page_index + 1,
                ),
            )
            found.append(obs)
            page_rows.append(
                {"index": len(page_rows), "page": page_index + 1, "sheet": None, "cells": cells}
            )
        if page_rows:
            tables.append(
                table_record(
                    reader="pdf_header_positions",
                    headers=header_cells,
                    rows=page_rows,
                    page=page_index + 1,
                )
            )
    document = None
    if tables:
        document = assemble_document(
            file_format="pdf",
            reader="pdf_header_positions",
            tables=tables,
            sufficient=True,
        )
    return found, document


def _nearest_labeled(
    x: float, columns: Sequence[Tuple[Optional[str], float, float, str]]
) -> Optional[str]:
    if not columns:
        return None
    field, _center, _x0, _token = min(columns, key=lambda item: abs(x - item[1]))
    return field if field in _QTY_FIELDS else None


def _cluster_words(words: Sequence[Sequence[Any]]) -> List[Tuple[float, List[Tuple[str, float, float]]]]:
    ordered = sorted(
        (word for word in words if len(word) >= 5),
        key=lambda word: (round(float(word[1]), 1), float(word[0])),
    )
    clusters: List[Dict[str, Any]] = []
    for word in ordered:
        if len(word) < 5:
            continue
        token = str(word[4]).strip()
        if not token:
            continue
        x0, y0, x1 = float(word[0]), float(word[1]), float(word[2])
        if clusters and abs(y0 - clusters[-1]["y"]) <= 3.5:
            clusters[-1]["tokens"].append((token, x0, x1))
        else:
            clusters.append({"y": y0, "tokens": [(token, x0, x1)]})
    return [(cluster["y"], cluster["tokens"]) for cluster in clusters]


def _rows_from_matrix(matrix: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    rows, _ambiguous = _observations_from_matrix(matrix)
    return rows


def _observations_from_matrix(
    matrix: Sequence[Sequence[Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    header_index = None
    mapping: Dict[int, str] = {}
    best = 0
    for index, row in enumerate(list(matrix)[:25]):
        mapped = _map_header_row(row)
        qty_count = len({field for field in mapped.values() if field in _QTY_FIELDS})
        if qty_count > best:
            best = qty_count
            header_index = index
            mapping = mapped
    if header_index is None or best < 2:
        return [], []
    labels = {
        field: _text(matrix[header_index][column]) or field
        for column, field in mapping.items()
        if field in _QTY_FIELDS and column < len(matrix[header_index])
    }
    ambiguous = _ambiguous_headers(matrix[header_index])
    observations = []
    for row in list(matrix)[header_index + 1 :]:
        obs: Dict[str, Any] = {}
        for column, field in mapping.items():
            if column >= len(row):
                continue
            obs[field] = row[column]
        name = _text(obs.get("product_name"))
        if not name:
            for cell in row:
                text = _text(cell)
                if text and _header_field(text) is None and not _NUMBER_RE.fullmatch(text.replace(",", "")):
                    name = text
                    obs["product_name"] = text
                    break
        if not name or _TOTAL_RE.match(name):
            continue
        if re.match(r"^(?:division|company)\s*:", name, re.I):
            continue
        if not any(_number(obs.get(field)) is not None or field in obs for field in _QTY_FIELDS):
            continue
        if labels:
            obs["source_columns"] = dict(labels)
        if ambiguous:
            obs["_ambiguous_columns"] = list(ambiguous)
        observations.append(obs)
    return observations, ambiguous


def _map_header_row(row: Sequence[Any]) -> Dict[int, str]:
    found: Dict[int, str] = {}
    used = set()
    for index, cell in enumerate(row):
        text = _text(cell)
        if not text or _VALUE_HEADER_RE.search(text):
            continue
        field = _header_field(text)
        if not field or field in used:
            if field in used:
                found = {column: mapped for column, mapped in found.items() if mapped != field}
                used.discard(field)
            continue
        found[index] = field
        used.add(field)
    return found


def _header_field(text: str) -> Optional[str]:
    return _HEADER_FIELDS.get(re.sub(r"[^a-z0-9]", "", text.lower()))


def _ambiguous_headers(row: Sequence[Any]) -> List[str]:
    seen = set()
    ambiguous = []
    for cell in row:
        text = _text(cell)
        if not text or _VALUE_HEADER_RE.search(text):
            continue
        field = _header_field(text)
        if field not in _QTY_FIELDS:
            continue
        if field in seen and field not in ambiguous:
            ambiguous.append(field)
        seen.add(field)
    return ambiguous


def _compare(
    extracted: List[Dict[str, Any]],
    source_items: List[Dict[str, Any]],
    *,
    reader: str,
    align: str,
) -> Dict[str, Any]:
    discrepancies: List[Dict[str, Any]] = []
    blank_as_zero = 0
    explicit_zero = 0
    compared = 0
    ambiguous = []
    for source in source_items:
        if isinstance(source, dict):
            for field in source.pop("_ambiguous_columns", []) or []:
                if field not in ambiguous:
                    ambiguous.append(field)
    for field in ambiguous:
        discrepancies.append(
            {
                "product_name": None,
                "field": field,
                "extracted": None,
                "source": None,
                "code": "ambiguous_column",
            }
        )
    if align == "index":
        if len(extracted) != len(source_items):
            discrepancies.append(
                {
                    "product_name": None,
                    "field": "line_items",
                    "extracted": len(extracted),
                    "source": len(source_items),
                    "code": "row_count_mismatch",
                }
            )
        else:
            for index, (got, src) in enumerate(zip(extracted, source_items)):
                row_findings, blanks, zeros = _compare_row(got, src)
                blank_as_zero += blanks
                explicit_zero += zeros
                compared += 1
                for finding in row_findings:
                    finding["index"] = index
                    discrepancies.append(finding)
    else:
        pairs = _match_by_name(extracted, source_items)
        matched_indexes = set()
        matched_sources = set()
        for index, src in pairs:
            matched_indexes.add(index)
            matched_sources.add(id(src))
            row_findings, blanks, zeros = _compare_row(extracted[index], src)
            blank_as_zero += blanks
            explicit_zero += zeros
            compared += 1
            for finding in row_findings:
                finding["index"] = index
                discrepancies.append(finding)
        if _name_alignment_is_reliable(compared, len(extracted)):
            discrepancies.extend(
                _coverage_findings(extracted, source_items, matched_indexes, matched_sources, reader)
            )
        elif source_items:
            discrepancies.append(
                {
                    "product_name": None,
                    "field": "line_items",
                    "extracted": len(extracted),
                    "source": len(source_items),
                    "compared": compared,
                    "code": "low_name_match",
                }
            )
    return {
        "status": "checked",
        "reader": reader,
        "compared_rows": compared,
        "discrepancy_count": len(discrepancies),
        "blank_read_as_zero": blank_as_zero,
        "explicit_zero": explicit_zero,
        "unreadable": sum(1 for item in discrepancies if item.get("code") == "unreadable_source"),
        "missing_rows": sum(1 for item in discrepancies if item.get("code") == "missing_row"),
        "duplicate_rows": sum(1 for item in discrepancies if item.get("code") == "duplicate_row"),
        "low_name_match": sum(1 for item in discrepancies if item.get("code") == "low_name_match"),
        "discrepancies": discrepancies[:30],
    }


def _name_alignment_is_reliable(compared: int, extracted_count: int) -> bool:
    """Detailed missing-row checks need a majority of names to align.

    Below that, one document-level flag is used. Per-row missing or duplicate
    labels would treat an alignment failure as extra products.
    """
    if extracted_count <= 0:
        return False
    return compared * 2 > extracted_count


def _compare_row(
    extracted: Dict[str, Any], source: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], int, int]:
    findings: List[Dict[str, Any]] = []
    blanks = 0
    zeros = 0
    product = str(extracted.get("product_name") or "")
    source_name = str(source.get("product_name") or "")
    if _norm(product) and _norm(source_name) and _norm(product) != _norm(source_name):
        if not _covers(product, source_name):
            findings.append(
                _finding(product, "product_name", product, source_name, "value_mismatch", source)
            )
    source_pack = _norm_pack(_field(source, "packing"))
    extracted_pack = _norm_pack(extracted.get("packing"))
    if source_pack and extracted_pack and source_pack != extracted_pack:
        blob = _norm_pack(
            "{0}{1}".format(source.get("product_name") or "", _field(source, "packing") or "")
        )
        if extracted_pack not in blob and source_pack not in extracted_pack:
            findings.append(
                _finding(product, "packing", extracted.get("packing"), _field(source, "packing"), "value_mismatch", source)
            )
    for field in _QTY_FIELDS + _EXTRA_QTY_FIELDS:
        if not _has_field(source, field):
            continue
        code = _classify(_field(extracted, field), _field(source, field))
        if code == "blank_read_as_zero":
            blanks += 1
            if field in _QTY_FIELDS:
                findings.append(
                    _finding(
                        product,
                        field,
                        _field(extracted, field),
                        _field(source, field),
                        code,
                        source,
                    )
                )
            continue
        if code == "explicit_zero":
            zeros += 1
            continue
        if code:
            findings.append(
                _finding(product, field, _field(extracted, field), _field(source, field), code, source)
            )
    return findings, blanks, zeros


def _finding(
    product: str,
    field: str,
    extracted: Any,
    source: Any,
    code: str,
    source_row: Dict[str, Any],
) -> Dict[str, Any]:
    page = None
    extra = source_row.get("extra") if isinstance(source_row.get("extra"), dict) else {}
    page = source_row.get("source_page") or extra.get("source_page")
    sheet = source_row.get("source_sheet") or extra.get("source_sheet")
    finding = {
        "product_name": product,
        "field": field,
        "extracted": extracted,
        "source": source,
        "code": code,
        "page": page,
        "sheet": sheet,
    }
    columns = source_row.get("source_columns")
    if isinstance(columns, dict) and columns:
        finding["source_columns"] = columns
    return finding


def _coverage_findings(
    extracted: List[Dict[str, Any]],
    source_items: List[Dict[str, Any]],
    matched_indexes: set,
    matched_sources: set,
    reader: str,
) -> List[Dict[str, Any]]:
    findings = []
    seen: Dict[str, int] = {}
    for index, item in enumerate(extracted):
        name = _norm(str(item.get("product_name") or ""))
        if not name:
            continue
        seen[name] = seen.get(name, 0) + 1
        source_matches = sum(
            1
            for source in source_items
            if _norm(str(source.get("product_name") or "")) == name
            or _covers(str(item.get("product_name") or ""), str(source.get("product_name") or ""))
        )
        if seen[name] > source_matches:
            findings.append(
                {
                    "index": index,
                    "product_name": item.get("product_name"),
                    "field": "line_items",
                    "extracted": item.get("product_name"),
                    "source": source_matches,
                    "code": "duplicate_row",
                    "reader": reader,
                }
            )
    source_pages = set()
    extracted_pages = set()
    for item in extracted:
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if extra.get("source_page") not in (None, ""):
            extracted_pages.add(extra.get("source_page"))
    for source in source_items:
        if id(source) in matched_sources:
            if source.get("source_page") not in (None, ""):
                source_pages.add(source.get("source_page"))
            continue
        if any(_covers(str(item.get("product_name") or ""), str(source.get("product_name") or "")) for item in extracted):
            continue
        findings.append(
            {
                "product_name": source.get("product_name"),
                "field": "line_items",
                "extracted": None,
                "source": source.get("product_name"),
                "code": "missing_row",
                "page": source.get("source_page"),
            }
        )
        if source.get("source_page") not in (None, ""):
            source_pages.add(source.get("source_page"))
    if extracted_pages:
        for page in sorted(source_pages - extracted_pages, key=lambda item: str(item)):
            findings.append(
                {
                    "product_name": None,
                    "field": "page",
                    "extracted": None,
                    "source": page,
                    "code": "incomplete_page",
                    "page": page,
                }
            )
    return findings


def _covers(extracted_name: str, source_name: str) -> bool:
    wanted = _tokens(extracted_name)
    have = _tokens(source_name)
    if not wanted or not have:
        return False
    for token in wanted:
        if not any(token == piece or token in piece or piece in token for piece in have):
            return False
    return True


def _tokens(value: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", _norm(value))


def _match_by_name(
    extracted: List[Dict[str, Any]], source_items: List[Dict[str, Any]]
) -> List[Tuple[int, Dict[str, Any]]]:
    pools: Dict[str, List[Dict[str, Any]]] = {}
    for source in source_items:
        pools.setdefault(_norm(str(source.get("product_name") or "")), []).append(source)
    pairs = []
    for index, item in enumerate(extracted):
        key = _norm(str(item.get("product_name") or ""))
        candidates = list(pools.get(key) or [])
        pack = _norm_pack(item.get("packing"))
        if candidates and pack:
            packed = [row for row in candidates if _norm_pack(_field(row, "packing")) == pack]
            if len(packed) == 1:
                pairs.append((index, packed[0]))
                continue
        if len(candidates) == 1:
            pairs.append((index, candidates[0]))
            continue
        scored = []
        for source in source_items:
            if _covers(str(item.get("product_name") or ""), str(source.get("product_name") or "")):
                overlap = len(
                    set(_tokens(str(item.get("product_name") or "")))
                    & set(_tokens(str(source.get("product_name") or "")))
                )
                scored.append((overlap, source))
        if not scored:
            continue
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if len(scored) == 1 or scored[0][0] > scored[1][0]:
            pairs.append((index, scored[0][1]))
    return pairs


def _classify(extracted: Any, source: Any) -> Optional[str]:
    if _unreadable(source):
        return "unreadable_source"
    src = _number(source)
    ext = _number(extracted)
    if src is None and ext == 0:
        return "blank_read_as_zero"
    if src is None and ext is None:
        return None
    if src is None:
        return "extracted_without_source"
    if ext is None:
        return "source_missing_from_extract"
    if abs(src - ext) <= 0.05:
        if src == 0:
            return "explicit_zero"
        return None
    # ±0.5% relative tolerance (totals / OCR rounding).
    if src != 0 and abs(src - ext) / abs(src) < 0.005:
        return None
    return "value_mismatch"


def _unreadable(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or _is_blank_text(text) or _number(text) is not None:
        return False
    return True


def _is_blank_text(value: str) -> bool:
    return value.strip().upper() in {"-", "—", "--", "–", "NA", "N/A", "NULL", "NONE", "."}


def _stamp(result: Dict[str, Any], report: Dict[str, Any]) -> None:
    items = _result_items(result)
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for finding in report.get("discrepancies") or []:
        index = finding.get("index")
        if not isinstance(index, int):
            continue
        grouped.setdefault(index, []).append(finding)
    for index, findings in grouped.items():
        if index < 0 or index >= len(items):
            continue
        messages = []
        for finding in findings:
            messages.append(
                "source: {field} extracted {extracted} but source {reader} has {source} ({code})".format(
                    field=finding.get("field"),
                    extracted=_shown(finding.get("extracted")),
                    reader=report.get("reader"),
                    source=_shown(finding.get("source")),
                    code=finding.get("code"),
                )
            )
        extra = items[index].get("extra")
        if not isinstance(extra, dict):
            extra = {}
            items[index]["extra"] = extra
        extra["source_discrepancy"] = True
        extra["source_validation_error"] = "; ".join(messages)
        for finding in findings:
            columns = finding.get("source_columns")
            if isinstance(columns, dict) and columns and not extra.get("source_columns"):
                extra["source_columns"] = columns
                break
        page = findings[0].get("page")
        if page:
            extra["source_evidence"] = "page:{0}".format(page)
        existing = str(extra.get("validation_error") or "")
        if not existing or existing.startswith("source:"):
            extra["validation_error"] = extra["source_validation_error"]


def _clear_source_flags(result: Dict[str, Any]) -> None:
    for item in _result_items(result):
        extra = item.get("extra")
        if not isinstance(extra, dict):
            continue
        extra.pop("source_discrepancy", None)
        extra.pop("source_validation_error", None)
        extra.pop("source_evidence", None)
        if str(extra.get("validation_error") or "").startswith("source:"):
            extra.pop("validation_error", None)


def _result_items(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = [item for item in (result.get("line_items") or []) if isinstance(item, dict)]
    if items:
        return items
    rows: List[Dict[str, Any]] = []
    for statement in result.get("statements") or []:
        if isinstance(statement, dict):
            rows.extend(
                item for item in (statement.get("line_items") or []) if isinstance(item, dict)
            )
    return rows


def _store(result: Dict[str, Any], report: Dict[str, Any]) -> None:
    totals = result.get("totals")
    if not isinstance(totals, dict):
        totals = {}
        result["totals"] = totals
    extra = totals.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        totals["extra"] = extra
    extra["source_grounding"] = report


def _insufficient(
    reason: str,
    reader: Optional[str] = None,
    *,
    blocks_verification: bool = False,
    ocr_invoked: bool = False,
    ocr_failed: bool = False,
    ocr_text: str = "",
    source_document: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "status": "insufficient_source_evidence",
        "reader": reader,
        "reason": reason,
        "compared_rows": 0,
        "discrepancy_count": 0,
        "blank_read_as_zero": 0,
        "discrepancies": [],
        "blocks_verification": blocks_verification,
        "ocr_invoked": ocr_invoked,
        "ocr_failed": ocr_failed,
        "ocr_text": ocr_text,
        "source_document": source_document
        or empty_document("unknown", reader, reason, blocks_verification=blocks_verification),
    }


def _has_field(item: Dict[str, Any], field: str) -> bool:
    if field in item:
        return True
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    return field in extra


def _field(item: Dict[str, Any], field: str) -> Any:
    if field in item and item.get(field) not in ("",):
        return item.get(field)
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if field in extra:
        return extra.get(field)
    return item.get(field)


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    try:
        from services.indian_normalize import parse_indian_number

        parsed = parse_indian_number(value)
        if parsed is None:
            return None
        return round(float(parsed), 2)
    except Exception:
        text = str(value).strip().replace(",", "")
        if not text or text.upper() in {"-", "NA", "N/A", "NULL", "NONE"}:
            return None
        try:
            return round(float(text), 2)
        except ValueError:
            return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _norm_pack(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", _text(value).lower())


def _shown(value: Any) -> str:
    if value is None:
        return "null"
    return str(value)


def _extension(filename: str, file_bytes: bytes) -> str:
    name = str(filename or "").lower()
    for ext in (
        ".xlsx",
        ".xlsm",
        ".xls",
        ".docx",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".tif",
        ".tiff",
        ".bmp",
        ".webp",
        ".html",
        ".htm",
        ".txt",
        ".doc",
    ):
        if name.endswith(ext):
            return ext
    if file_bytes.startswith(b"%PDF"):
        return ".pdf"
    if file_bytes.startswith(b"PK"):
        return ".xlsx"
    return ""


def _publish(holder: Optional[Dict[str, Any]], document: Optional[Dict[str, Any]]) -> None:
    if holder is not None and document is not None:
        holder["document"] = document


def _format_name(ext: str) -> str:
    return ext[1:] if ext.startswith(".") else (ext or "unknown")


def _document_for_matrix(
    matrix: Sequence[Sequence[Any]], reader: str, file_format: str
) -> Optional[Dict[str, Any]]:
    table = _table_from_matrix(matrix, reader)
    if table is None:
        return None
    return assemble_document(
        file_format=file_format,
        reader=reader,
        tables=[table],
        sufficient=True,
    )


def _table_from_matrix(
    matrix: Sequence[Sequence[Any]],
    reader: str,
    *,
    sheet: Optional[str] = None,
    page: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    header_index = None
    mapping: Dict[int, str] = {}
    best = 0
    rows = list(matrix)
    for index, row in enumerate(rows[:25]):
        mapped = _map_header_row(row)
        qty_count = len({field for field in mapped.values() if field in _QTY_FIELDS})
        if qty_count > best:
            best = qty_count
            header_index = index
            mapping = mapped
    if header_index is None or best < 2:
        return None
    header_row = list(rows[header_index])
    headers = [
        cell_record(
            raw,
            row=header_index,
            column=column,
            header=_text(raw) or None,
            field=mapping.get(column),
            page=page,
            sheet=sheet,
        )
        for column, raw in enumerate(header_row)
    ]
    body_rows = rows[header_index + 1 :]
    kept, row_count, truncated = limit_rows(body_rows)
    body = []
    for row_index, row in enumerate(kept):
        cells = []
        for column, header_value in enumerate(header_row):
            present = column < len(row)
            cells.append(
                cell_record(
                    row[column] if present else None,
                    row=header_index + 1 + row_index,
                    column=column,
                    header=_text(header_value) or None,
                    field=mapping.get(column),
                    page=page,
                    sheet=sheet,
                    present=present,
                )
            )
        body.append({"index": row_index, "page": page, "sheet": sheet, "cells": cells})
    return table_record(
        reader=reader,
        headers=headers,
        rows=body,
        page=page,
        sheet=sheet,
        ambiguous_columns=_ambiguous_headers(header_row),
        row_count=row_count,
        truncated=truncated,
    )


def _document_from_observations(
    rows: Sequence[Dict[str, Any]], reader: str, file_format: str
) -> Dict[str, Any]:
    """Rows already read by a parser. Coordinates stay absent when it had none."""
    kept, row_count, truncated = limit_rows(list(rows))
    body = []
    for index, row in enumerate(kept):
        if not isinstance(row, dict):
            continue
        page = row.get("source_page")
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        if page is None:
            page = extra.get("source_page")
        cells = []
        column = 0
        for field in ("product_name", "packing") + _QTY_FIELDS:
            if field not in row and field not in extra:
                continue
            value = row.get(field) if field in row else extra.get(field)
            cells.append(
                cell_record(
                    value,
                    row=index,
                    column=column,
                    header=field,
                    field=field,
                    page=page if isinstance(page, int) else None,
                )
            )
            column += 1
        body.append(
            {
                "index": index,
                "page": page if isinstance(page, int) else None,
                "sheet": None,
                "cells": cells,
            }
        )
    table = table_record(
        reader=reader,
        headers=[],
        rows=body,
        row_count=row_count,
        truncated=truncated,
    )
    return assemble_document(
        file_format=file_format,
        reader=reader,
        tables=[table],
        sufficient=True,
    )


def _spreadsheet_rows(
    file_bytes: bytes, ext: str
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []
    for sheet_name, matrix in _spreadsheet_matrices(file_bytes, ext):
        part = _rows_from_matrix(matrix)
        for item in part:
            item["source_sheet"] = sheet_name
        rows.extend(part)
        table = _table_from_matrix(matrix, "spreadsheet_cells", sheet=sheet_name)
        if table is not None:
            tables.append(table)
    document = None
    if tables:
        document = assemble_document(
            file_format=_format_name(ext),
            reader="spreadsheet_cells",
            tables=tables,
            sufficient=True,
        )
    return rows, document


def _spreadsheet_matrices(file_bytes: bytes, ext: str) -> List[Tuple[str, List[List[Any]]]]:
    if ext in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=False)
        try:
            matrices = []
            for sheet in workbook.worksheets:
                matrix = [list(row) for row in sheet.iter_rows(values_only=True)]
                if any(any(cell not in (None, "") for cell in row) for row in matrix):
                    matrices.append((sheet.title, matrix))
            return matrices
        finally:
            workbook.close()
    import xlrd

    book = xlrd.open_workbook(file_contents=file_bytes)
    matrices = []
    for sheet in book.sheets():
        matrix = [
            [sheet.cell_value(row, column) for column in range(sheet.ncols)]
            for row in range(sheet.nrows)
        ]
        if matrix:
            matrices.append((sheet.name, matrix))
    return matrices


def _docx_paragraph_text(file_bytes: bytes) -> str:
    try:
        from docx import Document
    except ImportError:
        return ""
    try:
        document = Document(io.BytesIO(file_bytes))
    except Exception:
        return ""
    return "\n".join(
        paragraph.text for paragraph in document.paragraphs if paragraph.text and paragraph.text.strip()
    )


_SALE_CLOSING_OBS = re.compile(
    r"^(?P<left>.+?)\s+(?P<unit>Pcs|NOS|PC)\s+"
    r"(?P<sq>-?\d+(?:\.\d+)?)\s+(?P<sv>-?\d+(?:\.\d+)?)\s+"
    r"(?P<cq>-?\d+(?:\.\d+)?)\s+(?P<cv>-?\d+(?:\.\d+)?)\s*$",
    re.I,
)


def _sale_closing_observations(text: str) -> Optional[List[Dict[str, Any]]]:
    """SALE qty/value and CLOSING qty/value paragraphs. Opening is not printed."""
    if not text:
        return None
    if not re.search(r"<\s*=+\s*SALE\s*=+\s*>", text, re.I):
        return None
    if not re.search(r"<\s*=+\s*CLOSING\s*=+\s*>", text, re.I):
        return None
    rows: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        match = _SALE_CLOSING_OBS.match(raw.strip())
        if not match:
            continue
        left = re.sub(r"\s+", " ", match.group("left")).strip()
        parts = left.split()
        packing = None
        name = left
        if len(parts) >= 2 and re.search(r"\d", parts[-1]):
            packing = parts[-1]
            name = " ".join(parts[:-1]).strip()
        if not name:
            continue
        rows.append(
            {
                "product_name": name,
                "packing": packing,
                "opening_qty": None,
                "receipts_qty": None,
                "sales_qty": float(match.group("sq")),
                "closing_qty": float(match.group("cq")),
            }
        )
    return rows or None


def _pro_age_observations(text: str) -> Optional[List[Dict[str, Any]]]:
    """DESCRIPTION/PACKING plus Pro. Age. Age is not packing."""
    if not text or not re.search(r"\bPro\.", text, re.I):
        return None
    if not re.search(r"\bAge\b", text, re.I):
        return None
    if not re.search(r"OPENING", text, re.I) or not re.search(r"CLOSING", text, re.I):
        return None
    from services.sales_statement_extractor import _split_pro_age_line

    rows: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or re.search(
            r"DESCRIPTION|OPENING|CLOSING|====|Page\s*:|SALES\s*&\s*STOCK|Pro\.?\s*Age",
            line,
            re.I,
        ):
            continue
        parsed = _split_pro_age_line(line)
        if not parsed:
            continue
        rows.append(
            {
                "product_name": parsed["product_name"],
                "opening_qty": parsed["opening_qty"],
                "receipts_qty": parsed["receipts_qty"],
                "sales_qty": parsed["sales_qty"],
                "closing_qty": parsed["closing_qty"],
            }
        )
    return rows or None


def _daily_stock_observations(text: str) -> Optional[List[Dict[str, Any]]]:
    """Op / Pur / Sales Retu / Sales / Adj / Cl / Order. Order is not a movement."""
    if not text or not re.search(r"Daily\s+Stock\s+Summary", text, re.I):
        return None
    rows: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        tokens = raw.split()
        values: List[float] = []
        index = len(tokens)
        while index > 0:
            token = tokens[index - 1].replace(",", "")
            if _number(token) is None:
                break
            values.append(float(token))
            index -= 1
        values.reverse()
        if len(values) < 7:
            continue
        name = " ".join(tokens[:index]).strip(" |")
        if not name or re.search(r"Product|Daily|Company|Page", name, re.I):
            continue
        opening, purchase, _sales_return, sale, _adjustment, closing, _order = values[:7]
        rows.append(
            {
                "product_name": name,
                "opening_qty": opening,
                "receipts_qty": purchase,
                "sales_qty": sale,
                "closing_qty": closing,
            }
        )
    return rows or None


def _observe_text_bytes(
    file_bytes: bytes,
    ext: str,
    holder: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, List[Dict[str, Any]], str]]:
    text = _decode_text(file_bytes)
    if ext in {".txt", ".text"}:
        pro_age = _pro_age_observations(text)
        if pro_age:
            return "delimited_text", pro_age, "name"
        daily = _daily_stock_observations(text)
        if daily:
            return "delimited_text", daily, "name"
    if ext in {".htm", ".html"} or "<table" in text.lower():
        matrix = _first_html_matrix(text)
        if matrix:
            rows = _rows_from_matrix(matrix)
            if rows:
                _publish(holder, _document_for_matrix(matrix, "html_table", "html"))
                return "html_table", rows, "name"
    matrix = _matrix_from_text(text)
    rows = _rows_from_matrix(matrix) if matrix else []
    if rows:
        _publish(holder, _document_for_matrix(matrix, "delimited_text", _format_name(ext)))
        return "delimited_text", rows, "name"
    return None


def _ostk_purc_qoh_observations(text: str) -> List[Dict[str, Any]]:
    """Read O.Stk / Purc / Tot / Sale / Qoh from the trailing seven printed cells.

    Whitespace splitting glues Pack onto the product name, so column indexes
    then read Purc as opening and Value as Qoh. The seven trailing numbers
    stay in header order even when Pack is not its own cell.
    """
    from services.sales_statement_extractor import (
        _clean_name,
        _is_ostk_purc_sale_qoh_text,
        _trailing_metrics,
    )

    if not _is_ostk_purc_sale_qoh_text(text):
        return []
    rows: List[Dict[str, Any]] = []
    for raw in str(text or "").splitlines():
        raw = raw.strip()
        split = _trailing_metrics(raw, 7)
        if not split:
            continue
        left, metrics = split
        tokens = list(left)
        packing = None
        if len(tokens) >= 2 and re.search(r"\d|'|`|ML|GM|TAB|CAP|SYP", tokens[-1], re.I):
            packing = tokens[-1].rstrip(",")
            tokens = tokens[:-1]
        name = _clean_name(" ".join(tokens))
        if len(name) < 2:
            continue
        if re.search(
            r"PRODUCT\s+NAME|\bPACK\b|O\.?\s*STK|\bQOH\b|\bPURC\b|"
            r"^(?:OPENING|PURCHASE|SALE|CLOSING)\s+VALUE\b",
            name,
            re.I,
        ):
            continue
        rows.append(
            {
                "product_name": name,
                "packing": packing,
                "opening_qty": metrics[0],
                "receipts_qty": metrics[1],
                "sales_qty": metrics[3],
                "closing_qty": metrics[4],
                "source_columns": {
                    "opening_qty": "O.Stk",
                    "receipts_qty": "Purc",
                    "sales_qty": "Sale",
                    "closing_qty": "Qoh",
                },
            }
        )
    return rows


def _observe_legacy_doc(
    file_bytes: bytes,
    holder: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, List[Dict[str, Any]], str]]:
    from services.sales_statement_extractor import _extract_legacy_doc_text

    text = _extract_legacy_doc_text(file_bytes)
    ostk_rows = _ostk_purc_qoh_observations(text)
    if ostk_rows:
        return "legacy_doc_text", ostk_rows, "name"
    matrix = _matrix_from_text(text)
    rows = _rows_from_matrix(matrix) if matrix else []
    if rows:
        _publish(holder, _document_for_matrix(matrix, "legacy_doc_text", "doc"))
        return "legacy_doc_text", rows, "name"
    return None


def _observe_ocr_text(
    ocr_text: Optional[str],
    *,
    reader: str,
    holder: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, List[Dict[str, Any]], str]]:
    if not ocr_text or not str(ocr_text).strip():
        return None
    text = str(ocr_text)
    if "<table" in text.lower():
        matrix = _first_html_matrix(text)
        if matrix:
            rows = _rows_from_matrix(matrix)
            if rows:
                _publish(holder, _document_for_matrix(matrix, reader, "ocr"))
                return reader, rows, "name"
    matrix = _matrix_from_text(text)
    rows = _rows_from_matrix(matrix) if matrix else []
    if rows:
        _publish(holder, _document_for_matrix(matrix, reader, "ocr"))
        return reader, rows, "name"
    return None


def _rows_from_html(text: str) -> List[Dict[str, Any]]:
    matrix = _first_html_matrix(text)
    if not matrix:
        return []
    return _rows_from_matrix(matrix)


def _first_html_matrix(text: str) -> Optional[List[List[Any]]]:
    parser = _HtmlTables()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return None
    for table in parser.tables:
        if _rows_from_matrix(table):
            return table
    return None


class _HtmlTables:
    def __init__(self) -> None:
        from html.parser import HTMLParser

        class _Parser(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.tables: List[List[List[str]]] = []
                self._table: Optional[List[List[str]]] = None
                self._row: Optional[List[str]] = None
                self._parts: Optional[List[str]] = None

            def handle_starttag(self, tag, attrs):
                tag = tag.lower()
                if tag == "table":
                    self._table = []
                elif tag == "tr" and self._table is not None:
                    self._row = []
                elif tag in {"td", "th"} and self._row is not None:
                    self._parts = []

            def handle_endtag(self, tag):
                tag = tag.lower()
                if tag in {"td", "th"} and self._row is not None and self._parts is not None:
                    self._row.append(re.sub(r"\s+", " ", "".join(self._parts)).strip())
                    self._parts = None
                elif tag == "tr" and self._table is not None and self._row is not None:
                    if any(self._row):
                        self._table.append(self._row)
                    self._row = None
                elif tag == "table" and self._table is not None:
                    if self._table:
                        self.tables.append(self._table)
                    self._table = None

            def handle_data(self, data):
                if self._parts is not None:
                    self._parts.append(data)

        self._parser = _Parser()
        self.tables = self._parser.tables

    def feed(self, text: str) -> None:
        self._parser.feed(text)

    def close(self) -> None:
        self._parser.close()


def _rows_from_text(text: str) -> List[Dict[str, Any]]:
    matrix = _matrix_from_text(text)
    if not matrix:
        return []
    return _rows_from_matrix(matrix)


def _matrix_from_text(text: str) -> Optional[List[List[Any]]]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    if sum("\t" in line for line in lines) >= 2:
        return [line.split("\t") for line in lines]
    if sum("|" in line for line in lines) >= 2:
        return [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]
    return [re.split(r"\s{2,}", line) for line in lines]


def _decode_text(file_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("latin-1", errors="ignore")


def _xlsx_matrix(file_bytes: bytes) -> List[List[Any]]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=False)
    try:
        sheet = workbook.active
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def _xls_matrix(file_bytes: bytes) -> List[List[Any]]:
    import xlrd

    book = xlrd.open_workbook(file_contents=file_bytes)
    sheet = book.sheet_by_index(0)
    return [
        [sheet.cell_value(row, column) for column in range(sheet.ncols)]
        for row in range(sheet.nrows)
    ]


def _docx_matrix(file_bytes: bytes) -> List[List[Any]]:
    try:
        from docx import Document
    except ImportError:
        return _docx_matrix_xml(file_bytes)
    document = Document(io.BytesIO(file_bytes))
    for table in document.tables:
        matrix = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        if _observations_from_matrix(matrix):
            return matrix
    return []


def _docx_matrix_xml(file_bytes: bytes) -> List[List[Any]]:
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    for table in root.iter(_W_NS + "tbl"):
        matrix = []
        for row in table.findall(_W_NS + "tr"):
            cells = []
            for cell in row.findall(_W_NS + "tc"):
                texts = [node.text or "" for node in cell.iter(_W_NS + "t")]
                cells.append("".join(texts).strip())
            matrix.append(cells)
        if matrix:
            return matrix
    return []
