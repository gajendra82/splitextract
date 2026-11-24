"""Diagnostic only. Two Gemini reads of a padded row crop.

Does not change production extraction, recovery, schema, or validation.
OCR only locates the row and the column header. Gemini sees the crop.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.focused_row_request import (
    FOCUSED_ROW_MODEL,
    PRINTED_PROMPT,
    PRINTED_SCHEMA,
    SEMANTIC_PROMPT,
    SEMANTIC_SCHEMA,
    build_focused_row_payload,
    request_fingerprint,
)
from services.vertex_gemini_client import generate_content_via_vertex

JPEG = Path(
    r"C:\Users\amnsa\Downloads\ZL_2026_August"
    r"\0000700077_2026_08_ZL_06_304_06092026141652.jpeg"
)
TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
PREVIEW = ROOT / "qa_results_images" / "_diag_v2"

# Measured on the upright header when OCR misses a label.
FALLBACK_X = {
    "left": 40,
    "right": 2560,
    "ss_left": 1771,
    "ss_right": 1920,
}

TARGETS = (
    ("H CLARINA ANTI ACNE CREAM", ("clarina", "cream"), ("face",)),
    ("H KAPIKACHHU TABLETS", ("kapikachhu",), ()),
    ("H MANJISHTHA TAB", ("manjish",), ()),
    ("H PILEX TABLETS", ("pilex", "tablet"), ("fort", "oint")),
)

# Op, Pur, Sale, SS Sp Qty, Bal
KNOWN = {
    "H CLARINA ANTI ACNE CREAM": (47, 0, 4, 0, 45),
    "H KAPIKACHHU TABLETS": (40, 0, 1, 0, 40),
    "H MANJISHTHA TAB": (48, 0, 8, 1, 39),
    "H PILEX TABLETS": (106, 100, 131, 1, 74),
}

PROMPT_A = SEMANTIC_PROMPT
PROMPT_B = PRINTED_PROMPT
SCHEMA_A = SEMANTIC_SCHEMA
SCHEMA_B = PRINTED_SCHEMA


def _ocr(image: Image.Image):
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = TESSERACT
    return pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)


def _lines(image: Image.Image):
    left = image.crop((0, 0, int(image.width * 0.62), image.height))
    data = _ocr(left)
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
            grouped.append({"top": top, "bottom": top + height, "text": token})
    return grouped


def _find(lines, required, blocked):
    for line in lines:
        text = line["text"].casefold()
        if all(part in text for part in required) and not any(part in text for part in blocked):
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


def _header_line(lines):
    for line in lines:
        text = line["text"].casefold()
        if "item" in text and "op" in text:
            return line
    for line in lines:
        text = line["text"].casefold()
        if "pack" in text and "pur" in text:
            return line
    return None


def _header_tokens(image: Image.Image, header):
    y0 = max(0, header["top"] - 8)
    y1 = min(image.height, header["bottom"] + 24)
    band = image.crop((0, y0, image.width, y1))
    data = _ocr(band)
    tokens = []
    for index, token in enumerate(data["text"]):
        token = (token or "").strip()
        if not token:
            continue
        left = int(data["left"][index])
        width = int(data["width"][index])
        tokens.append(
            {
                "text": token,
                "fold": token.casefold(),
                "x": left,
                "right": left + width,
            }
        )
    return tokens


def _token_span(tokens, predicate):
    hits = [token for token in tokens if predicate(token)]
    if not hits:
        return None
    return min(token["x"] for token in hits), max(token["right"] for token in hits)


def _column_box(image: Image.Image, header):
    tokens = _header_tokens(image, header)
    sale = _token_span(
        tokens,
        lambda token: token["fold"].startswith("sale") and 1500 <= token["x"] <= 1760,
    )
    ss = _token_span(
        tokens,
        lambda token: token["fold"] in {"ss", "ss|sp", "sp"} or token["fold"].startswith("ss"),
    )
    sp = _token_span(tokens, lambda token: token["fold"] in {"sp", "spqty"})
    bal = _token_span(
        tokens,
        lambda token: token["fold"].startswith("bal") and token["x"] < 2320,
    )
    bal_val = _token_span(
        tokens,
        lambda token: token["x"] >= 2320 and token["fold"].startswith(("bal", "va")),
    )
    ss_left = ss[0] if ss else FALLBACK_X["ss_left"]
    ss_right = max(
        ss[1] if ss else FALLBACK_X["ss_right"],
        sp[1] if sp else FALLBACK_X["ss_right"],
    )
    # Qty sits just after "Sp" and was not always OCR'd.
    ss_right += 36
    crop_left = 40
    crop_right = (bal_val[1] if bal_val else FALLBACK_X["right"]) + 80
    # Keep Sale on the left of SS and Sale Val / Bal on the right.
    pad = 96
    crop_left = min(crop_left, ss_left - pad)
    crop_right = max(crop_right, ss_right + pad)
    crop_left = max(0, crop_left)
    crop_right = min(image.width, crop_right)
    return {
        "left": crop_left,
        "right": crop_right,
        "ss_left": ss_left,
        "ss_right": ss_right,
        "sale_left": sale[0] if sale else None,
        "bal_right": bal[1] if bal else None,
        "pad_left": ss_left - crop_left,
        "pad_right": crop_right - ss_right,
    }


def _row_limits(line):
    """Keep one printed row. OCR boxes sometimes swallow the next product."""
    top = line["top"]
    bottom = min(line["bottom"], top + 56)
    return top, bottom


def _band(image, box, top, bottom, pad_top=8, pad_bottom=6):
    y0 = max(0, top - pad_top)
    y1 = min(image.height, bottom + pad_bottom)
    return image.crop((box["left"], y0, box["right"], y1))


def _stack(header: Image.Image, row: Image.Image) -> Image.Image:
    gap = 14
    margin = 28
    width = max(header.width, row.width) + margin * 2
    height = header.height + gap + row.height + margin * 2
    canvas = Image.new("RGB", (width, height), "white")
    canvas.paste(header, (margin, margin))
    canvas.paste(row, (margin, margin + header.height + gap))
    return canvas


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _ask(image: Image.Image, prompt: str, schema: dict, product: str = "") -> dict:
    kind = "printed" if schema is PRINTED_SCHEMA else "semantic"
    payload = build_focused_row_payload(_png(image), kind=kind)
    if payload["contents"][0]["parts"][0]["text"] != prompt:
        raise RuntimeError("focused-row prompt drifted from the diagnostic")
    if payload["generationConfig"]["responseJsonSchema"] != schema:
        raise RuntimeError("focused-row schema drifted from the diagnostic")
    fingerprint = request_fingerprint(
        payload, product=product, width=image.width, height=image.height
    )
    print("FINGERPRINT product=%s" % fingerprint["product"])
    print(
        "FINGERPRINT crop=%sx%s mime=%s crop_sha256=%s prompt_sha256=%s schema_sha256=%s model=%s"
        % (
            fingerprint["crop_width"],
            fingerprint["crop_height"],
            fingerprint["mime"],
            fingerprint["crop_sha256"],
            fingerprint["prompt_sha256"],
            fingerprint["schema_sha256"],
            fingerprint["model"],
        )
    )
    response = generate_content_via_vertex(FOCUSED_ROW_MODEL, payload, timeout=60)
    text = (
        ((response.json().get("candidates") or [{}])[0].get("content") or {})
        .get("parts", [{}])[0]
        .get("text")
        or ""
    )
    return json.loads(text)


def _num(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _ss_class(value):
    number = _num(value)
    if number is None:
        return "null"
    if number == 1:
        return "1"
    if number == 0:
        return "0"
    return "other:%s" % number


def _quintuple(result, keys):
    return tuple(_num(result.get(key)) for key in keys)


def _crops(image):
    lines = _lines(image)
    header = _header_line(lines)
    if header is None:
        raise SystemExit("column header row was not found")
    box = _column_box(image, header)
    header_img = _band(image, box, header["top"], header["bottom"], pad_top=10, pad_bottom=8)
    crops = []
    for label, required, blocked in TARGETS:
        line = _find(lines, required, blocked)
        if line is None and label == "H PILEX TABLETS":
            line = _find_pilex_tablets(lines)
        if line is None:
            crops.append((label, None, box))
            continue
        top, bottom = _row_limits(line)
        row_img = _band(image, box, top, bottom, pad_top=4, pad_bottom=3)
        crops.append((label, _stack(header_img, row_img), box))
    return crops


def _print_comparison(got, expected):
    status = "match" if got == expected else "differ"
    print(
        "printed comparison:",
        status,
        "expected Op=%s Pur=%s Sale=%s SS Sp Qty=%s Bal=%s" % expected,
    )


def main() -> None:
    image = ImageOps.exif_transpose(Image.open(JPEG)).convert("RGB")
    crops = _crops(image)
    keys_a = ("opening_qty", "receipts_qty", "sales_qty", "stock_out_qty", "closing_qty")
    keys_b = ("Op", "Pur", "Sale", "SS Sp Qty", "Bal")
    pilex = {"A": None, "B": None}
    scores = []
    for label, crop, box in crops:
        expected = KNOWN[label]
        print("PRODUCT", label)
        if crop is None:
            print("Crop dimensions: not found")
            print("Gemini result: row not located")
            print()
            continue
        print("Crop dimensions: %sx%s" % crop.size)
        print(
            "SS Sp Qty column x: %s-%s  padding left=%spx right=%spx"
            % (box["ss_left"], box["ss_right"], box["pad_left"], box["pad_right"])
        )
        print("TEST A semantic fields")
        try:
            result_a = _ask(crop, PROMPT_A, SCHEMA_A, label)
        except Exception as exc:
            print("Gemini result: ERROR %s" % exc)
            result_a = None
        if result_a is not None:
            print("Gemini result: %s" % json.dumps(result_a))
            got_a = _quintuple(result_a, keys_a)
            print("opening:", got_a[0])
            print("receipts:", got_a[1])
            print("sales:", got_a[2])
            print("stock_out:", got_a[3])
            print("closing:", got_a[4])
            _print_comparison(got_a, expected)
            scores.append(got_a == expected)
            if label == "H PILEX TABLETS":
                pilex["A"] = _ss_class(result_a.get("stock_out_qty"))
        print("TEST B printed column names")
        try:
            result_b = _ask(crop, PROMPT_B, SCHEMA_B, label)
        except Exception as exc:
            print("Gemini result: ERROR %s" % exc)
            result_b = None
        if result_b is not None:
            print("Gemini result: %s" % json.dumps(result_b))
            got_b = _quintuple(result_b, keys_b)
            print("Op:", got_b[0])
            print("Pur:", got_b[1])
            print("Sale:", got_b[2])
            print("SS Sp Qty:", got_b[3])
            print("Bal:", got_b[4])
            _print_comparison(got_b, expected)
            scores.append(got_b == expected)
            if label == "H PILEX TABLETS":
                pilex["B"] = _ss_class(result_b.get("SS Sp Qty"))
        print()

    print("PILEX SS Sp Qty TEST A:", pilex["A"])
    print("PILEX SS Sp Qty TEST B:", pilex["B"])
    print("CONCLUSION:", _conclusion(scores, pilex))


def _conclusion(scores, pilex):
    if not scores:
        return "D. crops were not read, so another preprocessing step is still untested"
    both = (pilex["A"], pilex["B"])
    if all(scores) and both == ("1", "1"):
        return "A. crop evidence is sufficient for production recovery"
    if "1" not in both:
        return (
            "C. Gemini still struggles with the tiny SS Sp Qty cell "
            "(PILEX printed 1, TEST A=%s, TEST B=%s)"
            % (pilex["A"], pilex["B"])
        )
    return (
        "B. SS Sp Qty needs a narrower crop "
        "(PILEX TEST A=%s, TEST B=%s)"
        % (pilex["A"], pilex["B"])
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "crops":
        image = ImageOps.exif_transpose(Image.open(JPEG)).convert("RGB")
        PREVIEW.mkdir(parents=True, exist_ok=True)
        for label, crop, box in _crops(image):
            print(label, None if crop is None else crop.size, box)
            if crop is not None:
                crop.save(PREVIEW / (label.replace(" ", "_") + ".png"))
    else:
        main()
