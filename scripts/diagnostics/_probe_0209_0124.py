from pathlib import Path

import fitz
import pytesseract
from PIL import Image, ImageEnhance, ImageOps

from services.sales_statement_extractor import _a2z_tesseract

_a2z_tesseract()
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "phase4" / "residual_crops"
BASE = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")

# 0209
doc = fitz.open(BASE / "0000700209_2026_08_ZL_18_374_04092026060313.pdf")
page = doc.load_page(0)
pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), alpha=False)
path = OUT / "0209_x3.png"
pix.save(path)
doc.close()
img = Image.open(path).convert("RGB")
w, h = img.size
# deskew-ish: just OCR top third
top = img.crop((0, int(h * 0.10), w, int(h * 0.40)))
top.save(OUT / "0209_top3.jpg")
print("0209 top OCR:\n", pytesseract.image_to_string(top, config="--psm 6")[:1500])
data = pytesseract.image_to_data(
    top, output_type=pytesseract.Output.DICT, config="--psm 6"
)
for i, txt in enumerate(data["text"]):
    t = (txt or "").strip()
    if "AACT" in t.upper() or t in {"69", "75", "75GR", "75GM"}:
        print("hit", t, data["conf"][i], data["left"][i], data["top"][i])

# 0124 header enhance
doc = fitz.open(BASE / "0000700124_2026_08_ZL_06_521_02092026204703.pdf")
page = doc.load_page(0)
pix = page.get_pixmap(matrix=fitz.Matrix(3.5, 3.5), alpha=False)
path = OUT / "0124_x35.png"
pix.save(path)
doc.close()
img = Image.open(path).convert("RGB")
w, h = img.size
# header qty labels only - roughly under title
band = img.crop((int(w * 0.28), int(h * 0.055), int(w * 0.90), int(h * 0.12)))
gray = ImageOps.autocontrast(band.convert("L"))
ups = gray.resize((gray.width * 2, gray.height * 2), Image.Resampling.LANCZOS)
ups = ImageEnhance.Contrast(ups).enhance(2.0)
ups = ImageOps.invert(ups)
ups = ImageOps.invert(ImageEnhance.Sharpness(ups).enhance(2.0))
ups.save(OUT / "0124_hdr_enh.png")
print("0124 enh:", repr(pytesseract.image_to_string(ups, config="--psm 6")))
print(
    "0124 enh7:",
    repr(pytesseract.image_to_string(ups, config="--psm 7")),
)
# also without invert
gray2 = ImageOps.autocontrast(band.convert("L"))
ups2 = ImageEnhance.Contrast(
    gray2.resize((gray2.width * 3, gray2.height * 3), Image.Resampling.LANCZOS)
).enhance(2.5)
ups2.save(OUT / "0124_hdr_enh2.png")
print("0124 enh2:", repr(pytesseract.image_to_string(ups2, config="--psm 6")))
