"""Dump word x-centres for header + sample rows."""
from pathlib import Path

import fitz

path = Path(r"C:\Users\adity\Downloads\Stock_Report (2).PDF")
doc = fitz.open(stream=path.read_bytes(), filetype="pdf")
page = doc[0]
words = page.get_text("words")  # x0,y0,x1,y1,word,block,line,wordno
# group by approximate y
rows = {}
for w in words:
    x0, y0, x1, y1, text, *_ = w
    key = round(y0 / 3) * 3
    rows.setdefault(key, []).append((x0, x1, (x0 + x1) / 2, text))

for y in sorted(rows)[:40]:
    toks = sorted(rows[y], key=lambda t: t[0])
    line = " | ".join(f"{t[3]}@{t[2]:.0f}" for t in toks)
    if any(
        k in line.upper()
        for k in (
            "PARTICULAR",
            "AACTARIL",
            "CONFIDO",
            "100.00",
            "79.00",
            "GASEX",
            "OPHTHA",
            "COMPANY",
            "RUMALAYA",
            "VALUE",
            "QNT",
        )
    ) or y < 200:
        print(f"y={y}: {line}")
