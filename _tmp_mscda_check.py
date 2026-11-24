import fitz
from services.sales_statement_extractor import _parse_medivision_op_wsale_from_doc

doc = fitz.open(r"C:\Users\adity\Downloads\MSCDA OCT 26.pdf")
r = _parse_medivision_op_wsale_from_doc(doc, "MSCDA.pdf")
assert r
print(
    "rows",
    len(r["line_items"]),
    "sv",
    r["totals"].get("sales_value"),
    "cv",
    r["totals"].get("closing_value"),
)
for it in r["line_items"][:12]:
    side = {
        k: v
        for k, v in (it.get("extra") or {}).items()
        if "sale" in k or "whole" in k
    }
    print(
        it.get("product_name"),
        "op",
        it.get("opening_qty"),
        "pq",
        it.get("receipts_qty"),
        "sq",
        it.get("sales_qty"),
        "sv",
        it.get("sales_value"),
        "cq",
        it.get("closing_qty"),
        "cv",
        it.get("closing_value"),
        "side",
        side,
    )
bad = 0
for it in r["line_items"]:
    o = float(it.get("opening_qty") or 0)
    p = float(it.get("receipts_qty") or 0)
    s = float(it.get("sales_qty") or 0)
    c = float(it.get("closing_qty") or 0)
    if abs((o + p - s) - c) > 0.05 and abs(o) + abs(p) + abs(s) + abs(c) > 0:
        bad += 1
        if bad <= 10:
            print("BAD", it.get("product_name"), o, p, s, c, "expect", o + p - s)
print("bad_identity", bad, "of", len(r["line_items"]))
