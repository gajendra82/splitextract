"""Investigate JR SHAH remaining discrepancies against source crops."""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps

ROOT = Path(__file__).resolve().parent
IMG = Path(
    r"C:\Users\amnsa\Downloads\ZL_2026_August"
    r"\0000700077_2026_08_ZL_06_304_06092026141652.jpeg"
)
REPORT = ROOT / "0000700077_extract_report.json"


def main() -> None:
    im = ImageOps.exif_transpose(Image.open(IMG)).convert("RGB")
    w, h = im.size
    print("size", w, h)
    for label, box in [
        ("header", (0, 0, w, int(h * 0.18))),
        ("mid", (0, int(h * 0.35), w, int(h * 0.62))),
        ("lower", (0, int(h * 0.55), w, int(h * 0.82))),
        ("footer", (0, int(h * 0.82), w, h)),
    ]:
        crop = ImageEnhance.Contrast(im.crop(box)).enhance(1.6)
        out = ROOT / f"jrshah_crop_{label}.jpg"
        crop.save(out, quality=95)
        print("wrote", out.name, crop.size)

    r = json.loads(REPORT.read_text(encoding="utf-8"))
    print("\nRows with SP/SS or identity delta:")
    cq = 0.0
    for it in r["line_items"]:
        o = float(it.get("opening_qty") or 0)
        rq = float(it.get("receipts_qty") or 0)
        s = float(it.get("sales_qty") or 0)
        c = float(it.get("closing_qty") or 0)
        cq += c
        ex = it.get("extra") or {}
        sp = float(ex.get("purchase_scheme_qty") or 0)
        ss = float(ex.get("sales_scheme_qty") or 0)
        delta = (o + rq - s) - c
        holds_as_ss = abs((o + rq - s - sp) - c) < 0.01
        if sp or ss or abs(delta) > 0.01:
            print(
                f"{it.get('product_name')}: op={o} pur={rq} sale={s} bal={c} "
                f"SP={sp} SS={ss} delta={delta} holds_if_SP_as_SS={holds_as_ss}"
            )
    print("closing_qty_sum", cq)
    print(
        "footer_closing_qty",
        (r.get("totals") or {}).get("extra", {}).get("closing_qty"),
    )


if __name__ == "__main__":
    main()
