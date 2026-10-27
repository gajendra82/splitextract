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
    "receipt": "receipts_qty",
    "receipts": "receipts_qty",
    "receiptqty": "receipts_qty",
    "issue": "sales_qty",
    "issueqty": "sales_qty",
    "closing": "closing_qty",
    "closingqty": "closing_qty",
    "closingstock": "closing_qty",
    "productname": "product_name",
    "product": "product_name",
    "itemname": "product_name",
    "packing": "packing",
}
_NUMBER_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
_TOTAL_RE = re.compile(r"^(?:grand\s*)?total\b", re.I)
_VALUE_HEADER_RE = re.compile(r"value|amount|amt|rate|mrp|free|scheme", re.I)
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def apply_source_grounding(
    result: Dict[str, Any],
    file_bytes: bytes,
    filename: str = "",
) -> Dict[str, Any]:
    """Stamp source disagreements onto ``result``. Quantities are left as extracted."""
    if not isinstance(result, dict):
        return result
    report = _safe_report(result, file_bytes or b"", filename or "")
    _store(result, report)
    if report.get("status") != "checked":
        return result
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


def _safe_report(result: Dict[str, Any], file_bytes: bytes, filename: str) -> Dict[str, Any]:
    try:
        return _build_report(result, file_bytes, filename)
    except Exception:
        logger.warning("source grounding failed for %s", filename, exc_info=True)
        return _insufficient("reader_error")


def _build_report(result: Dict[str, Any], file_bytes: bytes, filename: str) -> Dict[str, Any]:
    items = _result_items(result)
    if not items:
        return _insufficient("no_line_items")
    if not file_bytes:
        return _insufficient("no_source_bytes")
    ext = _extension(filename, file_bytes)
    observed = _observe(file_bytes, ext)
    if not observed:
        return _insufficient("no_source_table", reader=ext or "unknown")
    reader, source_items, align = observed
    return _compare(items, source_items, reader=reader, align=align)


def _observe(
    file_bytes: bytes, ext: str
) -> Optional[Tuple[str, List[Dict[str, Any]], str]]:
    if ext == ".pdf":
        return _observe_pdf(file_bytes)
    if ext in {".xlsx", ".xlsm"}:
        matrix = _xlsx_matrix(file_bytes)
        rows = _observations_from_matrix(matrix) if matrix else None
        if rows:
            return "spreadsheet_cells", rows, "name"
        return None
    if ext == ".xls":
        matrix = _xls_matrix(file_bytes)
        rows = _observations_from_matrix(matrix) if matrix else None
        if rows:
            return "spreadsheet_cells", rows, "name"
        return None
    if ext == ".docx":
        matrix = _docx_matrix(file_bytes)
        rows = _observations_from_matrix(matrix) if matrix else None
        if rows:
            return "docx_table", rows, "name"
        return None
    return None


def _observe_pdf(
    file_bytes: bytes,
) -> Optional[Tuple[str, List[Dict[str, Any]], str]]:
    import fitz

    from services.sales_statement_extractor import _parse_swil_opbal_lastpurc_from_pages

    doc = None
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        texts = [(page.get_text("text") or "") for page in doc]
        words = [page.get_text("words") or [] for page in doc]
        parsed = _parse_swil_opbal_lastpurc_from_pages(texts, words, "source.pdf")
        if isinstance(parsed, dict):
            rows = [item for item in (parsed.get("line_items") or []) if isinstance(item, dict)]
            if rows:
                return "pdf_word_positions", rows, "index"
        generic = _generic_pdf_rows(doc)
        if generic:
            return "pdf_word_positions", generic, "name"
    except Exception:
        return None
    finally:
        if doc is not None:
            doc.close()
    return None


def _generic_pdf_rows(doc) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for page_index, page in enumerate(doc):
        words = page.get_text("words") or []
        clusters = _cluster_words(words)
        header_fields: List[Tuple[str, float]] = []
        header_y = None
        for y, tokens in clusters:
            mapped: Dict[str, float] = {}
            for token, x0, x1 in tokens:
                field = _header_field(token)
                if field in _QTY_FIELDS and field not in mapped:
                    mapped[field] = (x0 + x1) / 2.0
            if len(mapped) >= 2 and len(mapped) > len(header_fields):
                header_fields = sorted(mapped.items(), key=lambda item: item[1])
                header_y = y
        if len(header_fields) < 2 or header_y is None:
            continue
        first_x = header_fields[0][1]
        for y, tokens in clusters:
            if abs(y - header_y) <= 8:
                continue
            obs: Dict[str, Any] = {}
            name_parts: List[str] = []
            for token, x0, x1 in tokens:
                if _NUMBER_RE.fullmatch(token.replace(" ", "")):
                    field = _nearest_field(x1, header_fields)
                    if field and field not in obs:
                        obs[field] = token
                elif x0 < first_x - 4:
                    name_parts.append(token)
            name = " ".join(name_parts).strip()
            if not name or _TOTAL_RE.match(name):
                continue
            if not any(field in obs for field in _QTY_FIELDS):
                continue
            obs["product_name"] = name
            obs["source_page"] = page_index + 1
            found.append(obs)
    return found


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


