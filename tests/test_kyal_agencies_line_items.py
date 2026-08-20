"""KYAL AGENCIES COMP-column phantom products / PARTICULARS names."""
import unittest

from app import (
    _parse_kyal_ocr_line_items,
    _parse_kyal_particulars_block,
    _parse_kyal_particulars_from_page,
    drop_kyal_comp_phantom_items,
    fix_kyal_product_names_from_ocr,
    ocr_suggests_kyal_agencies,
)


KYAL_OCR = """
KYAL AGENCIES PVT. LTD.
GST INVOICE Inv Mod CREDIT
Bill No. KAPL-26-70497 Dated 31/07/2026
S.N HSN COMP PARTICULARS PACK BATCH EXP. QTY. FREE N_MRP O_MRP RATE. AMOUNT GST% DIS%
1. 30049039 ZYDUS CO MEVA C TAB 15S 15 IB00818A 09/27 1 365.57 389.94 278.53 278.53 5.00 0.00
"""


class TestKyalAgenciesLineItems(unittest.TestCase):
    def test_detects_kyal_not_other_formats(self):
        self.assertTrue(ocr_suggests_kyal_agencies(KYAL_OCR))
        self.assertFalse(ocr_suggests_kyal_agencies("CHANDUKA AGENCIES\nCOMP PARTICULARS N_MRP"))
        self.assertFalse(ocr_suggests_kyal_agencies("SUPREME LIFE SCIENCES\nM.R.P SGST"))

    def test_parses_particulars_not_comp(self):
        rows = _parse_kyal_ocr_line_items(KYAL_OCR)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["comp"], "ZYDUS CO")
        self.assertEqual(rows[0]["product_description"], "MEVA C TAB 15S")
        self.assertEqual(rows[0]["quantity"], 1)
        self.assertEqual(rows[0]["unit_price"], 278.53)
        self.assertEqual(rows[0]["total_amount"], 278.53)
        self.assertEqual(rows[0]["lot_batch_number"], "IB00818A")

    def test_drops_zydus_co_phantom_keeps_meva(self):
        items = [
            {
                "product_description": "MEVA C TAB 15S",
                "quantity": "1",
                "unit_price": "278.53",
                "total_amount": "278.53",
                "lot_batch_number": "IB00818A",
            },
            {
                "product_description": "ZYDUS CO",
                "quantity": "15",
                "unit_price": "26.00",
                "total_amount": "389.94",
                "lot_batch_number": "IB00818A",
                "recovered_from_ocr": True,
            },
        ]
        out = drop_kyal_comp_phantom_items(items, KYAL_OCR)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["product_description"], "MEVA C TAB 15S")
        self.assertEqual(out[0]["quantity"], "1")
        self.assertEqual(out[0]["unit_price"], "278.53")
        self.assertEqual(out[0]["total_amount"], "278.53")

    def test_renames_comp_only_row_to_particulars(self):
        items = [
            {
                "product_description": "ZYDUS CO",
                "quantity": "1",
                "unit_price": "278.53",
                "total_amount": "278.53",
            },
        ]
        out = drop_kyal_comp_phantom_items(items, KYAL_OCR)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["product_description"], "MEVA C TAB 15S")
        self.assertEqual(out[0]["quantity"], "1")
        self.assertEqual(out[0]["unit_price"], "278.53")

    def test_fixes_mango_wrap_onto_fluticone(self):
        ocr = KYAL_OCR + """
--- KYAL PARTICULARS ---
085G079	ELECTRAL 4.40GM SACHET
AFC1051	MANGO FLUTICONE FT NASAL SPRAY
INA26002	INDERAL 10MG TAB 15S(DPCO)
"""
        items = [
            {
                "product_description": "ELECTRAL 4.40GM SACHET MANGO",
                "quantity": "50",
                "unit_price": "3.54",
                "total_amount": "177.00",
                "lot_batch_number": "085G079",
            },
            {
                "product_description": "FLUTICONE FT NASAL SPRAY",
                "quantity": "1",
                "unit_price": "422.70",
                "total_amount": "422.70",
                "lot_batch_number": "AFC1051",
            },
            {
                "product_description": "INDERAL 10MG TAB 15S(DPCO)",
                "quantity": "33",
                "unit_price": "16.05",
                "total_amount": "529.65",
                "lot_batch_number": "INA26002",
            },
        ]
        out = fix_kyal_product_names_from_ocr(items, ocr)
        by_batch = {it["lot_batch_number"]: it for it in out}
        self.assertEqual(by_batch["085G079"]["product_description"], "ELECTRAL 4.40GM SACHET")
        self.assertEqual(
            by_batch["AFC1051"]["product_description"],
            "MANGO FLUTICONE FT NASAL SPRAY",
        )
        self.assertEqual(
            by_batch["INA26002"]["product_description"],
            "INDERAL 10MG TAB 15S(DPCO)",
        )
        self.assertEqual(by_batch["085G079"]["quantity"], "50")
        self.assertEqual(by_batch["AFC1051"]["unit_price"], "422.70")

    def test_particulars_parser_on_sample_pdf(self):
        try:
            import fitz
        except ImportError:
            self.skipTest("fitz not available")
        path = "/tmp/kyal69598/invoice_KAPL-26-69598.pdf"
        import os
        if not os.path.exists(path):
            self.skipTest("sample PDF not present")
        doc = fitz.open(path)
        rows = _parse_kyal_particulars_from_page(doc[0])
        by_batch = {r["lot_batch_number"]: r["product_description"] for r in rows}
        self.assertEqual(by_batch.get("085G079"), "ELECTRAL 4.40GM SACHET")
        self.assertEqual(by_batch.get("AFC1051"), "MANGO FLUTICONE FT NASAL SPRAY")
        self.assertEqual(by_batch.get("PCM0179"), "INDERAL LA 20 TAB 15S(DPCO)")
        self.assertEqual(len(rows), 24)

    def test_no_op_for_other_formats(self):
        items = [
            {
                "product_description": "ZYDUS CO",
                "quantity": "15",
                "unit_price": "26.00",
                "total_amount": "389.94",
            },
        ]
        out = drop_kyal_comp_phantom_items(items, "CHANDUKA AGENCIES\nCOMP PARTICULARS N_MRP")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["product_description"], "ZYDUS CO")


if __name__ == "__main__":
    unittest.main()
