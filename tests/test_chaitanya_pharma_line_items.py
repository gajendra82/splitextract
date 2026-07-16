"""CHAITANYA/BENSUS Tax Inv. line-item corrections."""
import unittest

from app import fix_chaitanya_pharma_line_items_from_ocr


CHAITANYA_OCR = """
TAX INVOICE
CHAITANYA PHARMA
BENSUS PHARMA Tax Inv. No.: S 4360
MFD. QTY FREEPKG DESCRIPTION HSN Code BATCH # EXP MRP RATE VALUE DIS% GST%NET AMOUNT
ZYDUS CAR50D 0 10*15 TVINGLYN M 500 30049099 AXC1012 02/28 173.04 98.96 4948.00 6.00 5.00 4883.68 Bill No.
ZYDUS MED5I 0 3ML PENSEMAGLYN INJ PEN 90189099 B260023 03/31 1250.00 856.29 4281.45 6.00 5.00 4225.78 4360
"""


class TestChaitanyaPharmaLineItems(unittest.TestCase):
    def test_qty_from_value_rate_and_pen_fused_name(self):
        items = [
            {
                "product_description": "VINGLYN M 500",
                "quantity": "0",
                "unit_price": "98.96",
                "total_amount": "4883.68",
                "lot_batch_number": "AXC1012",
            },
            {
                "product_description": "PENSEMAGLYN INJ PEN",
                "quantity": "5",
                "unit_price": "856.29",
                "total_amount": "4281.45",
                "lot_batch_number": "B260023",
            },
        ]
        out = fix_chaitanya_pharma_line_items_from_ocr(items, CHAITANYA_OCR)
        self.assertEqual(out[0]["quantity"], "50")
        self.assertEqual(out[0]["unit_price"], "98.96")
        self.assertEqual(out[1]["product_description"], "SEMAGLYN INJ PEN")
        self.assertEqual(out[1]["quantity"], "5")

    def test_unit_price_from_rate_not_net_over_qty(self):
        ocr = """
        TAX INVOICE
        CHAITANYA PHARMA
        BENSUS PHARMA Tax Inv. No.: S 4923
        MFD. QTY FREEPKG DESCRIPTION HSN Code BATCH # EXP MRP RATE VALUE DIS% GST%NET AMOUNT
        ZYDUS TOP30 0 1VIAL AZTREO 1000 INJ 30042019 7015034A 11/27 808.13 139.86 4195.80 6.00 5.00 4141.25 Bill No.
        """
        items = [
            {
                "product_description": "AZTREO 1000 INJ",
                "quantity": "30",
                "unit_price": "138.04",  # wrongly derived from NET/qty
                "total_amount": "4141.25",
                "lot_batch_number": "7015034A",
                "additional_fields": {"mrp": "808.13"},
            }
        ]
        out = fix_chaitanya_pharma_line_items_from_ocr(items, ocr)
        self.assertEqual(out[0]["unit_price"], "139.86")
        self.assertEqual(out[0]["quantity"], "30")

    def test_strips_leading_t_from_tvinglyn_sale(self):
        ocr = """
        TAX INVOICE
        CHAITANYA PHARMA
        BENSUS PHARMA Tax Inv. No.: S 5178
        MFD. QTY FREEPKG DESCRIPTION HSN Code BATCH # EXP MRP RATE VALUE DIS% GST%NET AMOUNT
        ZYDUS CAR30D 0 10*15 TVINGLYN SALE 30049099 AXC1003* 12/27 123.19 77.49 2324.70 5.00 2440.94 Bill No.
        """
        items = [
            {
                "product_description": "TVINGLYN SALE",
                "quantity": "30",
                "unit_price": "77.49",
                "total_amount": "2324.70",
                "lot_batch_number": "AXC1003*",
            }
        ]
        out = fix_chaitanya_pharma_line_items_from_ocr(items, ocr)
        self.assertEqual(out[0]["product_description"], "VINGLYN SALE")

    def test_cleans_glued_pack_product_names_from_s5090(self):
        ocr = """
        TAX INVOICE
        CHAITANYA PHARMA
        BENSUS PHARMA Tax Inv. No.: S 5090
        MFD. QTY FREEPKG DESCRIPTION HSN Code BATCH # EXP MRP RATE VALUE DIS% GST%NET AMOUNT
        ZYDUS GER7M0 0 5*2*7*2MDLERIPHYLLIN INJ 30049094 NB00189A 02/30 11.22 8.06 564.20 6.00 5.00 556.87
        GERMAN 6 0 3*5 SUPPJONAC SUPPOS 100MG 30049069 TB0490A 03/29 149.74 108.54 651.24 6.00 5.00 642.77
        GERMAN2000 0 4*5*2.5 MCOLMBIMIST L RESPULES 30049099 EB60053 01/28 26.25 10.45 20900.00 6.00 5.00 20628.30
        ZYDUS 100 0 1ML AMNPUCOXIA INJ 30049099 CTB1012A 08/27 256.78 183.33 18333.00 6.00 5.00 18094.68
        ZYDUS 5 0 10ML ARZEP NASAL SPARY 30049099 MA06836A 11/27 589.30 403.70 2018.50 6.00 5.00 1992.25
        ZYDUS ME1D0I 0 10 ATORVA 80 TAB 30049079 IA01384C 08/27 433.49 296.96 2969.60 6.00 5.00 2931.00
        ZYDUS ME5D0I 0 10 CLOPITORVA 20 CAP 30049099 IB00722A 02/28 392.54 248.98 12449.00 6.00 5.00 12287.16
        ZYDUS 20 0 10*15*sTENGLYN TAB 30049099 IB00537A 01/28 111.99 62.71 1254.20 6.00 5.00 1238.59
        """
        items = [
            {"product_description": "MDLERIPHYLLIN INJ", "lot_batch_number": "NB00189A",
             "quantity": "70", "unit_price": "8.06", "total_amount": "564.20"},
            {"product_description": "SUPPJONAC SUPPOS 100MG", "lot_batch_number": "TB0490A",
             "quantity": "6", "unit_price": "108.54", "total_amount": "651.24"},
            {"product_description": "MCOLMBIMIST L RESPULES", "lot_batch_number": "EB60053",
             "quantity": "2000", "unit_price": "10.45", "total_amount": "20900.00"},
            {"product_description": "AMNPUCOXIA INJ", "lot_batch_number": "CTB1012A",
             "quantity": "100", "unit_price": "183.33", "total_amount": "18333.00"},
            {"product_description": "ARZEP NASAL SPARY", "lot_batch_number": "MA06836A",
             "quantity": "5", "unit_price": "403.70", "total_amount": "2018.50"},
            {"product_description": "10 ATORVA 80 TAB", "lot_batch_number": "IA01384C",
             "quantity": "10", "unit_price": "296.96", "total_amount": "2969.60"},
            {"product_description": "10 CLOPITORVA 20 CAP", "lot_batch_number": "IB00722A",
             "quantity": "50", "unit_price": "248.98", "total_amount": "12449.00"},
            {"product_description": "10*15*sTENGLYN TAB", "lot_batch_number": "IB00537A",
             "quantity": "20", "unit_price": "62.71", "total_amount": "1254.20"},
        ]
        out = fix_chaitanya_pharma_line_items_from_ocr(items, ocr)
        by_batch = {i["lot_batch_number"]: i["product_description"] for i in out}
        self.assertEqual(by_batch["NB00189A"], "DERIPHYLLIN INJ")
        self.assertEqual(by_batch["TB0490A"], "JONAC SUPPOS 100MG")
        self.assertEqual(by_batch["EB60053"], "COMBIMIST L RESPULES")
        self.assertEqual(by_batch["CTB1012A"], "NUCOXIA INJ")
        self.assertEqual(by_batch["MA06836A"], "ARZEP NASAL SPRAY")
        self.assertEqual(by_batch["IA01384C"], "ATORVA 80 TAB")
        self.assertEqual(by_batch["IB00722A"], "CLOPITORVA 20 CAP")
        self.assertEqual(by_batch["IB00537A"], "TENGLYN TAB")

    def test_s21074_pack_fused_product_names(self):
        """S 21074: pdfplumber glues PKG into product (3MILNAC, TINPROVIDAC, 'STRAMAZAC)."""
        ocr = """
        TAX INVOICE
        CHAITANYA PHARMA
        BENSUS PHARMA Tax Inv. No.: S 21074
        MFD. QTY FREEPKG DESCRIPTION HSN Code BATCH # EXP MRP RATE VALUE DIS% GST%NET AMOUNT
        ZYDUS CRZ5A0 0 5*10*3MILNAC INJ 30049059 *NA00385A 10/28 5.40 4.10 205.00 6.00 5.00 202.34 Bill No.
        ZYDUS CRZ1A0 0 14'S TINPROVIDAC CAP 30049099 *IA0789A 10/27 496.44 327.98 3279.80 6.00 5.00 3237.17 21074
        ZYDUS CR1Z0A0 0 10*15'S PANTODAC DSR CAP 30049099 *IA01873A 10/27 299.40 171.33 17133.00 6.00 5.00 16910.28 16-02-2026
        ZYDUS CRZ3A0 0 15*2*10'STRAMAZAC 50 MG CAP 30049069 *IA01885A 05/28 51.45 37.28 1118.40 6.00 5.00 1103.86 Bill No.
        """
        items = [
            {"product_description": "MILNAC INJ", "lot_batch_number": "*NA00385A",
             "quantity": "50", "unit_price": "4.10", "total_amount": "205.00"},
            {"product_description": "TINPROVIDAC CAP", "lot_batch_number": "*IA0789A",
             "quantity": "10", "unit_price": "327.98", "total_amount": "3279.80"},
            {"product_description": "PANTODAC DSR CAP", "lot_batch_number": "*IA01873A",
             "quantity": "100", "unit_price": "171.33", "total_amount": "17133.00"},
            {"product_description": "'STRAMAZAC 50 MG CAP", "lot_batch_number": "*IA01885A",
             "quantity": "30", "unit_price": "37.28", "total_amount": "1118.40"},
        ]
        out = fix_chaitanya_pharma_line_items_from_ocr(items, ocr)
        by_batch = {i["lot_batch_number"]: i["product_description"] for i in out}
        self.assertEqual(by_batch["*NA00385A"], "INAC INJ")
        self.assertEqual(by_batch["*IA0789A"], "PROVIDAC CAP")
        self.assertEqual(by_batch["*IA01873A"], "PANTODAC DSR CAP")
        self.assertEqual(by_batch["*IA01885A"], "TRAMAZAC 50 MG CAP")


if __name__ == "__main__":
    unittest.main()
