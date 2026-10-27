"""Recovery evidence selection. No Gemini calls."""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from services.evidence_selector import (
    KIND_EXCEL_CELLS,
    KIND_IMAGE_CROP,
    KIND_NONE,
    KIND_TEXT,
    SAFETY_FULL_DOCUMENT_FALLBACK,
    SAFETY_NO_SAFE_CROP,
    SAFETY_NO_SAFE_EVIDENCE,
    SAFETY_SAFE,
    select_recovery_evidence,
)
from services.stock_identity_profiles import IdentityProfile, failed_rows

FONT = Path(r"C:\Windows\Fonts\arial.ttf")
JPEG = Path(
    r"C:\Users\amnsa\Downloads\ZL_2026_August"
    r"\0000700077_2026_08_ZL_06_304_06092026141652.jpeg"
)
GENERIC = IdentityProfile("generic", True, True)
UNKNOWN = IdentityProfile("unknown", False, False)


def _font(size):
    if FONT.exists():
        return ImageFont.truetype(str(FONT), size)
    return ImageFont.load_default()


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _row(name):
    return {"product_name": name, "opening_qty": 10, "receipts_qty": 0, "sales_qty": 1, "closing_qty": 9}


def _table(width=1680):
    image = Image.new("RGB", (width, 280), "white")
    draw = ImageDraw.Draw(image)
    font = _font(28)
    headers = (
        (16, "Item"),
        (int(width * 0.22), "Opening"),
        (int(width * 0.40), "Purchase"),
        (int(width * 0.55), "Special"),
        (int(width * 0.68), "Adj"),
        (int(width * 0.78), "Sale"),
        (int(width * 0.90), "Closing"),
    )
    for x, text in headers:
        draw.text((x, 24), text, fill="black", font=font)
    draw.text((16, 120), "ALPHA KEEP", fill="black", font=font)
    draw.text((int(width * 0.22), 120), "10", fill="black", font=font)
    draw.text((int(width * 0.90), 120), "8", fill="black", font=font)
    draw.text((16, 190), "BETA TARGET", fill="black", font=font)
    draw.text((int(width * 0.22), 190), "47", fill="black", font=font)
    draw.text((int(width * 0.78), 190), "4", fill="black", font=font)
    draw.text((int(width * 0.90), 190), "45", fill="black", font=font)
    return image


def _pdf(lines_by_page):
    import fitz

    doc = fitz.open()
    for lines in lines_by_page:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 18
    data = doc.tobytes()
    doc.close()
    return data


def _image_pdf(image: Image.Image) -> bytes:
    import fitz

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    png = buffer.getvalue()
    doc = fitz.open()
    page = doc.new_page(width=image.width, height=image.height)
    page.insert_image(page.rect, stream=png)
    data = doc.tobytes()
    doc.close()
    return data


