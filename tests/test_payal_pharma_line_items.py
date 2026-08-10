"""PAYAL PHARMA columnar layout: dedupe names, realign qty/rate from OCR bands."""
import unittest

from app import (
    ocr_suggests_payal_pharma,
    _dedupe_payal_product_name,
    extract_payal_pharma_line_items_from_ocr,
    fix_payal_pharma_line_items_from_ocr,
)


PAYAL_OCR = """
PAYAL PHARMA.
19091 THROMBOPHOB OINT
049094 THROMBOPHOB GEL (GREEN)
Udy THROMBOPHOB GEL (GREEN)
30049074 AMLODAC M
MEVA C
NASOCLEAR NASAL SPRAY
COMBIMIST-L RESPULES
COMBIMIST-L RESPULES
FORMONIDE 0.5 RESPULES
ATORVA 10MG TAB
PROLUTON DEPOT 500MG AMPS - INJ
TESTOVIRON 250MG DEPOT
TRAMAZAC CAP
COMPLAMINA 150MG TAB
LIPAGLYN 4MG TAB
COMPLAMINA AMPS
DERINIDE 0.5MG RESPULES
DERINIDE 0.5MG RESPULES
NUPATCH 200MG PATCHES
JONAC SUPPOS 100MG TAB
SENSORCAINE 0.5% HEAVY INJ
30 DOXOLIN TAB
PACK.
MFGR EXP. BATCH NO.
IBOO846A
'B00992A
1BO1180A
SB00198A
1A0 19964,
TBO293A
EAG0002
EBG0050
26R027
14014328
GBO22BA
GBO485A
IBOO861A
SBO00212A
(BO0780A
CTC 1001
Fiso242
FK50307
CNC1010
TBO224A
FF5003
1BOO856A
ary
100
46
14
12
20
100
300
400
560
60
50
30
210
200
20
100
580
5420
100
S51
300
20
SCH
MRP
AMOUNT
199049.75
TAXABLE
199049.75
ORIGINAL FOR RECIPIENT
TAX INVOICE
SMan : 346-RC Visit: ALL
T Prod 19 TOTY 8653
Total Amt 209002.33
TO PAY 209002.00
In Words Rupees : TWO LAKH NINE THOUSAND TWO ONLY.
"""


