"""Diagnostic only. Ask Gemini to transcribe four cropped table rows.

Does not change production extraction, recovery, or validation.
OCR is used only to find the row. The crop image is what Gemini sees.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.vertex_gemini_client import generate_content_via_vertex

JPEG = Path(
    r"C:\Users\amnsa\Downloads\ZL_2026_August"
    r"\0000700077_2026_08_ZL_06_304_06092026141652.jpeg"
)
TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
MODEL = "gemini-2.5-flash-lite"

PROMPT = (
    "Read the printed cells exactly as they appear in this table row.\n"
    "\n"
    "Column mapping:\n"
    "Op = opening quantity\n"
    "Pur = purchase/receipt quantity\n"
    "Sale = sales quantity\n"
    "SS Sp Qty = stock-out/special quantity\n"
    "Bal = closing/balance quantity\n"
    "\n"
    "Copy the printed values exactly.\n"
    "Do not calculate any value.\n"
    "Do not use the stock equation.\n"
    "Do not infer a value.\n"
    "Return null only if the printed cell is genuinely unreadable or absent."
)

SCHEMA = {
    "type": "object",
    "title": "PrintedRow",
    "properties": {
        "product_name": {"type": ["string", "null"]},
        "opening_qty": {"type": ["number", "null"]},
        "receipts_qty": {"type": ["number", "null"]},
        "sales_qty": {"type": ["number", "null"]},
        "stock_out_qty": {"type": ["number", "null"]},
        "closing_qty": {"type": ["number", "null"]},
    },
    "required": [
        "product_name",
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "stock_out_qty",
        "closing_qty",
    ],
}

TARGETS = (
    ("H CLARINA ANTI ACNE CREAM", ("clarina", "cream"), ("face",)),
    ("H KAPIKACHHU TABLETS", ("kapikachhu",), ()),
    ("H MANJISHTHA TAB", ("manjish",), ()),
    ("H PILEX TABLETS", ("pilex", "tablet"), ("fort", "oint")),
)

KNOWN = {
    "H CLARINA ANTI ACNE CREAM": (47, 0, 4, 0, 45),
    "H KAPIKACHHU TABLETS": (40, 0, 1, 0, 40),
    "H MANJISHTHA TAB": (48, 0, 8, 1, 39),
    "H PILEX TABLETS": (106, 100, 131, 1, 74),
}


def _lines(image: Image.Image):
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = TESSERACT
    left = image.crop((0, 0, int(image.width * 0.62), image.height))
    data = pytesseract.image_to_data(left, output_type=pytesseract.Output.DICT)
    grouped = []
    for index, token in enumerate(data["text"]):
        token = (token or "").strip()
        if not token:
            continue
        top = int(data["top"][index])
        height = int(data["height"][index])
        if grouped and abs(top - grouped[-1]["top"]) <= 18:
            row = grouped[-1]
            row["top"] = min(row["top"], top)
            row["bottom"] = max(row["bottom"], top + height)
            row["text"] += " " + token
        else:
            grouped.append(
                {"top": top, "bottom": top + height, "text": token}
            )
    return grouped


def _find(lines, required, blocked):
    for line in lines:
        text = line["text"].casefold()
        if all(part in text for part in required) and not any(
            part in text for part in blocked
        ):
            return line
    return None


def _find_pilex_tablets(lines):
    """OCR splits this row: the name becomes 'ILE' and Op 106 sits beside it."""
    name_bits = []
    qty_bits = []
    for line in lines:
        compact = line["text"].casefold().replace(" ", "")
        if "fort" in compact or "oint" in compact:
            continue
        if "ile" in compact or "sotab" in compact or "60tab" in compact:
            name_bits.append(line)
        if "106" in line["text"] or "15805" in compact:
            qty_bits.append(line)
    for name in name_bits:
        for qty in qty_bits:
            if abs(name["top"] - qty["top"]) <= 40:
                return {
                    "top": name["top"],
                    "bottom": max(name["bottom"], qty["bottom"]),
                    "text": name["text"] + " " + qty["text"],
                }
    return None


def _header_band(lines):
    for line in lines:
        text = line["text"].casefold()
        if "item" in text and "op" in text:
            return line
    for line in lines:
        if "pack" in line["text"].casefold() and "pur" in line["text"].casefold():
            return line
    return None


def _band(image: Image.Image, top: int, bottom: int) -> Image.Image:
    pad_top = 10
    pad_bottom = 16
    y0 = max(0, top - pad_top)
    y1 = min(image.height, bottom + pad_bottom)
    return image.crop((30, y0, image.width - 30, y1))


def _stack(header: Image.Image, row: Image.Image) -> Image.Image:
    gap = 12
    width = max(header.width, row.width)
    canvas = Image.new("RGB", (width, header.height + gap + row.height), "white")
    canvas.paste(header, (0, 0))
    canvas.paste(row, (0, header.height + gap))
    return canvas


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _ask(image: Image.Image) -> dict:
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": PROMPT},
                    {
                        "inline_data": {
                            "mime_type": "image/png",
                            "data": _png(image),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
            "responseJsonSchema": SCHEMA,
        },
    }
    response = generate_content_via_vertex(MODEL, payload, timeout=60)
    text = (
        ((response.json().get("candidates") or [{}])[0].get("content") or {})
        .get("parts", [{}])[0]
        .get("text")
        or ""
    )
    return json.loads(text)


def main() -> None:
    image = ImageOps.exif_transpose(Image.open(JPEG)).convert("RGB")
    lines = _lines(image)
    header = _header_band(lines)
    if header is None:
        raise SystemExit("column header row was not found")
    header_img = _band(image, header["top"], header["bottom"])
    for label, required, blocked in TARGETS:
        line = _find(lines, required, blocked)
        if line is None and label == "H PILEX TABLETS":
            line = _find_pilex_tablets(lines)
        print("PRODUCT", label)
        if line is None:
            print("Crop dimensions: not found")
            print("Gemini result: row not located")
            print()
            continue
        row_img = _band(image, line["top"], line["bottom"])
        crop = _stack(header_img, row_img)
        print("Crop dimensions: %sx%s" % crop.size)
        try:
            result = _ask(crop)
        except Exception as exc:
            print("Gemini result: ERROR %s" % exc)
            print()
            continue
        print("Gemini result: %s" % json.dumps(result))
        print("opening:", result.get("opening_qty"))
        print("receipts:", result.get("receipts_qty"))
        print("sales:", result.get("sales_qty"))
        print("stock_out:", result.get("stock_out_qty"))
        print("closing:", result.get("closing_qty"))
        expected = KNOWN[label]
        got = (
            result.get("opening_qty"),
            result.get("receipts_qty"),
            result.get("sales_qty"),
            result.get("stock_out_qty"),
            result.get("closing_qty"),
        )
        print(
            "printed comparison:",
            "match" if got == expected else "differ",
            "expected Op=%s Pur=%s Sale=%s SS=%s Bal=%s" % expected,
        )
        print()


if __name__ == "__main__":
    main()