class EvidenceSelectorTests(unittest.TestCase):
    def test_digital_pdf_with_complete_text_stays_text(self):
        payload = _pdf(
            [
                [
                    "Item | Opening | Purchase | Special Adj | Sale | Closing",
                    "Product A | 100 | 20 | 5 | 10 | 115",
                ]
            ]
        )
        with patch("services.evidence_selector._render_pdf_pages") as render:
            evidences = select_recovery_evidence(
                file_bytes=payload,
                filename="statement.pdf",
                failing_rows=[_row("Product A")],
                identity_profile=GENERIC,
            )
        render.assert_not_called()
        self.assertEqual(len(evidences), 1)
        evidence = evidences[0]
        self.assertEqual(evidence.kind, KIND_TEXT)
        self.assertEqual(evidence.safety, SAFETY_SAFE)
        self.assertEqual(evidence.source_page, 1)
        self.assertIn("Special Adj", evidence.headers)
        self.assertIn("Special Adj", evidence.text)
        self.assertIsNone(evidence.png)
        self.assertIsNone(evidence.crop_bounds)
        self.assertNotIn("stock_out", evidence.text)

    def test_incomplete_pdf_text_does_not_guess_a_crop(self):
        payload = _pdf(
            [
                [
                    "Item | Opening | Purchase | Special Adj | Sale | Closing",
                    "Product A | 100",
                ]
            ]
        )
        with patch("services.evidence_selector._render_pdf_pages", return_value={1: b"png"}) as render:
            with patch("services.stock_row_crops.locate_row_crops", return_value=[None]) as locate:
                evidences = select_recovery_evidence(
                    file_bytes=payload,
                    filename="statement.pdf",
                    failing_rows=[_row("Product A")],
                    identity_profile=GENERIC,
                )
        render.assert_called_once()
        locate.assert_called_once()
        evidence = evidences[0]
        self.assertNotEqual(evidence.kind, KIND_TEXT)
        self.assertEqual(evidence.safety, SAFETY_FULL_DOCUMENT_FALLBACK)
        self.assertIsNone(evidence.crop_bounds)
        self.assertIsNone(evidence.png)

    def test_scanned_pdf_crops_when_the_page_can_be_located(self):
        payload = _image_pdf(_table())
        evidences = select_recovery_evidence(
            file_bytes=payload,
            filename="scan.pdf",
            failing_rows=[_row("BETA TARGET")],
            identity_profile=GENERIC,
        )
        evidence = evidences[0]
        self.assertEqual(evidence.kind, KIND_IMAGE_CROP)
        self.assertEqual(evidence.safety, SAFETY_SAFE)
        self.assertEqual(evidence.mime_type, "image/png")
        self.assertIsNotNone(evidence.png)
        self.assertIsNotNone(evidence.crop_bounds)
        self.assertLess(evidence.crop_bounds["left"], evidence.crop_bounds["right"])
        self.assertNotEqual(evidence.crop_bounds["right"], 2640)

    def test_exif_rotation_is_corrected_before_the_crop(self):
        upright = _table()
        stored = upright.transpose(Image.Transpose.ROTATE_90)
        exif = stored.getexif()
        exif[274] = 6
        buffer = io.BytesIO()
        stored.save(buffer, format="JPEG", exif=exif.tobytes(), quality=95)
        evidences = select_recovery_evidence(
            file_bytes=buffer.getvalue(),
            filename="sheet.jpeg",
            failing_rows=[_row("BETA TARGET")],
            identity_profile=GENERIC,
        )
        evidence = evidences[0]
        self.assertEqual(evidence.kind, KIND_IMAGE_CROP)
        self.assertGreater(evidence.width, evidence.height)
        self.assertNotEqual((evidence.crop_bounds["left"], evidence.crop_bounds["right"]), (40, 2640))

    def test_crop_bounds_follow_the_detected_header_on_each_width(self):
        narrow = select_recovery_evidence(
            file_bytes=_jpeg(_table(1100)),
            filename="narrow.jpeg",
            failing_rows=[_row("BETA TARGET")],
            identity_profile=GENERIC,
        )[0]
        wide = select_recovery_evidence(
            file_bytes=_jpeg(_table(2200)),
            filename="wide.jpeg",
            failing_rows=[_row("BETA TARGET")],
            identity_profile=GENERIC,
        )[0]
        self.assertEqual(narrow.kind, KIND_IMAGE_CROP)
        self.assertEqual(wide.kind, KIND_IMAGE_CROP)
        self.assertGreater(wide.crop_bounds["right"] - narrow.crop_bounds["right"], 400)
        self.assertLess(narrow.crop_bounds["right"], 1100)
        self.assertLess(wide.crop_bounds["right"], 2200)
        self.assertNotEqual(narrow.crop_bounds["right"], 2640)
        self.assertNotEqual(wide.crop_bounds["left"], 40)
        self.assertTrue(narrow.headers)
        self.assertNotIn("stock_out_qty", narrow.headers)

    def test_missing_header_returns_no_safe_crop(self):
        blank = Image.new("RGB", (400, 200), "white")
        evidences = select_recovery_evidence(
            file_bytes=_jpeg(blank),
            filename="blank.jpeg",
            failing_rows=[_row("BETA TARGET")],
            identity_profile=GENERIC,
        )
        evidence = evidences[0]
        self.assertEqual(evidence.safety, SAFETY_NO_SAFE_CROP)
        self.assertIsNone(evidence.crop_bounds)
        self.assertIsNone(evidence.png)

    def test_missing_target_row_returns_no_safe_crop(self):
        evidences = select_recovery_evidence(
            file_bytes=_jpeg(_table()),
            filename="sheet.jpeg",
            failing_rows=[_row("MISSING PRODUCT")],
            identity_profile=GENERIC,
        )
        evidence = evidences[0]
        self.assertEqual(evidence.safety, SAFETY_NO_SAFE_CROP)
        self.assertEqual(evidence.row_id, "MISSING PRODUCT")
        self.assertIsNone(evidence.crop_bounds)

    def test_multipage_pdf_uses_the_page_that_contains_the_row(self):
        payload = _pdf(
            [
                [
                    "Page One | Alpha | Beta | Gamma",
                    "OTHER PRODUCT | 1 | 2 | 3",
                ],
                [
                    "Page Two | Opening | Special Adj | Closing",
                    "TARGET PRODUCT | 9 | 4 | 5",
                ],
            ]
        )
        evidences = select_recovery_evidence(
            file_bytes=payload,
            filename="statement.pdf",
            failing_rows=[_row("TARGET PRODUCT")],
            identity_profile=GENERIC,
        )
        evidence = evidences[0]
        self.assertEqual(evidence.kind, KIND_TEXT)
        self.assertEqual(evidence.source_page, 2)
        self.assertIn("Page Two", evidence.headers)
        self.assertIn("Special Adj", evidence.headers)
        self.assertNotIn("Alpha", evidence.text)

    def test_excel_uses_cells_and_keeps_unknown_headers(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Stock"
        sheet.append(["Item", "Opening", "Purchase", "Special Adj", "Sale", "Closing"])
        sheet.append(["Neighbor", 1, 1, 0, 0, 2])
        sheet.append(["Product A", 100, 20, 5, 10, 115])
        sheet.append(["Other", 3, 0, 0, 1, 2])
        buffer = io.BytesIO()
        workbook.save(buffer)
        evidences = select_recovery_evidence(
            file_bytes=buffer.getvalue(),
            filename="statement.xlsx",
            failing_rows=[_row("Product A"), _row("Other")],
            identity_profile=GENERIC,
        )
        self.assertEqual(len(evidences), 2)
        self.assertEqual({item.kind for item in evidences}, {KIND_EXCEL_CELLS})
        self.assertEqual({item.row_id for item in evidences}, {"Product A", "Other"})
        first = evidences[0]
        self.assertIsNone(first.png)
        self.assertIsNone(first.mime_type)
        self.assertIn("Special Adj", first.headers)
        self.assertIn("Sheet: Stock", first.text)
        self.assertIn("Neighbor", first.text)
        self.assertNotIn("stock_out", first.text)

    def test_txt_html_and_docx_use_text(self):
        txt = b"Item | Opening | Special Adj | Closing\nProduct A | 10 | 1 | 9\n"
        html = (
            b"<table><tr><td>Item</td><td>Opening</td><td>Special Adj</td><td>Closing</td></tr>"
            b"<tr><td>Product A</td><td>10</td><td>1</td><td>9</td></tr></table>"
        )
        from docx import Document

        document = Document()
        table = document.add_table(rows=2, cols=4)
        for index, value in enumerate(["Item", "Opening", "Special Adj", "Closing"]):
            table.rows[0].cells[index].text = value
        for index, value in enumerate(["Product A", "10", "1", "9"]):
            table.rows[1].cells[index].text = value
        buffer = io.BytesIO()
        document.save(buffer)
        for filename, payload in (
            ("notes.txt", txt),
            ("notes.html", html),
            ("notes.docx", buffer.getvalue()),
        ):
            evidence = select_recovery_evidence(
                file_bytes=payload,
                filename=filename,
                failing_rows=[_row("Product A")],
                identity_profile=GENERIC,
            )[0]
            self.assertEqual(evidence.kind, KIND_TEXT, filename)
            self.assertEqual(evidence.safety, SAFETY_SAFE, filename)
            self.assertIn("Special Adj", evidence.headers, filename)
            self.assertIsNone(evidence.png, filename)
            self.assertIsNone(evidence.crop_bounds, filename)

    def test_unknown_file_does_not_guess_a_crop(self):
        with patch("services.stock_row_crops.locate_row_crops") as locate:
            evidences = select_recovery_evidence(
                file_bytes=b"not a known document",
                filename="notes.bin",
                failing_rows=[_row("Product A")],
                identity_profile=GENERIC,
            )
        locate.assert_not_called()
        evidence = evidences[0]
        self.assertEqual(evidence.kind, KIND_NONE)
        self.assertEqual(evidence.safety, SAFETY_NO_SAFE_EVIDENCE)
        self.assertIsNone(evidence.crop_bounds)
        self.assertIsNone(evidence.png)

    def test_unknown_profile_does_not_crop(self):
        with patch("services.stock_row_crops.locate_row_crops") as locate:
            evidences = select_recovery_evidence(
                file_bytes=_jpeg(_table()),
                filename="sheet.jpeg",
                failing_rows=[_row("BETA TARGET")],
                identity_profile=UNKNOWN,
            )
        locate.assert_not_called()
        self.assertEqual(evidences[0].kind, KIND_NONE)
        self.assertEqual(evidences[0].safety, SAFETY_NO_SAFE_EVIDENCE)
        self.assertIsNone(evidences[0].crop_bounds)

    @unittest.skipUnless(JPEG.exists(), "source photo is not on this machine")
    def test_existing_photo_still_crops_from_detected_bounds(self):
        rows = [
            {"product_name": "H CLARINA ANTI ACNE CREAM", "opening_qty": 47, "receipts_qty": 0, "sales_qty": 4, "closing_qty": 45},
            {"product_name": "H KAPIKACHHU TABLETS", "opening_qty": 40, "receipts_qty": 0, "sales_qty": 1, "closing_qty": 40},
            {"product_name": "H MANJISHTHA TAB", "opening_qty": 48, "receipts_qty": 0, "sales_qty": 8, "closing_qty": 39},
            {"product_name": "H PILEX TABLETS", "opening_qty": 106, "receipts_qty": 100, "sales_qty": 131, "closing_qty": 74},
        ]
        evidences = select_recovery_evidence(
            file_bytes=JPEG.read_bytes(),
            filename=JPEG.name,
            failing_rows=rows,
            identity_profile=GENERIC,
        )
        self.assertEqual(len(evidences), 4)
        self.assertEqual([item.row_id for item in evidences], [row["product_name"] for row in rows])
        for evidence in evidences:
            self.assertEqual(evidence.kind, KIND_IMAGE_CROP)
            self.assertEqual(evidence.safety, SAFETY_SAFE)
            self.assertLess(evidence.crop_bounds["left"], evidence.crop_bounds["right"])
            self.assertNotEqual(
                (evidence.crop_bounds["left"], evidence.crop_bounds["right"]),
                (40, 2640),
            )
            self.assertLess(evidence.height, 400)

    def test_selector_has_no_fixed_crop_or_product_rule(self):
        root = Path(__file__).resolve().parents[1]
        for name in (
            "services/evidence_selector.py",
            "services/recovery_runner.py",
            "services/stock_row_crops.py",
        ):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("2640", text, name)
            self.assertNotIn("1771", text, name)
            for token in ("CLARINA", "KAPIKACHHU", "MANJISHTHA", "PILEX"):
                self.assertNotIn(token, text, name)
        runner = (root / "services/recovery_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("image_to_data", runner)
        self.assertNotIn("get_pixmap", runner)
        self.assertNotIn("openpyxl", runner)

    def test_runner_sends_selector_evidence_and_profile_rows(self):
        from services.evidence_selector import RecoveryEvidence
        from services.recovery_runner import build_targeted_payloads
        from services.sales_statement_extractor import empty_line_item

        def item(name, opening, receipts, sales, closing):
            row = empty_line_item()
            row["product_name"] = name
            row["opening_qty"] = opening
            row["receipts_qty"] = receipts
            row["sales_qty"] = sales
            row["closing_qty"] = closing
            row["extra"] = {}
            return row

        bad = item("PRODUCT A", 100, 50, 20, 150)
        good = item("PRODUCT B", 10, 0, 1, 9)
        statement = {
            "line_items": [bad, good],
            "totals": {
                "extra": {
                    "extraction_method": "swil_erp",
                    "stock_identity_kind": "opening_receipts_sales_closing",
                }
            },
            "phase1": {"validation": {"accepted": False}},
        }
        seen = {}

        def locate(_file_bytes, _filename, rows):
            seen["rows"] = [row.get("product_name") for row in rows]
            from services.stock_row_crops import StockRowCrop

            return [StockRowCrop(rows[0]["product_name"], b"CROP-A", 20, 10, True, False)]

        with patch("services.stock_row_crops.locate_row_crops", side_effect=locate):
            payloads = build_targeted_payloads(
                statement,
                strategy="targeted_stock_extraction",
                file_bytes=b"jpeg",
                filename="photo.jpeg",
            )
        self.assertEqual(seen["rows"], [row["product_name"] for row in failed_rows(statement)])
        self.assertEqual(seen["rows"], ["PRODUCT A"])
        self.assertEqual(len(payloads), 1)
        parts = payloads[0]["contents"][0]["parts"]
        self.assertEqual(parts[1]["inline_data"]["data"], b"CROP-A")
        self.assertEqual(payloads[0]["generationConfig"]["responseJsonSchema"]["title"], "SemanticRow")

        excel = RecoveryEvidence(
            kind=KIND_EXCEL_CELLS,
            safety=SAFETY_SAFE,
            row_id="PRODUCT A",
            text="Header: Item | Special Adj\nRow: PRODUCT A | 5",
            headers=["Item", "Special Adj"],
        )
        with patch("services.evidence_selector.select_recovery_evidence", return_value=[excel]):
            text_payloads = build_targeted_payloads(
                statement,
                strategy="targeted_stock_extraction",
                file_bytes=b"sheet",
                filename="statement.xlsx",
            )
        text_parts = text_payloads[0]["contents"][0]["parts"]
        self.assertNotIn("inline_data", text_parts[1])
        self.assertIn("Special Adj", text_parts[1]["text"])
        self.assertNotEqual(
            text_payloads[0]["generationConfig"]["responseJsonSchema"].get("title"),
            "SemanticRow",
        )


if __name__ == "__main__":
    unittest.main()