class TestPayalPharmaLineItems(unittest.TestCase):
    def test_detects_payal_format(self):
        self.assertTrue(ocr_suggests_payal_pharma(PAYAL_OCR, "PAYAL PHARMA."))
        self.assertTrue(ocr_suggests_payal_pharma(PAYAL_OCR, "PAVAL PHARMA."))
        self.assertFalse(ocr_suggests_payal_pharma(
            "SUPREME LIFE SCIENCES MARG ERP NANO", ""))

    def test_dedupe_repeated_product_name(self):
        self.assertEqual(
            _dedupe_payal_product_name("THROMBOPHOB OINT THROMBOPHOB OINT"),
            "THROMBOPHOB OINT",
        )
        self.assertEqual(
            _dedupe_payal_product_name("AMLODAC M AMLODAC M"),
            "AMLODAC M",
        )
        self.assertEqual(
            _dedupe_payal_product_name("THROMBOPHOB GEL (GREEN)"),
            "THROMBOPHOB GEL (GREEN)",
        )

    def test_extracts_ocr_columns(self):
        rows = extract_payal_pharma_line_items_from_ocr(PAYAL_OCR)
        self.assertEqual(len(rows), 22)
        self.assertEqual(rows[0]["product_description"], "THROMBOPHOB OINT")
        self.assertEqual(rows[0]["quantity"], "100")
        self.assertEqual(rows[17]["quantity"], "5420")
        self.assertEqual(rows[19]["quantity"], "51")
        self.assertEqual(rows[-1]["product_description"], "DOXOLIN TAB")

    def test_fixes_duplicate_names_and_swapped_rate(self):
        items = [
            {
                "product_description": "THROMBOPHOB OINT THROMBOPHOB OINT",
                "quantity": "100",
                "unit_price": "129.96",
                "total_amount": "12996.00",
                "lot_batch_number": "IB00846A",
                "additional_fields": {"mrp": "236.80"},
            },
            {
                "product_description": "THROMBOPHOB GEL (GREEN)",
                "quantity": "46",
                "unit_price": "224.25",
                "total_amount": "10315.50",
                "lot_batch_number": "IB00992A",
                "additional_fields": {"mrp": "337.17"},
            },
            {
                "product_description": "THROMBOPHOB GEL (GREEN)",
                "quantity": "14",
                "unit_price": "224.25",
                "total_amount": "3139.50",
                "lot_batch_number": "IB01180A",
                "additional_fields": {"mrp": "337.17"},
            },
            {
                "product_description": "AMLODAC M",
                "quantity": "12",
                "unit_price": "144.22",
                "total_amount": "1730.64",
                "lot_batch_number": "SB00198A",
                "additional_fields": {"mrp": "210.32"},
            },
            {
                "product_description": "MEVA C",
                "quantity": "20",
                "unit_price": "241.75",
                "total_amount": "4835.00",
                "lot_batch_number": "IA01996A",
                "additional_fields": {"mrp": "365.57"},
            },
            {
                "product_description": "NASOCLEAR NASAL SPRAY",
                "quantity": "100",
                "unit_price": "40.94",
                "total_amount": "4094.00",
                "lot_batch_number": "TB0293A",
                "additional_fields": {"mrp": "72.98"},
            },
            {
                "product_description": "COMBIMIST-L RESPULES",
                "quantity": "300",
                "unit_price": "12.60",
                "total_amount": "3780.00",
                "lot_batch_number": "EA50002",
                "additional_fields": {"mrp": "26.25"},
            },
            {
                "product_description": "COMBIMIST-L RESPULES",
                "quantity": "400",
                "unit_price": "12.60",
                "total_amount": "5040.00",
                "lot_batch_number": "EB50050",
                "additional_fields": {"mrp": "26.25"},
            },
            {
                "product_description": "FORMONIDE 0.5 RESPULES",
                "quantity": "560",
                "unit_price": "42.35",
                "total_amount": "23716.00",
                "lot_batch_number": "26R027",
                "additional_fields": {"mrp": "79.41"},
            },
            {
                "product_description": "ATORVA 10MG TAB",
                "quantity": "60",
                "unit_price": "42.37",
                "total_amount": "2542.20",
                "lot_batch_number": "IA01432B",
                "additional_fields": {"mrp": "79.63"},
            },
            {
                "product_description": "PROLUTON DEPOT 500MG AMPS - INJ",
                "quantity": "50",
                "unit_price": "285.45",
                "total_amount": "14272.50",
                "lot_batch_number": "GB0228A",
                "additional_fields": {"mrp": "462.53"},
            },
            {
                "product_description": "TESTOVIRON 250MG DEPOT",
                "quantity": "30",
                "unit_price": "325.74",
                "total_amount": "9772.20",
                "lot_batch_number": "GB0485A",
                "additional_fields": {"mrp": "517.32"},
            },
            {
                "product_description": "TRAMAZAC CAP",
                "quantity": "210",
                "unit_price": "35.28",
                "total_amount": "7408.80",
                "lot_batch_number": "IB00861A",
                "additional_fields": {"mrp": "51.76"},
            },
            {
                "product_description": "COMPLAMINA 150MG TAB",
                "quantity": "200",
                "unit_price": "36.14",
                "total_amount": "7228.00",
                "lot_batch_number": "SB00212A",
                "additional_fields": {"mrp": "52.18"},
            },
            {
                "product_description": "LIPAGLYN 4MG TAB",
                "quantity": "20",
                "unit_price": "381.80",
                "total_amount": "7636.00",
                "lot_batch_number": "IB00780A",
                "additional_fields": {"mrp": "501.13"},
            },
            {
                "product_description": "COMPLAMINA AMPS",
                "quantity": "100",
                "unit_price": "20.71",
                "total_amount": "2071.00",
                "lot_batch_number": "CTC1001",
                "additional_fields": {"mrp": "29.90"},
            },
            {
                "product_description": "DERINIDE 0.5MG RESPULES",
                "quantity": "580",
                "unit_price": "8.89",
                "total_amount": "5156.20",
                "lot_batch_number": "FI50242",
                "additional_fields": {"mrp": "22.91"},
            },
            {
                "product_description": "DERINIDE 0.5MG RESPULES",
                "quantity": "5420",
                "unit_price": "8.89",
                "total_amount": "48183.80",
                "lot_batch_number": "FK50307",
                "additional_fields": {"mrp": "22.91"},
            },
            {
                "product_description": "NUPATCH 200MG PATCHES",
                "quantity": "100",
                "unit_price": "334.38",
                "total_amount": "33438.00",
                "lot_batch_number": "CNC1010",
                "additional_fields": {"mrp": "195.05"},
            },
            {
                "product_description": "JONAC SUPPOS 100MG TAB",
                "quantity": "51",
                "unit_price": "133.75",
                "total_amount": "13375.00",
                "lot_batch_number": "TB0224A",
                "additional_fields": {"mrp": "149.74"},
            },
            {
                "product_description": "SENSORCAINE 0.5% HEAVY INJ",
                "quantity": "300",
                "unit_price": "14.62",
                "total_amount": "4386.00",
                "lot_batch_number": "FF5003",
                "additional_fields": {"mrp": "31.57"},
            },
            {
                "product_description": "DOXOLIN TAB",
                "quantity": "20",
                "unit_price": "104.11",
                "total_amount": "2082.20",
                "lot_batch_number": "IB00856A",
                "additional_fields": {"mrp": "143.83"},
            },
        ]
        out = fix_payal_pharma_line_items_from_ocr(
            items, PAYAL_OCR, "PAYAL PHARMA.")
        self.assertEqual(out[0]["product_description"], "THROMBOPHOB OINT")
        self.assertEqual(out[18]["unit_price"], "133.75")
        self.assertEqual(out[18]["total_amount"], "13375.00")
        self.assertEqual(out[18]["quantity"], "100")
        # Stolen NUPATCH rate must not remain on JONAC
        self.assertNotEqual(out[19]["unit_price"], "133.75")
        self.assertEqual(out[19]["quantity"], "51")
        self.assertEqual(out[17]["quantity"], "5420")


if __name__ == "__main__":
    unittest.main()
