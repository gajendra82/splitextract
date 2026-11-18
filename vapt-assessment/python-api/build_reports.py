#!/usr/bin/env python3
"""Generate the Python API SAST, DAST, and summary deliverables."""

from __future__ import annotations

import csv
import html
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    CondPageBreak,
    HRFlowable,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from findings_data import (
    APP_MTIME,
    ASSESSMENT_DATE,
    BRANCH,
    COLLECTION_UTC,
    COVERAGE,
    EVIDENCE,
    FINDINGS,
    HEAD,
    HEAD_DATE,
    HEAD_SUBJECT,
    PROCESS,
    PROCESS_START,
)

ROOT = Path(__file__).resolve().parent
GENERATED = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
NAVY = colors.HexColor("#12324D")
RULE = colors.HexColor("#D5DDE3")
ROW = colors.HexColor("#F4F7F8")
SEV = {
    "Critical": colors.HexColor("#7A1020"),
    "High": colors.HexColor("#9B2C2C"),
    "Medium": colors.HexColor("#8A4B08"),
    "Low": colors.HexColor("#1F4E79"),
    "Informational": colors.HexColor("#3E4C59"),
}
ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Informational": 4}


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def sev_counts(rows):
    counts = Counter(row["severity"] for row in rows)
    return {name: counts.get(name, 0) for name in ORDER}


def status_counts(rows):
    return Counter(row["status"] for row in rows)


def sast_rows():
    return [row for row in FINDINGS if row["sast_id"]]


def dast_rows():
    return [row for row in FINDINGS if row["dast_id"]]


def sorted_rows(rows):
    return sorted(rows, key=lambda row: (ORDER[row["severity"]], row["canonical_id"]))


STYLES = getSampleStyleSheet()
BODY = ParagraphStyle(
    "Body", parent=STYLES["Normal"], fontName="Times-Roman", fontSize=10,
    leading=13, alignment=TA_JUSTIFY, textColor=colors.HexColor("#1C1C1C"), spaceAfter=6,
)
H1 = ParagraphStyle(
    "H1", parent=STYLES["Heading1"], fontName="Times-Bold", fontSize=16,
    leading=19, textColor=NAVY, spaceBefore=8, spaceAfter=6,
)
H2 = ParagraphStyle(
    "H2", parent=STYLES["Heading2"], fontName="Times-Bold", fontSize=13,
    leading=16, textColor=NAVY, spaceBefore=10, spaceAfter=4,
)
H3 = ParagraphStyle(
    "H3", parent=STYLES["Heading3"], fontName="Times-Bold", fontSize=11,
    leading=14, textColor=colors.HexColor("#1C1C1C"), spaceBefore=8, spaceAfter=3,
)
SMALL = ParagraphStyle(
    "Small", parent=BODY, fontSize=8.5, leading=11, alignment=TA_LEFT, spaceAfter=0,
)
TH = ParagraphStyle(
    "TH", parent=SMALL, fontName="Times-Bold", textColor=colors.white, fontSize=8, leading=10,
)
CELL = ParagraphStyle(
    "Cell", parent=SMALL, fontName="Times-Roman", textColor=colors.HexColor("#1C1C1C"),
)
LABEL = ParagraphStyle(
    "Label", parent=SMALL, fontName="Times-Bold", textColor=NAVY,
)
COVER = ParagraphStyle(
    "Cover", parent=STYLES["Title"], fontName="Times-Bold", fontSize=22,
    leading=26, textColor=NAVY, alignment=TA_LEFT, spaceAfter=8,
)


def P(text, style=BODY):
    return Paragraph(str(text), style)


def kv_table(pairs):
    data = [[P(k, LABEL), P(v, CELL)] for k, v in pairs]
    table = Table(data, colWidths=[42 * mm, 128 * mm], repeatRows=0)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#E7EEF2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.3, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def grid(headers, rows, widths):
    head = [P(h, TH) for h in headers]
    body = [[P(col, CELL) for col in row] for row in rows]
    table = Table([head] + body, colWidths=widths, repeatRows=1)
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.3, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("FONTNAME", (0, 0), (-1, 0), "Times-Bold"),
    ]
    for index in range(1, len(body) + 1):
        if index % 2 == 0:
            commands.append(("BACKGROUND", (0, index), (-1, index), ROW))
    table.setStyle(TableStyle(commands))
    return table


