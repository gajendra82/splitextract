from pathlib import Path

import pytesseract
from PIL import Image, ImageEnhance, ImageOps

from services.sales_statement_extractor import _a2z_tesseract

_a2z_tesseract()
BASE = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "phase4" / "residual_crops"
img = Image.open(BASE / "0000701078_2026_08_ZL_03_362_05092026080346.jpeg").convert(
    "RGB"
)
w, h = img.size
data = pytesseract.image_to_data(
    img, output_type=pytesseract.Output.DICT, config="--psm 6"
)

# All tokens near AACTARIL y
aact_y = None
for i, txt in enumerate(data["text"]):
    if "AACT" in (txt or "").upper():
        aact_y = data["top"][i]
        print("AACT", txt, data["left"][i], data["top"][i], data["conf"][i])
print("--- tokens y in [260,320] ---")
for i, txt in enumerate(data["text"]):
    t = (txt or "").strip()
    if not t:
        continue
    if 260 <= data["top"][i] <= 320:
        print(
            t,
            "x",
            data["left"][i],
            "y",
            data["top"][i],
            "w",
            data["width"][i],
            "conf",
            data["conf"][i],
        )

# Closing header x
close_x = None
for i, txt in enumerate(data["text"]):
    if (txt or "").strip().lower() == "closing":
        close_x = data["left"][i]
        print("Closing header", close_x, data["top"][i], data["width"][i])
        break

if aact_y is not None and close_x is not None:
    # Crop closing cell using header x alignment
    cell = img.crop(
        (
            close_x - 10,
            aact_y - 6,
            close_x + 80,
            aact_y + 28,
        )
    )
    cell.save(OUT / "1078_close_aligned.png")
    gray = ImageOps.autocontrast(cell.convert("L"))
    ups = gray.resize((gray.width * 4, gray.height * 4), Image.Resampling.LANCZOS)
    ups = ImageEnhance.Contrast(ups).enhance(2.5)
    ups.save(OUT / "1078_close_aligned_enh.png")
    for psm in (7, 8, 10, 13, 6):
        t = pytesseract.image_to_string(
            ups, config=f"--psm {psm} -c tessedit_char_whitelist=0123456789-"
        ).strip()
        print("aligned close psm", psm, repr(t))

# Also try full-line OCR of AACTARIL band
band = img.crop((0, aact_y - 8, w, aact_y + 30))
band.save(OUT / "1078_aact_band2.png")
print("band:", repr(pytesseract.image_to_string(band, config="--psm 6")))
print(
    "band digits:",
    repr(
        pytesseract.image_to_string(
            ImageOps.autocontrast(band.convert("L")).resize(
                (band.width * 2, band.height * 2), Image.Resampling.LANCZOS
            ),
            config="--psm 6 -c tessedit_char_whitelist=0123456789- ",
        )
    ),
)

# TALEKT expiry: header Expiry at x=817, TALEKT at y=1239; also token 12 at x=839 y=1240
print("--- TALEKT line ---")
for i, txt in enumerate(data["text"]):
    t = (txt or "").strip()
    if not t:
        continue
    if 1225 <= data["top"][i] <= 1270:
        print(t, "x", data["left"][i], "y", data["top"][i], "conf", data["conf"][i])
