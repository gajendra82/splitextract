from pathlib import Path

import fitz
import pytesseract
from PIL import Image, ImageEnhance, ImageOps, ImageFilter

from services.sales_statement_extractor import _a2z_tesseract

_a2z_tesseract()
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "phase4" / "residual_crops"
BASE = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
doc = fitz.open(BASE / "0000700275_2026_08_ZL_18_374_04092026171615.pdf")
page = doc.load_page(0)
pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), alpha=False)
path = OUT / "0275_x3.png"
pix.save(path)
doc.close()
img = Image.open(path).convert("RGB")
w, h = img.size
data = pytesseract.image_to_data(
    img, output_type=pytesseract.Output.DICT, config="--psm 6"
)
# Find 100ML near LIV
cands = []
for i, txt in enumerate(data["text"]):
    t = (txt or "").strip()
    if not t:
        continue
    if t.upper() in {"100ML", "37", "162", "72", "35", "241"} or "LIV" in t.upper():
        cands.append(
            (
                t,
                data["left"][i],
                data["top"][i],
                data["width"][i],
                data["height"][i],
                data["conf"][i],
            )
        )
        print("tok", cands[-1])

# Around first LIV-52 + 100ML cluster (~y 1070 at 2.5x → ~1284 at 3x)
for y0 in (1050, 1200, 1250, 1280):
    band = img.crop((0, y0, w, y0 + 80))
    band.save(OUT / f"0275_band_{y0}.jpg")
    print(
        "band",
        y0,
        repr(pytesseract.image_to_string(band, config="--psm 6"))[:200],
    )

# Try removing blue ink (strike/handwriting) then OCR
import numpy as np

arr = np.array(img)
# blue-ish pixels: B channel high relative to R
blue = (arr[:, :, 2].astype(int) > arr[:, :, 0].astype(int) + 25) & (
    arr[:, :, 2].astype(int) > arr[:, :, 1].astype(int) + 15
)
cleaned = arr.copy()
cleaned[blue] = [255, 255, 255]
Image.fromarray(cleaned).crop((0, 1250, w, 1350)).save(OUT / "0275_noblue.jpg")
noblue = Image.fromarray(cleaned)
band = noblue.crop((300, 1260, w - 50, 1330))
gray = ImageOps.autocontrast(band.convert("L"))
ups = ImageEnhance.Contrast(
    gray.resize((gray.width * 2, gray.height * 2), Image.Resampling.LANCZOS)
).enhance(2.0)
ups.save(OUT / "0275_noblue_enh.png")
print("noblue:", repr(pytesseract.image_to_string(ups, config="--psm 6")))
print(
    "noblue dig:",
    repr(
        pytesseract.image_to_string(
            ups, config="--psm 6 -c tessedit_char_whitelist=0123456789 "
        )
    ),
)