def paint_page(canvas, doc):
    canvas.saveState()
    width, height = A4
    canvas.setFillColor(NAVY)
    canvas.rect(0, height - 28, width, 28, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Times-Bold", 8)
    canvas.drawString(16 * mm, height - 18, f"CONFIDENTIAL  |  Python API VAPT  |  {doc.report_label}")
    canvas.rect(0, 0, width, 24, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Times-Roman", 8)
    canvas.drawString(16 * mm, 9, "Sales POD extraction service  |  22 October 2026")
    canvas.drawRightString(width - 16 * mm, 9, f"Page {doc.page}")
    canvas.restoreState()


def bullets(items):
    flow = []
    for item in items:
        flow.append(P("•  " + item, SMALL))
        flow.append(Spacer(1, 2))
    return flow


def finding_story(row, id_key, evidence_key):
    story = [CondPageBreak(55 * mm), P(f"{row[id_key]}  {row['title']}", H2)]
    badge = row["severity"]
    story.append(kv_table([
        ("Canonical ID", row["canonical_id"]),
        ("Severity", badge),
        ("CVSS", f"{row['cvss']}  {row['vector']}"),
        ("Status", row["status"]),
        ("SAST result", row["sast_status"]),
        ("DAST result", row["dast_status"]),
        ("Confidence", row["confidence"]),
        ("CWE / OWASP", f"{row['cwe']}. {row['owasp']}"),
        ("Component", row["component"]),
        ("Location", row["location"]),
    ]))
    story.append(Spacer(1, 4))
    for label, key in (
        ("Description", "description"),
        ("Root cause", "root_cause"),
        ("Preconditions", "preconditions"),
        ("Impact", "impact"),
        ("Evidence", evidence_key),
        ("Remediation", "recommendation"),
        ("Verification", "verification"),
        ("Limitations", "limitations"),
    ):
        story.append(P(label, H3))
        story.append(P(row[key]))
    story.append(P("Evidence IDs: " + ", ".join(row["evidence_ids"]), SMALL))
    story.append(Spacer(1, 6))
    return story


def common_context():
    return [
        ("Service", "FastAPI application app:app, started by runner.py"),
        ("Framework", "FastAPI 0.115.8, Starlette 0.45.3, Uvicorn 0.34.0, Python 3.12.3"),
        ("Listen", "0.0.0.0:8001, proxied by nginx at https://zydus-py.mediola.in/extract/"),
        ("Process", PROCESS),
        ("Process start", PROCESS_START + " UTC"),
        ("Branch / HEAD", f"{BRANCH} / {HEAD}"),
        ("HEAD commit", f"{HEAD_DATE}  {HEAD_SUBJECT}"),
        ("Working tree", f"app.py differs from HEAD. File mtime {APP_MTIME} UTC, which is before the process start, so the running process loaded this working tree."),
        ("Assessment added", "Files under vapt-assessment/python-api/ only. Application code was not modified."),
        ("Collection start", COLLECTION_UTC),
        ("Report generated", GENERATED),
    ]


def methodology():
    return [
        "Source review of app.py, runner.py, services/, requirements.txt, Dockerfile, docker-compose.yml, and the live systemd unit.",
        "Bandit 1.9.4 over app.py, runner.py, and services/. 197 results: 0 high, 2 medium, 195 low. The two medium results are the all-interface bind (CAN-PY-005). Low results were triaged: 187 are try/except/pass or continue, 7 are regex or column-label false positives from the hardcoded-password check, and 1 is random.uniform used only for retry jitter. Those low results are not findings.",
        "pip-audit 2.10.1 against the production site-packages. A first attempt to re-resolve the freeze file failed because cffi==2.0.0 could not be downloaded. That attempt installed nothing. The successful attempt used --path and did not upgrade packages.",
        "Safe local HTTP checks with curl: GET of health and documentation, empty POSTs with no file and no URL, one CORS preflight, nginx header inspection through a loopback resolve, and one cleartext health GET to this host's own address. No document was uploaded. No URL was fetched by the application. No Azure object was read or written.",
        "openssl for certificate dates and one TLS 1.2 handshake. ss, ps, stat, ufw, and iptables for the listener and file modes.",
        "Not executed: OWASP ZAP, Burp Suite, nmap, Nikto, Nuclei, Semgrep, and any load, brute-force, or parser-exploit test.",
    ]


def limitations():
    return [
        "The target is the live extraction process. Destructive, high-volume, and data-plane tests were not run.",
        "No application account exists, so role and cross-account tests could not be performed. Object-level storage access was not executed.",
        "Server-side URL fetch was not exercised. Cloud-metadata and internal-network destinations were not requested.",
        "No synthetic or customer file was submitted, so MIME confusion, parser crashes, and processing timeouts were not measured at runtime.",
        "Rate-limit effectiveness and off-box Internet exposure of port 8001 were not measured. The port answered on this host's own primary address, and the host firewall does not filter it.",
        "Port 8000 is a separate listening process and was not assessed. nginx also maps ports 8002 and 8003; those ports were not listening.",
        "The Laravel application was out of scope. A prior Python assessment dated 22 September 2026 was not treated as retest evidence.",
        "Dependency advisories were not exploited and were not each converted into a vulnerability.",
    ]


def roadmap():
    return [
        "1. Authenticate every extraction route and restrict nginx /extract/ to the Laravel servers. Disable public API documentation.",
        "2. Remove arbitrary URL fetch, or allow only named HTTPS storage hosts, and stop returning internal exception text.",
        "3. Ignore client container names and client blob prefixes. Stop returning an account-key SAS to anonymous callers.",
        "4. Change /var/www/html/.env to mode 600 and rotate the credentials that were world-readable.",
        "5. Bind the API to 127.0.0.1 so TLS and the nginx body limit cannot be bypassed.",
        "6. Remove in-flight file names and batch identifiers from unauthenticated health output.",
        "7. Replace reflected CORS with an explicit origin list. Add HSTS and hide server versions.",
        "8. Enforce one upload-size cap and one queue on every extraction route. Remove or protect /test-extract.",
        "9. Run the service as an unprivileged user. Align the Dockerfile with runner.py.",
        "10. Patch pillow, python-multipart, starlette, and the PDF libraries in a test environment, then retest extraction.",
    ]


def build_pdf(path: Path, label: str, rows, id_key: str, evidence_key: str, intro: list[str]):
    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=16 * mm, bottomMargin=14 * mm,
        title=label, author="Internal VAPT assessment",
    )
    doc.report_label = label
    story = []
    story.append(Spacer(1, 18 * mm))
    story.append(P("CONFIDENTIAL", H1))
    story.append(P(label, COVER))
    story.append(P(
        "Sales POD Invoice Intelligence Platform<br/>Python data-extraction service",
        H2,
    ))
    story.append(P(f"Assessment date: {ASSESSMENT_DATE}<br/>Generated: {GENERATED}", BODY))
    story.append(P(
        "Prepared for the client IT Security Team. This document reports only checks that were completed. "
        "It does not state that the service is free of vulnerabilities.",
        BODY,
    ))
    story.append(PageBreak())

    story.append(P("1. Executive summary", H1))
    for paragraph in intro:
        story.append(P(paragraph))
    counts = sev_counts(rows)
    story.append(P("Findings in this report", H2))
    story.append(grid(
        ["Severity", "Count"],
        [[name, str(counts[name])] for name in ORDER],
        [80 * mm, 40 * mm],
    ))
    story.append(Spacer(1, 4))
    statuses = status_counts(rows)
    story.append(P(
        f"Records in this report: {len(rows)}. Open: {statuses.get('Open', 0)}. "
        "Remediated: 0. No finding is closed.",
        BODY,
    ))

    story.append(P("2. Application and scope", H1))
    story.append(kv_table(common_context()))
    story.append(Spacer(1, 6))
    story.append(P(
        "In scope: the FastAPI service in this directory and the nginx /extract/ location that proxies to it. "
        "Out of scope: the Laravel application, the process on port 8000, and the nginx locations aimed at ports 8002 and 8003.",
        BODY,
    ))

    story.append(P("3. Methodology and tools", H1))
    story.extend(bullets(methodology()))
    story.append(P("4. Coverage", H1))
    story.append(grid(
        ["Area", "Result", "Finding"],
        [[a, c, d or "—"] for a, b, c, d, e in COVERAGE],
        [62 * mm, 78 * mm, 30 * mm],
    ))

    story.append(P("5. Findings", H1))
    story.append(grid(
        ["ID", "Severity", "CVSS", "Status", "Title"],
        [[row[id_key], row["severity"], row["cvss"], row["status"], row["title"]] for row in sorted_rows(rows)],
        [28 * mm, 24 * mm, 22 * mm, 22 * mm, 74 * mm],
    ))
    for row in sorted_rows(rows):
        story.extend(finding_story(row, id_key, evidence_key))

    story.append(P("6. Remediation order", H1))
    story.extend(bullets(roadmap()))
    story.append(P("7. Retest", H1))
    story.append(P(
        "No remediation was applied during this assessment, and no retest was passed. "
        "Every record remains Open. A code change by itself would not close a runtime item.",
        BODY,
    ))
    story.append(P("8. Limitations", H1))
    story.extend(bullets(limitations()))
    story.append(P("9. Disclaimer", H1))
    story.append(P(
        "The assessment is a point-in-time review of the source on disk and of the non-destructive checks listed above. "
        "Absence of a finding in an area means that area was reviewed or marked untested. It does not mean the area is secure. "
        "Scanner output was triaged before it was accepted as a finding.",
        BODY,
    ))
    doc.build(story, onFirstPage=paint_page, onLaterPages=paint_page)


def html_page(title, body):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{esc(title)}</title>
<style>
  @page {{ margin: 16mm 14mm 18mm 14mm; }}
  body {{ font-family: "Times New Roman", Times, serif; color: #1c1c1c; margin: 0; background: #f3f5f6; }}
  header.banner {{ background: #12324d; color: #fff; padding: 10px 28px; font-size: 13px; letter-spacing: .02em; }}
  main {{ max-width: 920px; margin: 0 auto; background: #fff; padding: 28px 36px 48px; }}
  h1 {{ color: #12324d; font-size: 22px; margin: 1.2em 0 .4em; page-break-after: avoid; }}
  h2 {{ color: #12324d; font-size: 16px; margin: 1.1em 0 .3em; page-break-after: avoid; }}
  h3 {{ font-size: 13px; margin: .8em 0 .2em; page-break-after: avoid; }}
  p, li {{ font-size: 12.5px; line-height: 1.45; }}
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0 16px; page-break-inside: auto; }}
  th {{ background: #12324d; color: #fff; text-align: left; font-size: 12px; padding: 6px 8px; }}
  td {{ border: 1px solid #d5dde3; padding: 6px 8px; vertical-align: top; font-size: 12px; }}
  tr {{ page-break-inside: avoid; }}
  tbody tr:nth-child(even) td {{ background: #f4f7f8; }}
  .sev {{ color: #fff; font-weight: bold; padding: 2px 6px; }}
  .Critical {{ background: #7a1020; }} .High {{ background: #9b2c2c; }}
  .Medium {{ background: #8a4b08; }} .Low {{ background: #1f4e79; }}
  .Informational {{ background: #3e4c59; }}
  .finding {{ border-top: 3px solid #12324d; padding-top: 8px; page-break-inside: avoid; }}
  footer {{ max-width: 920px; margin: 0 auto; color: #12324d; font-size: 12px; padding: 8px 36px 24px; }}
</style>
</head>
<body>
<header class="banner">CONFIDENTIAL &nbsp;|&nbsp; Python API VAPT &nbsp;|&nbsp; {esc(title)}</header>
<main>
{body}
</main>
<footer>Sales POD extraction service &nbsp;|&nbsp; {esc(ASSESSMENT_DATE)} &nbsp;|&nbsp; Generated {esc(GENERATED)}</footer>
</body>
</html>
"""


def html_table(headers, rows):
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{cell if isinstance(cell, str) and cell.startswith('<') else esc(cell)}</td>" for cell in row) + "</tr>")
    return "<table><thead><tr>" + head + "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"


def sev_html(name):
    return f'<span class="sev {esc(name)}">{esc(name)}</span>'


def build_html(path: Path, title: str, rows, id_key: str, evidence_key: str, intro: list[str]):
    counts = sev_counts(rows)
    parts = [
        f"<h1>{esc(title)}</h1>",
        f"<p>Assessment date: {esc(ASSESSMENT_DATE)}. Generated: {esc(GENERATED)}. "
        "Prepared for the client IT Security Team. This report does not state that the service is secure.</p>",
        "<h1>1. Executive summary</h1>",
    ]
    parts.extend(f"<p>{esc(p)}</p>" for p in intro)
    parts.append("<h2>Findings in this report</h2>")
    parts.append(html_table(["Severity", "Count"], [[name, counts[name]] for name in ORDER]))
    statuses = status_counts(rows)
    parts.append(f"<p>Records: {len(rows)}. Open: {statuses.get('Open', 0)}. Remediated: 0.</p>")
    parts.append("<h1>2. Application and scope</h1>")
    parts.append(html_table(["Item", "Detail"], common_context()))
    parts.append("<p>In scope: this FastAPI service and nginx location /extract/. Out of scope: Laravel, port 8000, and the locations aimed at ports 8002 and 8003.</p>")
    parts.append("<h1>3. Methodology and tools</h1><ul>")
    parts.extend(f"<li>{esc(item)}</li>" for item in methodology())
    parts.append("</ul><h1>4. Coverage</h1>")
    parts.append(html_table(
        ["Area", "Method", "Result", "Finding"],
        [[a, b, c, d or "—"] for a, b, c, d, e in COVERAGE],
    ))
    parts.append("<h1>5. Findings summary</h1>")
    parts.append(html_table(
        ["ID", "Severity", "CVSS", "Status", "Title"],
        [[row[id_key], sev_html(row["severity"]), row["cvss"], row["status"], row["title"]] for row in sorted_rows(rows)],
    ))
    parts.append("<h1>6. Detailed findings</h1>")
    for row in sorted_rows(rows):
        parts.append(f'<section class="finding"><h2>{esc(row[id_key])} {esc(row["title"])}</h2>')
        parts.append(html_table(["Field", "Detail"], [
            ("Canonical ID", row["canonical_id"]),
            ("Severity", sev_html(row["severity"])),
            ("CVSS", f'{row["cvss"]} {row["vector"]}'),
            ("Status", row["status"]),
            ("SAST result", row["sast_status"]),
            ("DAST result", row["dast_status"]),
            ("Confidence", row["confidence"]),
            ("CWE / OWASP", f'{row["cwe"]}. {row["owasp"]}'),
            ("Component", row["component"]),
            ("Location", row["location"]),
        ]))
        for label, key in (
            ("Description", "description"),
            ("Root cause", "root_cause"),
            ("Preconditions", "preconditions"),
            ("Impact", "impact"),
            ("Evidence", evidence_key),
            ("Remediation", "recommendation"),
            ("Verification", "verification"),
            ("Limitations", "limitations"),
        ):
            parts.append(f"<h3>{esc(label)}</h3><p>{esc(row[key])}</p>")
        parts.append(f"<p>Evidence IDs: {esc(', '.join(row['evidence_ids']))}</p></section>")
    parts.append("<h1>7. Remediation order</h1><ul>")
    parts.extend(f"<li>{esc(item)}</li>" for item in roadmap())
    parts.append("</ul><h1>8. Retest</h1>")
    parts.append("<p>No remediation was applied and no retest was passed. Every record remains Open.</p>")
    parts.append("<h1>9. Limitations</h1><ul>")
    parts.extend(f"<li>{esc(item)}</li>" for item in limitations())
    parts.append("</ul><h1>10. Disclaimer</h1>")
    parts.append("<p>This is a point-in-time review. An area with no finding was either reviewed or marked untested. Scanner alerts were triaged before acceptance.</p>")
    path.write_text(html_page(title, "\n".join(parts)), encoding="utf-8")


def write_csv(path: Path, rows, id_key):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "id", "canonical_id", "title", "severity", "cvss", "vector", "status",
            "sast_status", "dast_status", "confidence", "cwe", "component", "location",
        ])
        writer.writeheader()
        for row in sorted_rows(rows):
            writer.writerow({
                "id": row[id_key],
                "canonical_id": row["canonical_id"],
                "title": row["title"],
                "severity": row["severity"],
                "cvss": row["cvss"],
                "vector": row["vector"],
                "status": row["status"],
                "sast_status": row["sast_status"],
                "dast_status": row["dast_status"],
                "confidence": row["confidence"],
                "cwe": row["cwe"],
                "component": row["component"],
                "location": row["location"],
            })


def write_json(path: Path, kind: str, rows, id_key):
    payload = {
        "report": kind,
        "classification": "CONFIDENTIAL",
        "assessment_date": ASSESSMENT_DATE,
        "generated_utc": GENERATED,
        "head": HEAD,
        "branch": BRANCH,
        "working_tree_app_mtime_utc": APP_MTIME,
        "process_start_utc": PROCESS_START,
        "severity_counts": sev_counts(rows),
        "status_counts": dict(status_counts(rows)),
        "remediated": 0,
        "findings": [],
    }
    for row in sorted_rows(rows):
        item = dict(row)
        item["id"] = row[id_key]
        payload["findings"].append(item)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_registers():
    with (ROOT / "Python_API_Findings_Crosswalk.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["canonical_id", "sast_id", "dast_id", "title", "severity", "cvss", "status", "sast_status", "dast_status"])
        for row in sorted_rows(FINDINGS):
            writer.writerow([
                row["canonical_id"], row["sast_id"], row["dast_id"], row["title"],
                row["severity"], row["cvss"], row["status"], row["sast_status"], row["dast_status"],
            ])
    with (ROOT / "Python_API_Coverage_Matrix.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["area", "method", "result", "finding_ids", "notes"])
        writer.writerows(COVERAGE)
    with (ROOT / "Python_API_Evidence_Index.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["evidence_id", "artifact", "finding_ids", "collected_utc", "method", "sanitization"])
        writer.writerows(EVIDENCE)
    with (ROOT / "Python_API_Retest_Tracker.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["canonical_id", "status", "retest_result", "notes"])
        for row in sorted_rows(FINDINGS):
            writer.writerow([row["canonical_id"], row["status"], "Not retested", "No remediation was applied in this assessment."])


def write_markdown():
    counts = sev_counts(FINDINGS)
    lines = [
        "# Python API VAPT summary",
        "",
        f"Classification: CONFIDENTIAL. Assessment date: {ASSESSMENT_DATE}. Generated: {GENERATED}.",
        "",
        "The service is a FastAPI application on port 8001. Thirteen unique findings are open. "
        "None are Critical or High. Six are Medium. No remediation was verified.",
        "",
        "## Counts",
        "",
        "| Severity | Unique findings |",
        "| --- | --- |",
    ]
    for name in ORDER:
        lines.append(f"| {name} | {counts[name]} |")
    lines += [
        "",
        f"Total unique: {len(FINDINGS)}. Open: {len(FINDINGS)}. Remediated: 0.",
        "",
        "## Findings",
        "",
        "| ID | Severity | Status | Title |",
        "| --- | --- | --- | --- |",
    ]
    for row in sorted_rows(FINDINGS):
        lines.append(f"| {row['canonical_id']} | {row['severity']} | {row['status']} | {row['title']} |")
    lines += ["", "## Remediation order", ""]
    lines.extend(f"- {item}" for item in roadmap())
    lines += ["", "## Limitations", ""]
    lines.extend(f"- {item}" for item in limitations())
    lines.append("")
    (ROOT / "Python_API_VAPT_Summary.md").write_text("\n".join(lines), encoding="utf-8")

    log = [
        "# Python API VAPT execution log",
        "",
        f"Generated: {GENERATED}",
        "",
        "## Inventory",
        "",
        f"- Repository: /var/www/html/split-extract",
        f"- Branch: {BRANCH}",
        f"- HEAD: {HEAD} ({HEAD_DATE}) {HEAD_SUBJECT}",
        f"- Working tree: app.py and several test files differ from HEAD. app.py mtime {APP_MTIME} UTC.",
        f"- Diff stat versus HEAD for app.py was recorded as 4410 insertions and 184 deletions. The diff body was not copied into the report.",
        f"- Running process: {PROCESS}, started {PROCESS_START} UTC, after the app.py mtime.",
        "- Framework confirmed from source and the live OpenAPI title: FastAPI. Not Flask or Django.",
        "- Routes from live OpenAPI: GET /, /health, /live, /ready; POST /split-and-extract, /test-extract, /extract-sales-statement, /extract-pod-grn. Documentation routes /docs, /redoc, /openapi.json also returned HTTP 200.",
        "- Two Python functions are decorated as POST /extract-sales-statement. The live OpenAPI description matches the later function.",
        "- Dependencies: requirements.txt uses minimum versions. The running virtualenv freeze is scans/venv_freeze.txt.",
        "- No SQL, ORM, os.system, shell=True, eval, exec, or pickle use was found in the application Python.",
        "- Outbound HTTP: requests.get of caller-supplied split_raw_url, and requests.post from services/internal_invoice_ai.py to a configured model URL with an optional bearer token.",
        "- Live unit: /etc/systemd/system/split-extract.service, User=root, EnvironmentFile=/var/www/html/.env, ExecStart runner.py. The copy under deploy/systemd/ is not the loaded unit.",
        "- nginx location /extract/ proxies to 127.0.0.1:8001 with client_max_body_size 500M. Port 8000 is a different listener. Ports 8002 and 8003 were not listening.",
        "",
        "## Commands and tools",
        "",
        "- Bandit 1.9.4. JSON: scans/bandit.json (medium and higher) and scans/bandit_all.json.",
        "- pip-audit 2.10.1. Failed attempt: requirement reinstall of venv_freeze.txt stopped on cffi==2.0.0 and installed nothing. Successful attempt: --path site-packages. Result: 109 records, 70 unique identifiers, 12 packages. Exit status 1 because advisories were found.",
        "- curl to 127.0.0.1:8001 and to nginx through --resolve zydus-py.mediola.in:443:127.0.0.1. One health GET to 148.230.66.182:8001.",
        "- openssl s_client and x509. ss, ps, stat, ufw, iptables -S INPUT.",
        "- Python ast parsing was used as a syntax check and does not write bytecode.",
        "- Not installed or not run: OWASP ZAP, Semgrep, nmap, Burp Suite.",
        "",
        "## Tests not performed",
        "",
    ]
    log.extend(f"- {item}" for item in limitations())
    log += [
        "",
        "## Application code",
        "",
        "This assessment wrote files only under vapt-assessment/python-api/. It did not change application behavior and did not restart the service.",
        "",
    ]
    (ROOT / "Python_API_Execution_Log.md").write_text("\n".join(log), encoding="utf-8")


def secret_scan(paths):
    patterns = [
        re.compile(r"AccountKey=", re.I),
        re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
        re.compile(r"AIza[0-9A-Za-z\-_]{20,}"),
        re.compile(r"sk-[A-Za-z0-9]{20,}"),
        re.compile(r"eyJ[A-Za-z0-9_\-]{30,}"),
    ]
    problems = []
    for path in paths:
        if not path.is_file():
            problems.append(f"missing {path.name}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in patterns:
            if pattern.search(text):
                problems.append(f"{path.name} matched {pattern.pattern}")
    return problems


SAST_INTRO = [
    "Static review of the FastAPI extraction service found no Critical or High source defect that was demonstrated as remotely exploitable code execution, SQL injection, or command injection. The priority source defects are missing authentication, an unauthenticated URL fetch, and caller-controlled blob paths used with the service storage credential.",
    "Six findings in the full register are Medium. This SAST report includes every unique finding because each one is visible in source, configuration, or the dependency scan. Runtime proof is recorded separately and is not invented where a live test was not performed.",
    "Bandit reported 197 results. After triage, only the all-interface bind was accepted, and it is the same issue as the cleartext listener. The dependency scanner reported 70 unique advisory identifiers. Those identifiers are one informational finding, not 70 confirmed vulnerabilities.",
]

DAST_INTRO = [
    "Dynamic testing was limited to non-destructive requests against the live process: unauthenticated access, CORS, headers, TLS, the cleartext listener, and health output. No file was uploaded, no URL was submitted for the server to fetch, and no Azure object was touched.",
    "Runtime testing confirmed missing authentication, reflected CORS, missing browser headers, version disclosure, a cleartext listener on this host, world-readable credentials not being served over HTTP, and in-flight document identifiers on /health. The values of those identifiers were redacted.",
    "URL fetch, blob access, and resource exhaustion were not tested. Those items stay open on source evidence and are marked not performed in this DAST report. They are not described as proven runtime compromises.",
]

SUMMARY_INTRO = [
    "This summary consolidates the static and dynamic reviews of one Python service: the FastAPI extraction API in this directory. Thirteen unique findings are open. None are Critical or High. Six are Medium.",
    "The Medium items that were observed at runtime are missing authentication, a world-readable secrets file, a cleartext listener, and health output that included an in-flight document identity. The Medium items confirmed in source but not executed are the URL fetch and the caller-controlled blob path.",
    "No remediation was applied. Nothing in this assessment is marked retest-passed. The service is not declared secure.",
]


def main():
    specs = [
        ("SAST", "Python API SAST Report", sast_rows(), "sast_id", "sast_evidence", SAST_INTRO,
         "Python_API_SAST_Report", "Python_API_SAST_Findings"),
        ("DAST", "Python API DAST Report", dast_rows(), "dast_id", "dast_evidence", DAST_INTRO,
         "Python_API_DAST_Report", "Python_API_DAST_Findings"),
        ("Summary", "Python API VAPT Summary", FINDINGS, "canonical_id", "sast_evidence", SUMMARY_INTRO,
         "Python_API_VAPT_Summary", None),
    ]
    outputs = []
    for kind, title, rows, id_key, evidence_key, intro, stem, register in specs:
        pdf = ROOT / f"{stem}.pdf"
        html_path = ROOT / f"{stem}.html"
        build_pdf(pdf, title, rows, id_key, evidence_key, intro)
        build_html(html_path, title, rows, id_key, evidence_key, intro)
        outputs.extend([pdf, html_path])
        if register:
            csv_path = ROOT / f"{register}.csv"
            json_path = ROOT / f"{register}.json"
            write_csv(csv_path, rows, id_key)
            write_json(json_path, kind, rows, id_key)
            outputs.extend([csv_path, json_path])
    write_registers()
    write_markdown()
    outputs.extend([
        ROOT / "Python_API_Findings_Crosswalk.csv",
        ROOT / "Python_API_Coverage_Matrix.csv",
        ROOT / "Python_API_Evidence_Index.csv",
        ROOT / "Python_API_Retest_Tracker.csv",
        ROOT / "Python_API_VAPT_Summary.md",
        ROOT / "Python_API_Execution_Log.md",
    ])
    problems = secret_scan(outputs)
    # Summary PDF uses sast evidence text; also scan that the DAST report does not contain secret assignments.
    if problems:
        raise SystemExit("Secret scan failed: " + "; ".join(problems))
    print("generated", len(outputs), "files")
    for path in outputs:
        print(f"{path.name}\t{path.stat().st_size}")


if __name__ == "__main__":
    main()
