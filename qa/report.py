"""JSON, HTML, and Cursor fix-prompt reports."""

from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List


def _json_default(value: Any) -> str:
    return str(value)


def dump_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=_json_default)


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _status_class(status: str) -> str:
    return {
        "PASS": "pass",
        "FAIL": "fail",
        "REVIEW": "review",
        "ERROR": "error",
        "RATE_LIMITED": "limited",
    }.get(status, "error")


def render_html(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    rows = []
    details = []
    for index, item in enumerate(report.get("files") or []):
        status = item.get("status") or ""
        mismatch_count = len(item.get("mismatches") or []) + len(item.get("missing_rows") or []) + len(
            item.get("extra_rows") or []
        )
        rows.append(
            "<tr>"
            f"<td><a href='#f{index}'>{_esc(item.get('file'))}</a></td>"
            f"<td>{_esc(item.get('format'))}</td>"
            f"<td class='{_status_class(status)}'>{_esc(status)}</td>"
            f"<td>{mismatch_count}</td>"
            "</tr>"
        )
        blocks = [
            f"<h3 id='f{index}'>{_esc(item.get('file'))}</h3>",
            "<p>"
            f"Format: {_esc(item.get('format'))} · "
            f"API method: {_esc(item.get('api_method'))} · "
            f"Status: <b class='{_status_class(status)}'>{_esc(status)}</b> · "
            f"Expected rows: {_esc(item.get('expected_rows'))} · "
            f"Extracted rows: {_esc(item.get('extracted_rows'))} · "
            f"Matched: {_esc(item.get('matched_rows'))}"
            "</p>",
        ]
        if item.get("error"):
            blocks.append(f"<pre>{_esc(item.get('error'))}</pre>")
        if item.get("traceback"):
            blocks.append(f"<pre>{_esc(item.get('traceback'))}</pre>")
        for mismatch in (item.get("mismatches") or [])[:40]:
            blocks.append(
                "<div class='pair'>"
                f"<div><b>Expected</b><br>{_esc(mismatch.get('product'))}<br>"
                f"{_esc(mismatch.get('field'))} = {_esc(mismatch.get('expected'))}</div>"
                f"<div><b>Actual</b><br>{_esc(mismatch.get('product'))}<br>"
                f"{_esc(mismatch.get('field'))} = {_esc(mismatch.get('actual'))}</div>"
                f"<div><b>Mismatch</b><br>{_esc(mismatch.get('field'))}"
                f"<br>{_esc(mismatch.get('note') or mismatch.get('kind'))}</div>"
                "</div>"
            )
        for row in (item.get("missing_rows") or [])[:20]:
            blocks.append(
                "<div class='pair'><div><b>Expected, missing from API</b><br>"
                f"{_esc(row.get('product'))}<br>"
                f"opening = {_esc(row.get('opening_qty'))}<br>"
                f"sales = {_esc(row.get('sales_qty'))}<br>"
                f"closing = {_esc(row.get('closing_qty'))}</div>"
                "<div><b>Actual</b><br>not extracted</div>"
                "<div><b>Mismatch</b><br>missing product</div></div>"
            )
        for row in (item.get("extra_rows") or [])[:20]:
            blocks.append(
                "<div class='pair'><div><b>Expected</b><br>not in PDF rows</div>"
                f"<div><b>Actual</b><br>{_esc(row.get('product'))}<br>"
                f"opening = {_esc(row.get('opening_qty'))}<br>"
                f"sales = {_esc(row.get('sales_qty'))}<br>"
                f"closing = {_esc(row.get('closing_qty'))}</div>"
                f"<div><b>Mismatch</b><br>{_esc(row.get('kind'))}</div></div>"
            )
        for note in (item.get("ocr_review") or [])[:15]:
            blocks.append(
                "<p class='review-note'>REVIEW: "
                f"{_esc(note.get('product'))} { _esc(note.get('note') or note.get('field'))}</p>"
            )
        details.append("".join(blocks))

    regression = report.get("regression") or {}
    newly = regression.get("newly_failing") or []
    reg_html = ""
    if newly:
        reg_html = "<h2>Regressions</h2><ul>" + "".join(
            f"<li>{_esc(name)}</li>" for name in newly
        ) + "</ul>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sales statement QA</title>
<style>
body {{ font-family: Georgia, serif; margin: 24px; color: #1c1c1c; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0 28px; }}
th, td {{ border-bottom: 1px solid #ddd; text-align: left; padding: 6px 8px; }}
.cards span {{ display: inline-block; margin-right: 18px; font-size: 18px; }}
.pass {{ color: #0b6b2a; }}
.fail {{ color: #9d1c1c; }}
.review {{ color: #8a5a00; }}
.error {{ color: #555; }}
.limited {{ color: #5c3d99; }}
.pair {{ display: grid; grid-template-columns: 1fr 1fr 180px; gap: 12px;
  border: 1px solid #eee; padding: 8px; margin: 8px 0; }}
pre {{ white-space: pre-wrap; background: #f6f6f6; padding: 8px; }}
.review-note {{ color: #8a5a00; }}
</style>
</head>
<body>
<h1>Sales statement QA</h1>
<p>{_esc(report.get("run_id"))}</p>
<div class="cards">
<span>TOTAL PDFs <b>{_esc(summary.get("total"))}</b></span>
<span class="pass">PASS <b>{_esc(summary.get("pass"))}</b></span>
<span class="fail">FAIL <b>{_esc(summary.get("fail"))}</b></span>
<span class="review">REVIEW <b>{_esc(summary.get("review"))}</b></span>
<span class="error">ERROR <b>{_esc(summary.get("error"))}</b></span>
<span class="limited">RATE_LIMITED <b>{_esc(summary.get("rate_limited") or 0)}</b></span>
</div>
{reg_html}
<table>
<thead><tr><th>PDF</th><th>Format</th><th>Status</th><th>Errors</th></tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
{''.join(details)}
</body>
</html>
"""


def _fmt_value(value: Any) -> str:
    if value is None:
        return "null"
    return str(value)


def _file_issues(item: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    number = 1
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for mismatch in item.get("mismatches") or []:
        grouped[str(mismatch.get("product"))].append(mismatch)
    for product, fields in list(grouped.items())[:12]:
        lines.append(f"{number}. {product}")
        lines.append("Expected:")
        for field in fields:
            lines.append(
                f"{field.get('field')} = {_fmt_value(field.get('expected'))}"
            )
        lines.append("Actual:")
        for field in fields:
            lines.append(
                f"{field.get('field')} = {_fmt_value(field.get('actual'))}"
            )
        note = next((f.get("note") for f in fields if f.get("note")), "")
        if note:
            lines.append(note)
        lines.append("")
        number += 1
    hidden = max(0, len(grouped) - 12)
    if hidden:
        lines.append(f"... and {hidden} more product mismatches.")
        lines.append("")
    for row in (item.get("missing_rows") or [])[:8]:
        lines.append(f"{number}. Missing product: {row.get('product')}")
        lines.append(
            "Expected: "
            f"opening_qty = {_fmt_value(row.get('opening_qty'))}, "
            f"sales_qty = {_fmt_value(row.get('sales_qty'))}, "
            f"closing_qty = {_fmt_value(row.get('closing_qty'))}"
        )
        lines.append("Actual: row not extracted")
        lines.append("")
        number += 1
    for row in (item.get("extra_rows") or [])[:8]:
        lines.append(f"{number}. Extra row: {row.get('product')} ({row.get('kind')})")
        lines.append(
            "Actual: "
            f"opening_qty = {_fmt_value(row.get('opening_qty'))}, "
            f"sales_qty = {_fmt_value(row.get('sales_qty'))}, "
            f"closing_qty = {_fmt_value(row.get('closing_qty'))}"
        )
        lines.append("")
        number += 1
    for row in (item.get("accounting") or [])[:6]:
        lines.append(
            f"{number}. {row.get('product')} fails {row.get('formula')} check: "
            f"closing_qty actual {_fmt_value(row.get('actual_closing'))}, "
            f"formula {_fmt_value(row.get('expected_closing'))}"
        )
        lines.append("")
        number += 1
    return lines


def common_issue(item: Dict[str, Any]) -> str:
    """One shared handler problem for this PDF, so the same failure is not repeated."""
    if item.get("status") == "ERROR":
        return "Extraction raised an exception."
    kinds = {m.get("kind") for m in item.get("mismatches") or []}
    extra_kinds = {r.get("kind") for r in item.get("extra_rows") or []}
    qty_fields = {
        m.get("field")
        for m in (item.get("mismatches") or [])
        if str(m.get("kind") or "").startswith("wrong_")
        and str(m.get("field") or "").endswith("_qty")
    }
    if "sales_qty" in qty_fields and "closing_qty" in qty_fields:
        return "Sales and closing quantities are shifted."
    if qty_fields:
        labels = ", ".join(sorted(qty_fields))
        return f"Quantity columns are shifted ({labels})."
    if extra_kinds & {"header_or_footer_as_product", "footer_or_date_as_product"}:
        return "Header, footer, or date text was parsed as a product."
    if item.get("missing_rows"):
        return "Products printed in the PDF were not extracted."
    if "extra_product" in extra_kinds:
        return "Extra product rows were extracted."
    if "unavailable_field_not_null" in kinds:
        return "Missing value fields were returned as 0 instead of null."
    if item.get("duplicates"):
        return "Duplicate product rows were extracted."
    note_text = " ".join(str(note) for note in (item.get("notes") or [])).lower()
    if "ocr" in note_text or item.get("ocr_review"):
        return "OCR did not read the scanned page reliably."
    return "Field mapping does not match the printed columns."


def _prompt_example(item: Dict[str, Any]) -> List[str]:
    name = Path(str(item.get("file") or "pdf")).name
    lines = [f"{name}:"]
    issue = common_issue(item)
    mismatches = item.get("mismatches") or []

    def from_mismatch(row: dict) -> List[str]:
        product = row.get("product") or ""
        if product:
            lines.append(str(product))
        lines.append(
            f"Expected {row.get('field')} = {_fmt_value(row.get('expected'))}"
        )
        lines.append(
            f"Actual {row.get('field')} = {_fmt_value(row.get('actual'))}"
        )
        return lines

    if "not extracted" in issue:
        missing = next(iter(item.get("missing_rows") or []), None)
        if missing:
            lines.append(str(missing.get("product") or ""))
            lines.append(
                "Expected "
                f"opening_qty = {_fmt_value(missing.get('opening_qty'))}, "
                f"sales_qty = {_fmt_value(missing.get('sales_qty'))}, "
                f"closing_qty = {_fmt_value(missing.get('closing_qty'))}"
            )
            lines.append("Actual: row not extracted")
            return lines
    if "parsed as a product" in issue:
        extra = next(
            (
                row
                for row in (item.get("extra_rows") or [])
                if row.get("kind") in {"header_or_footer_as_product", "footer_or_date_as_product"}
            ),
            None,
        )
        if extra:
            lines.append(str(extra.get("product") or ""))
            lines.append("Expected: not a product row")
            lines.append(
                "Actual "
                f"opening_qty = {_fmt_value(extra.get('opening_qty'))}, "
                f"sales_qty = {_fmt_value(extra.get('sales_qty'))}, "
                f"closing_qty = {_fmt_value(extra.get('closing_qty'))}"
            )
            return lines
    if "shifted" in issue:
        shifted = next(
            (
                row
                for row in mismatches
                if str(row.get("kind") or "").startswith("wrong_")
                and str(row.get("field") or "").endswith("_qty")
            ),
            None,
        )
        if shifted:
            return from_mismatch(shifted)
    if "0 instead of null" in issue:
        missing_value = next(
            (row for row in mismatches if row.get("kind") == "unavailable_field_not_null"),
            None,
        )
        if missing_value:
            return from_mismatch(missing_value)
    if item.get("status") == "ERROR" and item.get("error"):
        lines.append(str(item.get("error")))
        return lines
    mismatch = next((row for row in mismatches if row.get("field")), None)
    if mismatch is not None:
        return from_mismatch(mismatch)
    missing = next(iter(item.get("missing_rows") or []), None)
    if missing:
        lines.append(str(missing.get("product") or ""))
        lines.append(f"Expected sales_qty = {_fmt_value(missing.get('sales_qty'))}")
        lines.append("Actual: row not extracted")
        return lines
    if item.get("error"):
        lines.append(str(item.get("error")))
        return lines
    lines.append("See report.json for the row diff.")
    return lines


def render_fix_prompt(report: Dict[str, Any]) -> str:
    problems = [
        item
        for item in (report.get("files") or [])
        if item.get("status") in {"FAIL", "ERROR"}
    ]
    if not problems:
        return (
            "No extraction failures in this run.\n"
            "REVIEW rows are ambiguous and are listed only in report.html.\n"
            "RATE_LIMITED rows are retried on the next run and are not handler bugs.\n"
        )
    grouped: Dict[tuple, List[dict]] = defaultdict(list)
    for item in problems:
        format_name = str(item.get("format") or "Unknown layout")
        grouped[(format_name, common_issue(item))].append(item)

    parts = [
        "Fix the underlying extraction handlers for the shared problems below.",
        "Do not patch individual PDF filenames.",
        "Do not change column mapping for formats that passed.",
        "Do not invent values that are not printed. Keep 0 and null distinct.",
        "Do not treat footer, date, or header text as product rows.",
        "",
    ]
    for (format_name, issue), items in grouped.items():
        handlers = Counter(
            str(item.get("api_method") or "")
            for item in items
            if item.get("api_method")
        )
        parts.append(f"FORMAT: {format_name}")
        parts.append("")
        parts.append("Affected PDFs:")
        for item in items:
            parts.append(f"- {Path(str(item.get('file') or '')).name}")
        parts.append("")
        parts.append("Common issue:")
        parts.append(issue)
        parts.append("")
        if handlers:
            handler, _count = handlers.most_common(1)[0]
            parts.append("Handler:")
            parts.append(handler)
            parts.append("")
        parts.append("Examples:")
        for item in items[:3]:
            parts.extend(_prompt_example(item))
            parts.append("")
        parts.append(f"Fix the common {format_name} extraction issue.")
        parts.append("Do not patch individual PDF filenames.")
        parts.append("Fix the underlying handler/parser/OCR logic.")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"
