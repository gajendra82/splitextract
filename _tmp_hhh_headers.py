import io
import re
from pathlib import Path

import pdfplumber

from services.stock_native_resolver import _row_label, _row_numbers
from services.stock_native_tables import from_pdf_words, resolver_ready

p = Path(r"C:\Users\adity\Downloads\HHH.pdf")
with pdfplumber.open(io.BytesIO(p.read_bytes())) as pdf:
    built = from_pdf_words(pdf.pages[0])

h = built.get("header_cells") or []
ready = resolver_ready(h)
print("resolved", ready.get("resolved"))
for c in ready.get("columns") or []:
    print(c.get("col_index"), repr(c.get("header_text")), "->", c.get("canonical"))
print("n_rows", len(built.get("data_rows") or []))
for raw in (built.get("data_rows") or [])[:5]:
    print("row", _row_label(raw)[:50], "nums", _row_numbers(raw)[:12])
for raw in built.get("data_rows") or []:
    lab = _row_label(raw)
    if re.search(r"(?i)Value in|Total Quantity|Quantity", lab):
        print("FOOTER", lab, _row_numbers(raw))
        break