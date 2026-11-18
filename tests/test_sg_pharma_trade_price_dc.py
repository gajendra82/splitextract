"""SG PHARMA Tax Inv / *DC* / Trade Price: restore rate/amount from Taxable column."""
import unittest

from app import (
    _ocr_suggests_sg_pharma_trade_price_dc,
    _parse_sg_pharma_csquare_qty_rate_rows,
    _parse_sg_pharma_trade_price_dc_rows,
    fix_sg_pharma_csquare_qty_rate_from_ocr,
    fix_sg_pharma_trade_price_dc_summary_from_ocr,
)


# Compact pdfplumber line (order-no glued into MRP/Rate; Taxable/Net intact)
SG_DC_OCR = """
SG PHARMA DISTRIBUTORS PVT LTD TAX INVOICE
Tax Inv.No. :26000730038858
M/S VRINDA MEDICARE PVT LTD (A UNIT OF PUSHPANJALI MEDICO9)
Store :STR002 GSTIN:09AAECV7274N1ZI FSSAI :
GSTIN: 07ABHCS9108C1ZE Pan No.:ABHCS9108C
Quantity Billed Free Batch Exp M.R.P Trade Price Disc Taxable Net Value
ZYD *DC* THROMBOPHOB OINT 30GM 1X30GM30049099 160 0 IB01572A 0 5 - 2 9 2600 203860.800086515118.41 0 0.00 0.00 18945.60 5 947.28 19892.88
ZYD *DC* THROMBOPHOB GEL 30GM 1X30GM30049099 160 0 IB01938A 0 6 - 2 9 2600303870.107086514179.82 0 0.00 0.00 28771.20 5 1438.56 30209.76
ZYD *DC* TENGLYN M 1GM (15)TAB 1X15 30049099 10 0 IB00974A 0 3 - 2 8 2600208800.8010865121073.72 0 0.00 0.00 1737.20 5 86.86 1824.06
ZYD *DC* PENEGRA 50MG TAB [211.30] 1X4 30049099 20 0 BRB1086 0 3 - 2 7 260109088.000908651373.29 0 0.00 0.00 1465.80 5 73.29 1539.09
ZYD *DC* PENEGRA 100MG TAB 1X4 30049099 20 0 BRC1098 0 4 - 2 8 260404028.0401086511268.53 0 0.00 0.00 3370.60 5 168.53 3539.13
Tot Items:5 Tot Qty :370
IRN No: :
Grand Total 57005.00
"""

SG_DC_OCR_PAGE2 = """
SG PHARMA DISTRIBUTORS PVT LTD TAX INVOICE
Tax Inv.No. :26000730038755
GSTIN:09AAECV7274N1ZI FSSAI :
GSTIN: 07ABHCS9108C1ZE Pan No.:ABHCS9108C
ZYD *DC* HYOCIMAX S TAB 1X10 30049099 20 0 IB01725A 0 6 - 2 8 26000 6890.90808647439.89 0 0.00 0.00 797.80 5 39.89 837.69
ZYD *DC* NASOCLEAR NASAL DROP 20ML 1X20ML30049069 100 0 DBC1052 0 4 - 2 9 2600 7028.0908086471460.87 0 0.00 0.00 4087.00 5 204.35 4291.35
ZYD *DC* NUCOXIA MR 4MG TAB 1X10 30049069 48 0 IB01857A 0 6 - 2 8 2600307840.2060864712028.30 0 0.00 0.00 10958.40 5 547.92 11506.32
ZYD *DC* GRD BIX 250GM 1X250GM19059020 10 0 BHC1142 1 1 - 2 7 260 504048.05000864731654.82 0 0.00 0.00 3648.20 5 182.41 3830.61
ZYD *DC* NEBULA 5MG TAB 1X10 30049079 30 0 BEC1006 1 2 - 2 7 260109048.0203086471209.81 0 0.00 0.00 3294.30 5 164.72 3459.02
Tot Items:17 Tot Qty :432
IRN No: :19b16955a1f068eac2f9862a725d47037a71c92af95de55b753b07ad0c6509e4
Grand Total 59186.00
"""

# Classic C-Square pipe layout must NOT be treated as *DC* Trade Price
SG_CSQUARE_OCR = """
SG PHARMA DISTRIBUTORS PVT LTD Bill To : WELLO RETAIL PRIVATE LIMITED
TAX INV NO : 2600073002053
| 30043919 DECA DURABOLIN 50MG INJ 1X1 GB0124A 12-29 10 0.00 508.13 290.95 0.00 2909.50 5.0 ZYDU
| 30049079 ATORVA 40MG TAB 1X10 IB00787A 03-29 180 0.00 207.50 114.80 0.00 20664.00 5.0 ZYDU
| 30049079 CLOPITORVA 40MG TAB 1X10 CPR26001 12-27 10 0.00 390.23 240.73 0.00 2407.30 5.0 ZYDU
Tot Items:3 Tot Qty:200
Software by C-Square Info Solution Pvt Ltd.
"""


