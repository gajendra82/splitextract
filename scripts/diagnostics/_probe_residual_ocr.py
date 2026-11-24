"""Probe secondary digit OCR on residual GT-20 crops."""
from __future__ import annotations

from pathlib import Path

import fitz
import pytesseract
from PIL import Image, ImageEnhance, ImageOps

from services.sales_statement_extractor import _a2z_tesseract

_a2z_tesseract()

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "phase4" / "residual_crops"
OUT.mkdir(parents=True, exist_ok=True)
BASE = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")


def dig(pil: Image.Image, label: str) -> str:
    gray = ImageOps.autocontrast(pil.convert("L"))
    ups = gray.resize(
        (max(1, gray.width * 3), max(1, gray.height * 3)),
        Image.Resampling.LANCZOS,
    )
    ups = ImageEnhance.Contrast(ups).enhance(2.2)
    ups.save(OUT / f"{label}.png")
    best = ""
    for psm in (7, 6, 8, 13):
        t = pytesseract.image_to_string(
            ups, config=f"--psm {psm} -c tessedit_char_whitelist=0123456789-"
        ).strip()
        print(label, "psm", psm, repr(t))
        if len(t) > len(best):
            best = t
    return best


def probe_1078() -> None:
    img = Image.open(
        BASE / "0000701078_2026_08_ZL_03_362_05092026080346.jpeg"
    ).convert("RGB")
    w, h = img.size
    data = pytesseract.image_to_data(
        img, output_type=pytesseract.Output.DICT, config="--psm 6"
    )
    for i, txt in enumerate(data["text"]):
        t = (txt or "").strip()
        if not t:
            continue
        up = t.upper()
        if any(k in up for k in ("AACT", "TALEKT", "CLOSING", "EXPIRY")) or t in {
            "69",
            "89",
            "12",
            "13",
            "82",
        }:
            print(
                f"tok={t!r} conf={data['conf'][i]} "
                f"box=({data['left'][i]},{data['top'][i]},"
                f"{data['width'][i]},{data['height'][i]})"
            )

    for name in ("AACT", "TALEKT"):
        for i, txt in enumerate(data["text"]):
            if name not in (txt or "").upper():
                continue
            ty, th = data["top"][i], data["height"][i]
            print(name, "at", data["left"][i], ty, th)
            row = img.crop((int(w * 0.55), max(0, ty - 5), w - 5, ty + th + 12))
            dig(row, f"1078_{name.lower()}_row_right")
            for j, x0 in enumerate((0.60, 0.63, 0.66, 0.70, 0.74, 0.78)):
                cell = img.crop(
                    (
                        int(w * x0),
                        max(0, ty - 4),
                        int(w * (x0 + 0.08)),
                        ty + th + 10,
                    )
                )
                dig(cell, f"1078_{name.lower()}_x{j}")
            break


def probe_0209() -> None:
    doc = fitz.open(BASE / "0000700209_2026_08_ZL_18_374_04092026060313.pdf")
    page = doc.load_page(0)
    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
    path = OUT / "0209_hires.png"
    pix.save(path)
    doc.close()
    img = Image.open(path).convert("RGB")
    w, h = img.size
    data = pytesseract.image_to_data(
        img, output_type=pytesseract.Output.DICT, config="--psm 6"
    )
    for i, txt in enumerate(data["text"]):
        t = (txt or "").strip()
        if not t:
            continue
        if "AACT" in t.upper() or t in {"69", "75GR", "75GM"}:
            print(
                "0209",
                t,
                data["conf"][i],
                data["left"][i],
                data["top"][i],
                data["width"][i],
                data["height"][i],
            )
    for i, txt in enumerate(data["text"]):
        if "AACT" not in (txt or "").upper():
            continue
        ty, th = data["top"][i], data["height"][i]
        row = img.crop((0, max(0, ty - 4), w, ty + th + 14))
        row.save(OUT / "0209_aact_row.png")
        print("0209 row OCR:", repr(pytesseract.image_to_string(row, config="--psm 6")))
        dig(row.crop((int(w * 0.25), 0, w, row.height)), "0209_aact_nums")
        break


def probe_1151() -> None:
    img = Image.open(
        BASE / "0000701151_2026_08_ZL_14_361_03092026025650.jpg"
    ).convert("RGB")
    w, h = img.size
    bottom = img.crop((0, int(h * 0.82), w, h))
    bottom.save(OUT / "1151_bottom2.jpg")
    print("1151 bottom:", repr(pytesseract.image_to_string(bottom, config="--psm 6")))
    data = pytesseract.image_to_data(
        bottom, output_type=pytesseract.Output.DICT, config="--psm 6"
    )
    for i, txt in enumerate(data["text"]):
        t = (txt or "").strip()
        if "TOTAL" in t.upper() or t.replace(",", "").isdigit() and len(t) >= 5:
            print(
                "1151 tok",
                t,
                data["conf"][i],
                data["left"][i],
                data["top"][i],
            )


def probe_0124() -> None:
    doc = fitz.open(BASE / "0000700124_2026_08_ZL_06_521_02092026204703.pdf")
    page = doc.load_page(0)
    pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), alpha=False)
    path = OUT / "0124_hires.png"
    pix.save(path)
    doc.close()
    img = Image.open(path).convert("RGB")
    w, h = img.size
    header = img.crop((0, 0, w, int(h * 0.18)))
    header.save(OUT / "0124_header2.jpg")
    print("0124 header:", repr(pytesseract.image_to_string(header, config="--psm 6")))
    # qty header band only
    band = img.crop((int(w * 0.35), int(h * 0.08), int(w * 0.85), int(h * 0.16)))
    band.save(OUT / "0124_qty_headers.png")
    print("0124 qty hdr:", repr(pytesseract.image_to_string(band, config="--psm 6")))


def probe_0275() -> None:
    doc = fitz.open(BASE / "0000700275_2026_08_ZL_18_374_04092026171615.pdf")
    page = doc.load_page(0)
    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
    path = OUT / "0275_hires.png"
    pix.save(path)
    doc.close()
    img = Image.open(path).convert("RGB")
    w, h = img.size
    data = pytesseract.image_to_data(
        img, output_type=pytesseract.Output.DICT, config="--psm 6"
    )
    for i, txt in enumerate(data["text"]):
        t = (txt or "").strip()
        if not t:
            continue
        if "LIV" in t.upper() or t in {"37", "162", "72", "35", "241", "100ML"}:
            print(
                "0275",
                t,
                data["conf"][i],
                data["left"][i],
                data["top"][i],
                data["width"][i],
                data["height"][i],
            )


if __name__ == "__main__":
    print("=== 1078 ===")
    probe_1078()
    print("=== 0209 ===")
    probe_0209()
    print("=== 1151 ===")
    probe_1151()
    print("=== 0124 ===")
    probe_0124()
    print("=== 0275 ===")
    probe_0275()
