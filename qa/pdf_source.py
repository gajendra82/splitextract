"""Independent PDF text and word extraction for ground truth.

Uses embedded PDF text first. Renders and OCRs a page only when that page has
almost no embedded text. Does not call the production statement parser.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

Word = Tuple[float, float, float, float, str]


@dataclass
class PageSource:
    page_index: int
    words: List[Word]
    text: str
    source: str  # embedded | ocr
    notes: List[str] = field(default_factory=list)


def _configure_tesseract() -> bool:
    try:
        import pytesseract
    except ImportError:
        return False
    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
        return True
    if os.name == "nt":
        win_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(win_cmd):
            pytesseract.pytesseract.tesseract_cmd = win_cmd
            return True
    return True


def _embedded_words(page) -> List[Word]:
    words: List[Word] = []
    for w in page.get_text("words") or []:
        if len(w) < 5:
            continue
        text = str(w[4]).strip()
        if not text:
            continue
        words.append((float(w[0]), float(w[1]), float(w[2]), float(w[3]), text))
    return words


def _ocr_page(page, zoom: float, psm: int):
    import fitz
    import pytesseract
    from PIL import Image
    import io

    _configure_tesseract()
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    config = f"--psm {psm}"
    data = pytesseract.image_to_data(
        image, output_type=pytesseract.Output.DICT, config=config
    )
    words: List[Word] = []
    n = len(data.get("text") or [])
    for i in range(n):
        text = str(data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if conf < 0:
            continue
        x = float(data["left"][i]) / zoom
        y = float(data["top"][i]) / zoom
        w = float(data["width"][i]) / zoom
        h = float(data["height"][i]) / zoom
        if w <= 0 or h <= 0:
            continue
        words.append((x, y, x + w, y + h, text))
    string = pytesseract.image_to_string(image, config=config) or ""
    return words, string


def _page_text(words: Sequence[Word], embedded: str) -> str:
    if embedded and len("".join(embedded.split())) >= 40:
        return embedded
    rows: List[List[Word]] = []
    for word in sorted(words, key=lambda w: (((w[1] + w[3]) / 2.0), w[0])):
        cy = (word[1] + word[3]) / 2.0
        if not rows or abs(cy - ((rows[-1][0][1] + rows[-1][0][3]) / 2.0)) > 6:
            rows.append([word])
        else:
            rows[-1].append(word)
    lines = []
    for row in rows:
        row.sort(key=lambda w: w[0])
        lines.append(" ".join(w[4] for w in row))
    return "\n".join(lines)


def load_pdf_pages(path: str, max_pages: int = 20) -> Tuple[List[PageSource], List[str]]:
    """Read up to ``max_pages`` pages. Image-only pages are OCR'd once (psm 6)."""
    import fitz

    notes: List[str] = []
    doc = fitz.open(path)
    pages: List[PageSource] = []
    try:
        total = doc.page_count
        limit = min(total, max_pages)
        if total > max_pages:
            notes.append(
                f"Ground truth read the first {max_pages} of {total} pages, "
                "matching SALES_PDF_MAX_PAGES."
            )
        ocr_ready = None
        for index in range(limit):
            page = doc[index]
            embedded = page.get_text("text") or ""
            words = _embedded_words(page)
            source = "embedded"
            page_notes: List[str] = []
            embedded_len = len("".join(embedded.split()))
            if embedded_len < 40 or len(words) < 12:
                if ocr_ready is None:
                    ocr_ready = _configure_tesseract()
                if not ocr_ready:
                    page_notes.append("Tesseract is not available for this scanned page.")
                else:
                    zoom = float(os.getenv("QA_OCR_ZOOM", "3.0"))
                    chosen = []
                    chosen_text = ""
                    chosen_psm = None
                    chosen_digits = -1
                    for psm in (6, 4):
                        try:
                            ocr, string = _ocr_page(page, zoom=zoom, psm=psm)
                        except Exception as exc:
                            ocr = []
                            string = ""
                            page_notes.append(f"OCR psm {psm} failed: {exc}")
                            continue
                        digits = sum(
                            1 for w in ocr if any(ch.isdigit() for ch in w[4])
                        )
                        if digits > chosen_digits:
                            chosen = ocr
                            chosen_text = string
                            chosen_psm = psm
                            chosen_digits = digits
                        if psm == 6 and digits >= 25:
                            break
                    if len(chosen) > len(words):
                        words = chosen
                        source = "ocr"
                        embedded = chosen_text or ""
                        if chosen_psm:
                            page_notes.append(f"OCR used tesseract psm {chosen_psm}.")
            pages.append(
                PageSource(
                    page_index=index,
                    words=words,
                    text=_page_text(words, embedded),
                    source=source,
                    notes=page_notes,
                )
            )
    finally:
        doc.close()
    return pages, notes
