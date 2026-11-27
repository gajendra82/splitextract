"""Extract 142.jpg and dump sales samples."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, stream=sys.stderr)

from services.sales_statement_extractor import extract_sales_statement

path = Path(r"C:\Users\adity\Downloads\142.jpg")
result = extract_sales_statement(path.read_bytes(), path.name)
items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
extra = (result.get("totals") or {}).get("extra") or {}
cmap = extra.get("column_map") or []

slim = {
    "n": len(items),
    "method": extra.get("extraction_method"),
    "layout": extra.get("layout"),
    "kind": extra.get("stock_identity_kind"),
    "totals": {
        k: (result.get("totals") or {}).get(k)
        for k in ("sales_qty", "sales_value", "closing_qty", "closing_value", "opening_value")
    },
    "extra_focus": {
        k: extra.get(k)
        for k in (
            "sales_qty",
            "sales_value",
            "line_sales_qty_sum",
            "line_sales_value_sum",
            "total_row_source",
            "qty_only",
            "no_sales_value",
            "opening_value",
            "purchase_value",
        )
    },
    "column_map": [
        {
            "i": c.get("col_index"),
            "h": c.get("header_text"),
            "c": c.get("canonical"),
            "src": c.get("source"),
        }
        for c in cmap
        if isinstance(c, dict)
    ],
    "samples": [],
    "wrongish": [],
    "first10": [],
}

want = (
    "ARJUNA",
    "LIV 52 DROP",
    "LIV-52",
    "LIV 52",
    "CYSTONE",
    "SEPTILIN",
    "BONNISAN",
)
for it in items:
    name = str(it.get("product_name") or "")
    row = {
        "product_name": name,
        "oq": it.get("opening_qty"),
        "pq": it.get("purchase_qty") or it.get("receipts_qty"),
        "sq": it.get("sales_qty"),
        "sv": it.get("sales_value"),
        "cq": it.get("closing_qty"),
        "cv": it.get("closing_value"),
        "flags": (it.get("extra") or {}).get("flags"),
        "fs": {
            k: v
            for k, v in ((it.get("extra") or {}).get("field_source") or {}).items()
            if k in {"sales_qty", "sales_value", "closing_qty", "opening_qty", "purchase_qty"}
        },
    }
    if any(name.upper().startswith(w) or w in name.upper() for w in want):
        slim["samples"].append(row)
    try:
        sq = float(it.get("sales_qty")) if it.get("sales_qty") not in (None, "") else None
        sv = float(it.get("sales_value")) if it.get("sales_value") not in (None, "") else None
        cq = float(it.get("closing_qty")) if it.get("closing_qty") not in (None, "") else None
    except (TypeError, ValueError):
        sq = sv = cq = None
    if sq is None and (sv is not None or cq is not None):
        slim["wrongish"].append({**row, "why": "missing_sales_qty"})
    elif sq is not None and sv is not None and abs(sq - sv) < 0.01 and abs(sq) >= 1:
        slim["wrongish"].append({**row, "why": "qty_eq_value"})
    elif sq is not None and abs(sq) >= 10000:
        slim["wrongish"].append({**row, "why": "sales_qty_huge"})

for i in items[:10]:
    slim["first10"].append(
        {
            "n": i.get("product_name"),
            "oq": i.get("opening_qty"),
            "pq": i.get("purchase_qty") or i.get("receipts_qty"),
            "sq": i.get("sales_qty"),
            "sv": i.get("sales_value"),
            "cq": i.get("closing_qty"),
            "cv": i.get("closing_value"),
        }
    )

# expected anchors from image description
anchors = {
    "LIV 52 DROP": {"sq": 2026, "cq": 86},
    "CYSTONE TAB": {"sq": 1055, "cq": 88},
    "SEPTILIN TAB": {"sq": 621, "cq": 44},
}
slim["anchor_check"] = []


def _anchor_hit(key: str, exp: dict):
    """Match printed name, OCR near-miss (SEPTELIN), or qty fingerprint."""
    key_u = key.upper()
    # Prefer exact substring on the printed name.
    for it in items:
        name = (it.get("product_name") or "").upper()
        if key_u in name:
            return it
    # SEPTILIN ↔ SEPTELIN OCR typo on phone collage.
    if "SEPTILIN" in key_u:
        for it in items:
            name = (it.get("product_name") or "").upper()
            if "SEPT" in name and abs(float(it.get("sales_qty") or -1) - exp["sq"]) < 0.51:
                return it
    # CYSTONE TAB sometimes mislabeled CYSTONE SYP with correct qtys.
    if "CYSTONE" in key_u:
        for it in items:
            name = (it.get("product_name") or "").upper()
            if "CYSTONE" in name and abs(float(it.get("sales_qty") or -1) - exp["sq"]) < 0.51:
                return it
    return None


for key, exp in anchors.items():
    hit = _anchor_hit(key, exp)
    if not hit:
        slim["anchor_check"].append({"want": key, "status": "MISSING"})
    else:
        slim["anchor_check"].append(
            {
                "want": key,
                "got_name": hit.get("product_name"),
                "sq": hit.get("sales_qty"),
                "cq": hit.get("closing_qty"),
                "sv": hit.get("sales_value"),
                "exp": exp,
                "ok_sq": abs(float(hit.get("sales_qty") or -1) - exp["sq"]) < 0.51,
                "ok_cq": abs(float(hit.get("closing_qty") or -1) - exp["cq"]) < 0.51,
            }
        )

Path("_142_slim.json").write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")
print(json.dumps(slim, indent=2, default=str))