def _nearest_field(x: float, header_fields: Sequence[Tuple[str, float]]) -> Optional[str]:
    best_field = None
    best_distance = None
    for field, center in header_fields:
        distance = abs(x - center)
        if best_distance is None or distance < best_distance:
            best_field = field
            best_distance = distance
    if best_field is None or not header_fields:
        return None
    centers = [center for _field, center in header_fields]
    gaps = [centers[index + 1] - centers[index] for index in range(len(centers) - 1)]
    limit = (min(gaps) / 2.0) if gaps else 36.0
    if best_distance is not None and best_distance <= max(limit, 18.0):
        return best_field
    return None


def _observations_from_matrix(matrix: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
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
        return []
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
        if not any(_number(obs.get(field)) is not None or field in obs for field in _QTY_FIELDS):
            continue
        observations.append(obs)
    return observations


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


def _compare(
    extracted: List[Dict[str, Any]],
    source_items: List[Dict[str, Any]],
    *,
    reader: str,
    align: str,
) -> Dict[str, Any]:
    discrepancies: List[Dict[str, Any]] = []
    blank_as_zero = 0
    compared = 0
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
                row_findings, blanks = _compare_row(got, src)
                blank_as_zero += blanks
                compared += 1
                for finding in row_findings:
                    finding["index"] = index
                    discrepancies.append(finding)
    else:
        pairs = _match_by_name(extracted, source_items)
        for index, src in pairs:
            row_findings, blanks = _compare_row(extracted[index], src)
            blank_as_zero += blanks
            compared += 1
            for finding in row_findings:
                finding["index"] = index
                discrepancies.append(finding)
    blocking = [item for item in discrepancies if item.get("code") != "blank_read_as_zero"]
    return {
        "status": "checked",
        "reader": reader,
        "compared_rows": compared,
        "discrepancy_count": len(blocking),
        "blank_read_as_zero": blank_as_zero,
        "discrepancies": blocking[:30],
    }


def _compare_row(
    extracted: Dict[str, Any], source: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], int]:
    findings: List[Dict[str, Any]] = []
    blanks = 0
    product = str(extracted.get("product_name") or "")
    source_name = str(source.get("product_name") or "")
    if _norm(product) and _norm(source_name) and _norm(product) != _norm(source_name):
        findings.append(
            _finding(product, "product_name", product, source_name, "value_mismatch", source)
        )
    source_pack = _norm_pack(_field(source, "packing"))
    extracted_pack = _norm_pack(extracted.get("packing"))
    if source_pack and extracted_pack and source_pack != extracted_pack:
        findings.append(
            _finding(product, "packing", extracted.get("packing"), _field(source, "packing"), "value_mismatch", source)
        )
    for field in _QTY_FIELDS + _EXTRA_QTY_FIELDS:
        if not _has_field(source, field):
            continue
        code = _classify(_field(extracted, field), _field(source, field))
        if code == "blank_read_as_zero":
            blanks += 1
            continue
        if code:
            findings.append(
                _finding(product, field, _field(extracted, field), _field(source, field), code, source)
            )
    return findings, blanks


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
    return {
        "product_name": product,
        "field": field,
        "extracted": extracted,
        "source": source,
        "code": code,
        "page": page,
    }


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
        if not candidates:
            continue
        pack = _norm_pack(item.get("packing"))
        if pack:
            packed = [row for row in candidates if _norm_pack(_field(row, "packing")) == pack]
            if len(packed) == 1:
                pairs.append((index, packed[0]))
                continue
        if len(candidates) == 1:
            pairs.append((index, candidates[0]))
    return pairs


def _classify(extracted: Any, source: Any) -> Optional[str]:
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
    if abs(src - ext) > 0.05:
        return "value_mismatch"
    return None


def _stamp(result: Dict[str, Any], report: Dict[str, Any]) -> None:
    items = _result_items(result)
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for finding in report.get("discrepancies") or []:
        index = finding.get("index")
        if not isinstance(index, int):
            index = 0
        grouped.setdefault(index, []).append(finding)
    for index, findings in grouped.items():
        if index < 0 or index >= len(items):
            continue
        messages = []
        for finding in findings:
            messages.append(
                "source: {field} extracted {extracted} but source {reader} has {source}".format(
                    field=finding.get("field"),
                    extracted=_shown(finding.get("extracted")),
                    reader=report.get("reader"),
                    source=_shown(finding.get("source")),
                )
            )
        extra = items[index].get("extra")
        if not isinstance(extra, dict):
            extra = {}
            items[index]["extra"] = extra
        extra["source_discrepancy"] = True
        extra["source_validation_error"] = "; ".join(messages)
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


def _insufficient(reason: str, reader: Optional[str] = None) -> Dict[str, Any]:
    return {
        "status": "insufficient_source_evidence",
        "reader": reader,
        "reason": reason,
        "compared_rows": 0,
        "discrepancy_count": 0,
        "blank_read_as_zero": 0,
        "discrepancies": [],
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
