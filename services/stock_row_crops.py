"""Focused upright crops for Phase 2 targeted stock recovery.

OCR is used only to find the header and the failing row. The crop image
is the evidence sent to Gemini. If a crop cannot be made safely, callers
keep the existing full-document evidence.
"""

from __future__ import annotations

import base64
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

_STOP = {
    "tab",
    "tabs",
    "tablet",
    "tablets",
    "capsule",
    "capsules",
    "pack",
    "with",
    "and",
    "the",
    "for",
    "item",
    "name",
}
_DIFFERENTIATORS = ("fort", "oint", "face", "wash", "syrup", "drops", "soap", "balm")


@dataclass
class StockRowCrop:
    product_name: str
    png: bytes
    width: int
    height: int
    includes_header: bool
    includes_ss_column: bool
    # Source-image edges of the detected header, not a fixed page box.
    left: int = 0
    right: int = 0
    source_top: int = 0
    source_bottom: int = 0
    header_labels: tuple = ()


def stock_row_evidence_parts(
    file_bytes: bytes,
    filename: str,
    rows: List[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """PNG parts for each failing row, or None when cropping is not safe."""
    crops = build_stock_row_crops(file_bytes, filename, rows)
    if not crops:
        return None
    return [_png_part(crop.png) for crop in crops]


def build_stock_row_crops(
    file_bytes: bytes,
    filename: str,
    rows: List[Dict[str, Any]],
) -> Optional[List[StockRowCrop]]:
    """All requested rows, or None when any row cannot be cropped safely."""
    located = locate_row_crops(file_bytes, filename, rows)
    if not located or any(crop is None for crop in located):
        return None
    return [crop for crop in located if crop is not None]


def locate_row_crops(
    file_bytes: bytes,
    filename: str,
    rows: List[Dict[str, Any]],
) -> Optional[List[Optional[StockRowCrop]]]:
    """One crop slot per row. None in a slot means that row was not located.

    A missing header leaves every slot empty. Callers must not invent a box.
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix not in IMAGE_SUFFIXES or not file_bytes or not rows:
        return None
    image = load_upright_image(file_bytes)
    if image is None:
        return [None] * len(rows)
    try:
        lines = _lines(image)
        header = _header_line(lines)
        if header is None:
            logger.info("PHASE2_CROP header not found file=%s", filename)
            return [None] * len(rows)
        box = _column_box(image, header)
        if box is None:
            logger.info("PHASE2_CROP columns not safe file=%s", filename)
            return [None] * len(rows)
        header_img = _band(image, box, header["top"], header["bottom"], 10, 8)
        located: List[Optional[StockRowCrop]] = []
        for row in rows:
            name = str(row.get("product_name") or "").strip()
            line = _select_line(lines, name, row) if name else None
            if line is None:
                logger.info("PHASE2_CROP row not located file=%s product=%s", filename, name)
                located.append(None)
                continue
            top, bottom = _row_limits(line)
            row_img = _band(image, box, top, bottom, 4, 3)
            stacked = _stack(header_img, row_img)
            located.append(
                StockRowCrop(
                    product_name=name,
                    png=_png(stacked),
                    width=stacked.width,
                    height=stacked.height,
                    includes_header=True,
                    includes_ss_column=bool(box["ss_inside"]),
                    left=int(box["left"]),
                    right=int(box["right"]),
                    source_top=min(int(header["top"]), top),
                    source_bottom=max(int(header["bottom"]), bottom),
                    header_labels=tuple(box["labels"]),
                )
            )
        return located
    except Exception:
        logger.info("PHASE2_CROP failed file=%s", filename, exc_info=True)
        return [None] * len(rows)


def load_upright_image(file_bytes: bytes):
    """Apply JPEG EXIF orientation before any crop."""
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(file_bytes)) as source:
            upright = ImageOps.exif_transpose(source)
            if upright is None:
                return None
            return upright.convert("RGB")
    except Exception:
        logger.info("PHASE2_CROP image could not be opened", exc_info=True)
        return None


def _png_part(png: bytes) -> Dict[str, Any]:
    return {
        "inline_data": {
            "mime_type": "image/png",
            "data": base64.b64encode(png).decode("ascii"),
        }
    }


def _ocr(image, config: str = ""):
    import pytesseract

    program = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if program.exists():
        pytesseract.pytesseract.tesseract_cmd = str(program)
    kwargs = {"output_type": pytesseract.Output.DICT}
    if config:
        kwargs["config"] = config
    return pytesseract.image_to_data(image, **kwargs)


def _lines(image):
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


def _header_line(lines):
    """Pick a header by shape: several short words, not a numeric data row.

    No column vocabulary is required. A line that does not look like a
    header is left unmatched so the caller can refuse the crop.
    """
    best = None
    best_score = 0
    for line in lines:
        score = _header_score(line["text"])
        if score > best_score:
            best = line
            best_score = score
    if best_score < 3:
        return None
    return best


def _header_score(text: str) -> int:
    words = re.findall(r"[A-Za-z][A-Za-z0-9./&-]{0,24}", text or "")
    if len(words) < 3:
        return 0
    digits = re.findall(r"\d+(?:\.\d+)?", text or "")
    if len(digits) >= len(words):
        return 0
    short = sum(1 for word in words if len(word) <= 12)
    if short < 3:
        return 0
    return short


def _header_tokens(image, header):
    y0 = max(0, header["top"] - 8)
    y1 = min(image.height, header["bottom"] + 24)
    data = _ocr(image.crop((0, y0, image.width, y1)), "--psm 6")
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
                "key": re.sub(r"[^a-z0-9]", "", token.casefold()),
                "x": left,
                "right": left + width,
            }
        )
    return tokens


def _sale_like(key: str) -> bool:
    return key.startswith("sale") or key.startswith("sele")


def _ss_span(tokens):
    """SS Sp Qty sits between Sale and Sale Val. OCR may read SS as 88."""
    explicit = [
        token
        for token in tokens
        if token["key"] in {"ss", "sssp", "ssspqty"} or token["key"].startswith("ss")
    ]
    if explicit:
        ss = min(explicit, key=lambda token: token["x"])
        right = ss["right"]
        for token in tokens:
            if ss["x"] <= token["x"] <= right + 90 and token["key"] in {"sp", "qty", "spqty"}:
                right = max(right, token["right"])
        return ss["x"], right + 36
    qty = [token for token in tokens if token["key"] in {"qty", "spqty"} or token["key"].endswith("qty")]
    sp = [token for token in tokens if token["key"] == "sp"]
    if not qty and not sp:
        return None
    anchor = min(qty or sp, key=lambda token: token["x"])
    right = max(token["right"] for token in qty + sp)
    neighbors = [
        token
        for token in tokens
        if token["right"] <= anchor["x"] + 6
        and anchor["x"] - token["x"] < 180
        and token["key"] not in {"sp", "qty", "spqty"}
        and not _sale_like(token["key"])
    ]
    left = min((token["x"] for token in neighbors), default=max(0, anchor["x"] - 70))
    return left, right + 36


def _column_box(image, header):
    """Table edges are the outermost detected header tokens.

    A fixed page rectangle is not used. Column positions change with the
    customer, the scan size, and the camera, so a box that fit one
    photograph would clip or pad the next one. If the header tokens are
    too few to be a table, return None and let the caller fall back.
    """
    tokens = _header_tokens(image, header)
    usable = [token for token in tokens if token["key"]]
    labels = [token for token in usable if re.search(r"[a-z]", token["key"])]
    if len(labels) < 3:
        return None
    left = min(token["x"] for token in usable)
    right = max(token["right"] for token in usable)
    if right - left < 80:
        return None
    pad = max(8, int((right - left) * 0.015))
    left_edge = max(0, left - pad)
    right_edge = min(image.width, right + pad)
    if right_edge - left_edge < 80:
        return None
    span = _ss_span(tokens)
    ss_left = ss_right = 0
    ss_inside = False
    if span is not None:
        ss_left, ss_right = span
        ss_inside = left_edge <= ss_left and right_edge >= ss_right
    ordered = sorted(usable, key=lambda token: token["x"])
    return {
        "left": int(left_edge),
        "right": int(right_edge),
        "ss_left": ss_left,
        "ss_right": ss_right,
        "ss_inside": ss_inside,
        "labels": tuple(token["text"] for token in ordered),
    }


def _select_line(lines, product_name: str, row: Dict[str, Any]):
    tokens = _tokens(product_name)
    scored = []
    for line in lines:
        if not _eligible(line["text"], product_name):
            continue
        score = _score(line["text"], tokens)
        if score > 0:
            scored.append((score, line))
    if scored:
        best = max(score for score, _line in scored)
        winners = [line for score, line in scored if score == best]
        needed = 1 if len(tokens) <= 1 else 2
        if len(winners) == 1 and best >= needed:
            return winners[0]
        return None
    return _anchor_line(lines, product_name, tokens, row)


def _anchor_line(lines, product_name, tokens, row):
    """Last resort when OCR garbles the name but a unique printed qty remains."""
    found = []
    for number in _anchor_numbers(row):
        hits = [line for line in lines if _has_number(line["text"], number)]
        if len(hits) != 1:
            continue
        named = _nearby_name(lines, hits[0], product_name, tokens)
        if named is not None:
            found.append(named)
    if not found:
        return None
    tops = {line["top"] for line in found}
    if max(tops) - min(tops) > 24:
        return None
    chosen = min(found, key=lambda line: line["top"])
    band = [
        line
        for line in lines
        if abs(line["top"] - chosen["top"]) <= 40 and _eligible(line["text"], product_name)
    ]
    if band:
        chosen = dict(chosen)
        chosen["bottom"] = max(line["bottom"] for line in band)
    return chosen


def _nearby_name(lines, qty_line, product_name, tokens):
    grams = set()
    for token in tokens:
        grams.update(_fragments(token))
    if not grams:
        return None
    for line in lines:
        if abs(line["top"] - qty_line["top"]) > 40:
            continue
        if not _eligible(line["text"], product_name):
            continue
        compact = _compact(line["text"])
        if any(gram in compact for gram in grams):
            return line
    return None


def _anchor_numbers(row: Dict[str, Any]):
    numbers = []
    for key in ("opening_qty", "sales_qty", "receipts_qty"):
        try:
            value = float(row.get(key))
        except (TypeError, ValueError):
            continue
        if value < 10 or not value.is_integer():
            continue
        numbers.append(str(int(value)))
    return numbers


def _tokens(product_name: str):
    words = re.findall(r"[a-z0-9]+", product_name.casefold())
    chosen = [word for word in words if len(word) >= 4 and word not in _STOP]
    return chosen or [word for word in words if len(word) >= 4]


def _score(text: str, tokens) -> int:
    compact = _compact(text)
    return sum(1 for token in tokens if token in compact)


def _eligible(text: str, product_name: str) -> bool:
    compact = _compact(text)
    name = _compact(product_name)
    for word in _DIFFERENTIATORS:
        if word in compact and word not in name:
            return False
    return True


def _fragments(token: str):
    grams = set()
    for size in (4, 3):
        if len(token) < size:
            continue
        for index in range(len(token) - size + 1):
            grams.add(token[index : index + size])
    return grams


def _has_number(text: str, number: str) -> bool:
    return re.search(r"(?<!\d)%s(?!\d)" % re.escape(number), text) is not None


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def _row_limits(line):
    top = int(line["top"])
    bottom = min(int(line["bottom"]), top + 56)
    return top, bottom


def _band(image, box, top, bottom, pad_top, pad_bottom):
    y0 = max(0, int(top) - pad_top)
    y1 = min(image.height, int(bottom) + pad_bottom)
    return image.crop((box["left"], y0, box["right"], y1))


def _stack(header, row):
    from PIL import Image

    gap = 14
    margin = 28
    width = max(header.width, row.width) + margin * 2
    height = header.height + gap + row.height + margin * 2
    canvas = Image.new("RGB", (width, height), "white")
    canvas.paste(header, (margin, margin))
    canvas.paste(row, (margin, margin + header.height + gap))
    return canvas


def _png(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