class TestSgPharmaTradePriceDc(unittest.TestCase):
    def test_detects_dc_layout_not_csquare_pipe(self):
        self.assertTrue(_ocr_suggests_sg_pharma_trade_price_dc(SG_DC_OCR))
        self.assertFalse(_ocr_suggests_sg_pharma_trade_price_dc(SG_CSQUARE_OCR))

    def test_csquare_parser_unchanged_on_pipe_layout(self):
        rows = _parse_sg_pharma_csquare_qty_rate_rows(SG_CSQUARE_OCR)
        self.assertEqual(len(rows), 3)
        deca = next(r for r in rows if "DECA" in r["product_description"].upper())
        self.assertEqual(deca["quantity"], 10.0)
        self.assertAlmostEqual(deca["unit_price"], 290.95, places=2)

    def test_parse_taxable_rate_from_glued_order_no(self):
        rows = _parse_sg_pharma_trade_price_dc_rows(SG_DC_OCR)
        self.assertEqual(len(rows), 5)
        by_batch = {r["lot_batch_number"]: r for r in rows}
        self.assertEqual(by_batch["IB01572A"]["quantity"], 160.0)
        self.assertAlmostEqual(by_batch["IB01572A"]["unit_price"], 118.41, places=2)
        self.assertAlmostEqual(by_batch["IB01572A"]["total_amount"], 18945.60, places=2)
        self.assertAlmostEqual(by_batch["BRB1086"]["unit_price"], 73.29, places=2)
        self.assertAlmostEqual(by_batch["BRC1098"]["total_amount"], 3370.60, places=2)

    def test_parse_hsn19_and_large_qty(self):
        rows = _parse_sg_pharma_trade_price_dc_rows(SG_DC_OCR_PAGE2)
        self.assertEqual(len(rows), 5)
        by_batch = {r["lot_batch_number"]: r for r in rows}
        self.assertEqual(by_batch["DBC1052"]["quantity"], 100.0)
        self.assertAlmostEqual(by_batch["DBC1052"]["unit_price"], 40.87, places=2)
        self.assertEqual(by_batch["BHC1142"]["hsn_code"], "19059020")
        self.assertAlmostEqual(by_batch["BHC1142"]["unit_price"], 364.82, places=2)
        self.assertAlmostEqual(by_batch["IB01857A"]["total_amount"], 10958.40, places=2)

    def test_fix_gemini_glued_rate_amount(self):
        bad = [
            {
                "product_description": "THROMBOPHOB OINT 30GM",
                "quantity": "160",
                "unit_price": "2600.00",
                "total_amount": "203860.80",
                "lot_batch_number": "IB01572A",
                "additional_fields": {"mrp": "2600.00"},
            },
            {
                "product_description": "PENEGRA 50MG TAB [211.30]",
                "quantity": "20",
                "unit_price": "260109088.00",
                "total_amount": "260109088.00",
                "lot_batch_number": "BRB1086",
                "additional_fields": {"mrp": "260109088.00"},
            },
            {
                "product_description": "TENGLYN M 1GM (15)TAB",
                "quantity": "10",
                "unit_price": "208800.80",
                "total_amount": "208800.80",
                "lot_batch_number": "IB00974A",
                "additional_fields": {"mrp": "208800.80"},
            },
        ]
        out = fix_sg_pharma_csquare_qty_rate_from_ocr(
            bad, SG_DC_OCR, vendor="SG PHARMA DISTRIBUTORS PVT LTD"
        )
        self.assertEqual(out[0]["quantity"], "160")
        self.assertEqual(out[0]["unit_price"], "118.41")
        self.assertEqual(out[0]["total_amount"], "18945.60")
        self.assertIsNone((out[0].get("additional_fields") or {}).get("mrp"))
        self.assertEqual(out[1]["unit_price"], "73.29")
        self.assertEqual(out[1]["total_amount"], "1465.80")
        self.assertEqual(out[2]["unit_price"], "173.72")
        self.assertEqual(out[2]["total_amount"], "1737.20")

    def test_summary_gstin_swap_and_customer_digit(self):
        summary = {
            "customer": "M/S VRINDA MEDICARE PVT LTD (A UNIT OF PUSHPANJALI MEDICO9)",
            "customer_gstin": "07ABHCS9108C1ZE",
            "vendor": "SG PHARMA DISTRIBUTORS PVT LTD",
            "vendor_gstin": "09AAECV7274N1ZI",
            "irn": "a0c8aa0dec1aa10f08aad181b08dea05700500eeebaaaeed1c1eca8edde18e0d",
        }
        fix_sg_pharma_trade_price_dc_summary_from_ocr(
            summary, SG_DC_OCR, vendor=summary["vendor"]
        )
        self.assertEqual(summary["vendor_gstin"], "07ABHCS9108C1ZE")
        self.assertEqual(summary["customer_gstin"], "09AAECV7274N1ZI")
        self.assertIn("PUSHPANJALI MEDICO)", summary["customer"])
        self.assertNotIn("MEDICO9", summary["customer"])
        self.assertIsNone(summary["irn"])

    def test_keeps_real_irn(self):
        summary = {
            "customer": "M/S VRINDA MEDICARE PVT LTD (A UNIT OF PUSHPANJALI MEDICO4)",
            "customer_gstin": "07ABHCS9108C1ZE",
            "vendor_gstin": "09AAECV7274N1ZI",
            "irn": "19b16955a1f068eac2f9862a725d47037a71c92af95de55b753b07ad0c6509e4",
        }
        fix_sg_pharma_trade_price_dc_summary_from_ocr(summary, SG_DC_OCR_PAGE2)
        self.assertEqual(
            summary["irn"],
            "19b16955a1f068eac2f9862a725d47037a71c92af95de55b753b07ad0c6509e4",
        )

    def test_non_dc_unchanged(self):
        items = [
            {
                "product_description": "DECA DURABOLIN",
                "quantity": "100",
                "unit_price": "290.95",
                "total_amount": "29095.00",
                "lot_batch_number": "GB0124A",
            }
        ]
        # Pipe C-Square path still applies (not DC); ensure DC parser doesn't break it
        out = fix_sg_pharma_csquare_qty_rate_from_ocr(
            items, SG_CSQUARE_OCR, vendor="SG PHARMA DISTRIBUTORS PVT LTD"
        )
        self.assertEqual(out[0]["quantity"], "10")
        self.assertEqual(out[0]["unit_price"], "290.95")


if __name__ == "__main__":
    unittest.main()
