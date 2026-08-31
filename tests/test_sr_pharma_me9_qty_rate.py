"""SR. PHARMACEUTICALS CREDIT TAX INVOICE: Qty/Rate after Mrp (empty Free)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    enforce_schema,
    extract_sr_pharma_me9_party_details,
    fix_sr_pharma_me9_qty_rate_from_ocr,
    ocr_suggests_sr_pharma_me9_credit_invoice,
    recover_missing_items_from_ocr,
    recover_sr_pharma_me9_line_items_from_ocr,
    _apply_sr_pharma_multipage_ocr_line_items,
    _parse_sr_pharma_me9_qty_rate_rows,
)


SR_OCR = """
SR. PH ARM ACEUSiieastase MED POINT TAX INVOICE
SHOP NO.4,5,6 SHELTER ARCADE MAICKY ROAD,RANCHI-834001
Sale Type : CREDIT Invoice No : A002135
GSTIN : 20AOLPK7893L1ZD
1 | NUCOXIA 60 110 | 300490, 1B00802A, 3/28 | 276.22 60 187.51 | 0.00 | 250 | 281.27 | 250 | 281.27 11250.60 | CADIL
2 | NUCOXIA MR 18 300490) 1B00458A, 3/28 | 374.26 36 258.77 | 0.00 | 250 | 232.89 | 250 | 232.89 9315.72 | CADIL
3 | VINGLYN 50 MG ZYDUS 300490, AXC1003 12/27} 123.19 20 77.01 | 0.00 | 2.50 38.51 | 250 38.51 1540.20 | CADIL
4 | NASOCLEAR 20ML | 300490, TBO324A 229) 72.98 1360 40.53 | 0.00 | 2.50 | 1378.02 | 250 | 1378.02 5120.80 | MERC
5 | AMLODAC 10 110 | 300490, 1B00835A, 4/28) 175.64 500 119.24 | 0.00 | 2.50 | 1490.50 | 2.50 | 1490.50 59620.00 | ZYDUS
6 | AMLODAC 2.5 110 | 300490) 1B00510A. 2128) 57.33 720 35.77 | 0.00 | 250 | 643.86 | 250 | 643.86 25754.40 | ZYDUS.
7 | AMLODAC5 110 | 300490 1B00817C 2/28) 80.52 3780 50.06 | 0.00 | 250 | 4730.67 | 2.50 | 4730.67 | 189226.80 | ZYDUS
8 | AMLODAC AT 1°10 | 300490, 1B00519A, 2/28 | 264.41 500 145.76 | 0.00 | 2.50 | 1822.00 | 2.50 | 1822.00 72880.00 | ZYDUS
9 | AMLODAC CH TAB 610 | 300490, AFAH2601 2/28 | 183.59 42 124.63 | 0.00 | 2.50 | 130.86 | 250 | 130.86 5234.46 | ZYDUS
10] AMLODAC CH TAB 610 | 300490, AFAH2602 2/28 | 183.59 18 124.63 | 0.00 | 2.50 56.08 | 2.50 56.08 2243.34 | ZYDUS:
13] AMLODACT 115 | 300490, BEC1088A 2/28 | 257.65 100 160.80 | 0.00 | 2.50 | 402.00 | 250 | 402.00 16080.00 | ZYDUS
16] TENGLYN M 1000 1°15 _| 300490) 1A01949A, 11/27} 280.81 50 172,00 | 0.00 | 2.50 | 215.00 | 250 | 215.00 8600.00 | ZYDUS
17] TENGLYN TAB 11. | 300490 1Bo0537a | 1/28] 17689 | 300 108.33 |0.00 | 250 | 812.48 | 250 | 812.48 | 3249900 | zvDUS
18] THROMBOPHOB OINTMEMT 30gm | 300420 1B00930A | 3/29] 23680 | 50 146.41 | 0.00 | 250 | 193.01 | 250 | 18301 | 7320.50 | zvpuS
19] VINGLYN M 500 TAB 415 | 30040q axctoio | 228| 173.08 | 20 10438 | 0.00 | 250 | 5219 | 250 | 5219 | 2087.60 | zvous
20] AMLODAC D 110 | 300400 m2626002 | 1/28] 171.85 | 45 11666 | 0.00 | 250 | 131.24 | 250 | 131.24 | 5249.70 | zvous
Our ONLINE ORDER Codg 448469 me. in/beb
Total Qty.: 8041 Total Items : 22 Grand Total 601279.00
FOR SR. PHARMACEUTICALS
"""

JACKSON_OCR = "JACKSON MEDICALS Inv.No. D7655 Qty Rate Amount"


def _item(desc, qty, rate, total, batch=""):
    return {
        "product_description": desc,
        "quantity": qty,
        "unit_price": rate,
        "total_amount": total,
        "lot_batch_number": batch,
        "additional_fields": {},
    }


class TestSrPharmaMe9QtyRate(unittest.TestCase):
    def test_detects_sr_pharma_format(self):
        self.assertTrue(ocr_suggests_sr_pharma_me9_credit_invoice(SR_OCR))
        self.assertTrue(ocr_suggests_sr_pharma_me9_credit_invoice(
            "", vendor="SR. PHARMACEUTICALS"))
        self.assertFalse(ocr_suggests_sr_pharma_me9_credit_invoice(JACKSON_OCR))
        self.assertFalse(ocr_suggests_sr_pharma_me9_credit_invoice(
            "NATIONAL PHARMACEUTICALS ITEM NAME & PACK NET RATE RATE C.D%"))

    def test_parse_qty_not_mrp_after_empty_free(self):
        rows = _parse_sr_pharma_me9_qty_rate_rows(SR_OCR)

        nucoxia = next(r for r in rows if "NUCOXIA 60" in r["product_description"].upper())
        self.assertEqual(nucoxia["quantity"], 60.0)
        self.assertAlmostEqual(nucoxia["unit_price"], 187.51, places=2)

        nasoclear = next(r for r in rows if "NASOCLEAR" in r["product_description"].upper())
        self.assertEqual(nasoclear["quantity"], 1360.0)
        self.assertAlmostEqual(nasoclear["unit_price"], 40.53, places=2)

        amlodac5 = next(
            r for r in rows
            if re_key(r["product_description"]) == "AMLODAC5"
        )
        self.assertEqual(amlodac5["quantity"], 3780.0)
        self.assertAlmostEqual(amlodac5["unit_price"], 50.06, places=2)

        amlodac25 = next(
            r for r in rows if "AMLODAC 2.5" in r["product_description"].upper()
        )
        self.assertEqual(amlodac25["quantity"], 720.0)
        self.assertAlmostEqual(amlodac25["unit_price"], 35.77, places=2)

        tenglyn1000 = next(
            r for r in rows if "TENGLYN M 1000" in r["product_description"].upper()
        )
        self.assertEqual(tenglyn1000["quantity"], 50.0)
        self.assertAlmostEqual(tenglyn1000["unit_price"], 172.00, places=2)

        vinglyn500 = next(
            r for r in rows if "VINGLYN M 500" in r["product_description"].upper()
        )
        self.assertEqual(vinglyn500["quantity"], 20.0)
        self.assertAlmostEqual(vinglyn500["unit_price"], 104.38, places=2)

    def test_fix_restores_swapped_and_mrp_as_qty(self):
        items = [
            _item("NUCOXIA 60", "187.51", "60", "11250.60", "IB00802A"),
            _item("NASOCLEAR", "72.98", "40.53", "55120.80", "TB0324A"),
            _item("AMLODAC 5", "80.52", "50.06", "189226.80", "IB00817C"),
            _item("AMLODAC 2.5", "57.33", "720", "25754.40", "IB00510A"),
            _item("TENGLYN TAB", "176.89", "108.33", "32499.00", "IB00537A"),
        ]
        out = fix_sr_pharma_me9_qty_rate_from_ocr(
            items, SR_OCR, vendor="SR. PHARMACEUTICALS"
        )
        self.assertEqual(out[0]["quantity"], "60")
        self.assertEqual(out[0]["unit_price"], "187.51")
        self.assertEqual(out[1]["quantity"], "1360")
        self.assertEqual(out[1]["unit_price"], "40.53")
        self.assertEqual(out[2]["quantity"], "3780")
        self.assertEqual(out[2]["unit_price"], "50.06")
        self.assertEqual(out[3]["quantity"], "720")
        self.assertEqual(out[3]["unit_price"], "35.77")
        self.assertEqual(out[4]["quantity"], "300")
        self.assertEqual(out[4]["unit_price"], "108.33")

    def test_enforce_schema_restores_qty_rate(self):
        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "SR. PHARMACEUTICALS",
                    "customer": "MED POINT",
                    "invoice_no": "A002135",
                    "total": "601279.00",
                },
                "line_items": {"items": [
                    _item("NUCOXIA 60", "276.22", "60", "11250.60", "IB00802A"),
                    _item("NASOCLEAR 20ML", "10", "40.53", "55120.80", "TB0324A"),
                    _item("AMLODAC 5", "1", "189226.80", "189226.80", "IB00817C"),
                ], "count": 3},
                "ocr_text": SR_OCR,
            }
        }
        out = enforce_schema(payload)
        items = out["data"]["line_items"]["items"]
        nucoxia = next(i for i in items if "NUCOXIA 60" in i["product_description"].upper())
        self.assertEqual(str(int(float(nucoxia["quantity"]))), "60")
        self.assertEqual(float(nucoxia["unit_price"]), 187.51)
        nasoclear = next(i for i in items if "NASOCLEAR" in i["product_description"].upper())
        self.assertEqual(str(int(float(nasoclear["quantity"]))), "1360")
        self.assertEqual(float(nasoclear["unit_price"]), 40.53)
        amlodac5 = next(i for i in items if "AMLODAC 5" in i["product_description"].upper()
                        or re_key(i["product_description"]) == "AMLODAC5")
        self.assertEqual(str(int(float(amlodac5["quantity"]))), "3780")
        self.assertEqual(float(amlodac5["unit_price"]), 50.06)

    def test_recover_products_when_gemini_returns_none(self):
        items = recover_sr_pharma_me9_line_items_from_ocr([], SR_OCR)
        names = [i["product_description"].upper() for i in items]
        self.assertGreaterEqual(len(items), 14)
        self.assertTrue(any("NUCOXIA 60" in n for n in names))
        self.assertTrue(any("NASOCLEAR" in n for n in names))
        self.assertTrue(any("AMLODAC 5" in n or n.replace(" ", "") == "AMLODAC5"
                            for n in names))
        self.assertTrue(any("TENGLYN TAB" in n for n in names))
        nucoxia = next(i for i in items if "NUCOXIA 60" in i["product_description"].upper())
        self.assertEqual(str(int(float(nucoxia["quantity"]))), "60")
        self.assertEqual(float(nucoxia["unit_price"]), 187.51)

    def test_generic_fix9_uses_sr_pharma_parser(self):
        items = recover_missing_items_from_ocr([], SR_OCR)
        self.assertGreaterEqual(len(items), 14)
        self.assertEqual(
            recover_missing_items_from_ocr([], JACKSON_OCR),
            [],
        )

    def test_enforce_schema_empty_items_recovers_products(self):
        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "SR. PHARMACEUTICALS",
                    "customer": "MED POINT",
                    "invoice_no": "A002135",
                    "total": "601279.00",
                },
                "line_items": {"items": [], "count": 0},
                "ocr_text": SR_OCR,
            }
        }
        out = enforce_schema(payload)
        items = out["data"]["line_items"]["items"]
        self.assertGreaterEqual(len(items), 14)
        names = [i["product_description"].upper() for i in items]
        self.assertTrue(any("NUCOXIA 60" in n for n in names))
        self.assertTrue(any("NASOCLEAR" in n for n in names))
        self.assertTrue(any("AMLODAC" in n and "3780" == str(int(float(i["quantity"])))
                            for i, n in zip(items, names)))

    def test_multipage_ocr_fills_extracted_data(self):
        group = {"ocr_text": SR_OCR, "extracted_data": None}
        self.assertTrue(_apply_sr_pharma_multipage_ocr_line_items(group))
        items = (group["extracted_data"] or {}).get("line_items") or []
        self.assertGreaterEqual(len(items), 14)
        self.assertFalse(_apply_sr_pharma_multipage_ocr_line_items(
            {"ocr_text": JACKSON_OCR, "extracted_data": None}))

    def test_infers_qty_when_ocr_eats_qty_as_percent(self):
        ocr = (
            "SR. PHARMACEUTICALS SHELTER ARCADE GSTIN : 20AOLPK7893L1ZD\n"
            "12] AMLODAC M 60ML1) 300490 SB00355A 4/28 | 231.35 3% 144.50 | "
            "0.00 | 250 | 130.05 | 250 | 130.05 5202.00 | ZYDUS\n"
        )
        rows = _parse_sr_pharma_me9_qty_rate_rows(ocr)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantity"], 36.0)
        self.assertAlmostEqual(rows[0]["unit_price"], 144.50, places=2)

    def test_name_key_strips_company_so_rows_are_not_duplicated(self):
        ocr = (
            "SR. PHARMACEUTICALS SHELTER ARCADE GSTIN : 20AOLPK7893L1ZD\n"
            "3 | VINGLYN 50 MG ZYDUS 300490, AXC1003 12/27} 123.19 20 77.01 | "
            "0.00 | 2.50 38.51 | 250 38.51 1540.20 | CADIL\n"
            "3 | VINGLYN 50 MG 300490, AXC1003 12/27} 123.19 20 77.01 | "
            "0.00 | 2.50 38.51 | 250 38.51 1540.20 | CADIL\n"
        )
        rows = _parse_sr_pharma_me9_qty_rate_rows(ocr)
        self.assertEqual(len(rows), 1)

    def test_enforce_schema_keeps_vendor_and_products(self):
        """Shared GSTIN scoring must not wipe vendor (importers drop products)."""
        ocr = (
            SR_OCR
            + "\nGST No. : 20ABCFM8681Q1ZQ\n"
            + "This Bill : 601279.00\nGrand Total 601279.00\n"
            + "Date : 08-06-2026 Due Date : 08-06-2026\n"
        )
        payload = {
            "data": {
                "invoice_no": "A002135",
                "vendor": "SR. PHARMACEUTICALS",
                "customer": "MED POINT",
                "line_items": [],
                "ocr_text": ocr,
            }
        }
        out = enforce_schema(payload)
        summary = out["data"]["invoice_summary"]
        self.assertEqual(summary["vendor"], "SR. PHARMACEUTICALS")
        self.assertNotEqual(summary["vendor"].upper(), "NONE")
        self.assertEqual(summary["vendor_gstin"], "20AOLPK7893L1ZD")
        self.assertEqual(summary["customer_gstin"], "20ABCFM8681Q1ZQ")
        self.assertEqual(summary["customer"], "MED POINT")
        self.assertEqual(summary["invoice_date"], "2026-06-08")
        self.assertEqual(float(summary["total"]), 601279.00)
        items = out["data"]["line_items"]["items"]
        self.assertGreaterEqual(len(items), 14)
        names = [i["product_description"].upper() for i in items]
        self.assertTrue(any("NUCOXIA 60" in n for n in names))
        self.assertTrue(any("NASOCLEAR" in n for n in names))

    def test_party_details_from_ocr(self):
        ocr = (
            "SR. PHARMACEUTICALS SHELTER ARCADE GSTIN : 20AOLPK7893L1ZD\n"
            "MED POINT HEALTH POINT HOSPITAL CAMPUS BARIYATU\n"
            "GST No. : 20ABCFM8681Q1ZQ Date : 08-06-2026\n"
            "This Bill : 601279.00 Grand Total 601279.00\n"
        )
        d = extract_sr_pharma_me9_party_details(ocr)
        self.assertEqual(d["vendor"], "SR. PHARMACEUTICALS")
        self.assertEqual(d["vendor_gstin"], "20AOLPK7893L1ZD")
        self.assertEqual(d["customer"], "MED POINT")
        self.assertEqual(d["customer_gstin"], "20ABCFM8681Q1ZQ")
        self.assertEqual(d["invoice_date"], "2026-06-08")
        self.assertEqual(d["total"], "601279.00")


def re_key(name: str) -> str:
    import re
    cleaned = re.sub(r'\s+', ' ', str(name or '').strip(' |'))
    cleaned = re.sub(r'\s+\d{3}$', '', cleaned)
    return re.sub(r'[^A-Z0-9]+', '', cleaned.upper())


if __name__ == "__main__":
    unittest.main()
