"""OCR Bal. column digits and compare to extract + printed footer."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytesseract
from PIL import Image, ImageEnhance, ImageOps

ROOT = Path(__file__).resolve().parent
IMG = Path(
    r"C:\Users\amnsa\Downloads\ZL_2026_August"
    r"\0000700077_2026_08_ZL_06_304_06092026141652.jpeg"
)
REPORT = ROOT / "0000700077_extract_report.json"


def main() -> None:
    im = ImageOps.exif_transpose(Image.open(IMG)).convert("L")
    w, h = im.size
    # Right side of table: Sale Val | Bal | Bal Val — take Bal band.
    bal_band = im.crop((int(w * 0.72), int(h * 0.18), int(w * 0.86), int(h * 0.92)))
    bal_band = ImageEnhance.Contrast(bal_band).enhance(2.0)
    bal_band.save(ROOT / "jrshah_bal_band.jpg", quality=95)
    text = pytesseract.image_to_string(bal_band, config="--psm 6")
    nums = [int(x) for x in re.findall(r"\b\d{1,5}\b", text)]
    print("ocr_bal_candidates_count", len(nums))
    print("ocr_bal_sum_all", sum(nums))
    print("ocr_last_20", nums[-20:])

    r = json.loads(REPORT.read_text(encoding="utf-8"))
    bals = [float(i.get("closing_qty") or 0) for i in r["line_items"]]
    print("extract_bal_sum", sum(bals), "n", len(bals))
    print("extract_nonzero_bals", sum(1 for b in bals if b))
    print("printed_footer", 2035)


if __name__ == "__main__":
    main()
