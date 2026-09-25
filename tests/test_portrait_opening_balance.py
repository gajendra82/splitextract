"""Portrait Sales & Stock: packing stays out of Opening Qty."""

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _apply_total_row_to_result,
    _portrait_balance_items,
    _portrait_balance_split_pack,
    _is_portrait_opening_balance_text,
    _is_swil_opening_receipt_value_statement,
    extract_sales_statement,
)

_ALAYA_PDF = Path(
    r"C:\Users\Prerana Bhalerao\Downloads\ZA_2026_August"
    r"\0000736211_2026_08_ZA_03_302_05092026094339.pdf"
)

# Printed examples. Identity is name + packing, not name alone.
_ALAYA_ROWS = {
    "ARJUNA TAB|60'S": (19, 4191.97, 0, 0.00, 19, 4, 990.48, 15, 3309.45, 0),
    "BONNISAN DROP|30ML": (94, 6221.86, 0, 0.00, 94, 32, 2314.46, 62, 4103.78, 0),
    "BONNISAN SYP|200ML": (104, 10536.24, 0, 0.00, 104, 13, 1485.64, 91, 9219.21, 0),
    "BONNISAN SYP-|100ML": (136, 8173.60, 112, 6731.20, 248, 126, 8271.60, 122, 7332.20, 0),
    "BONNISPAZ DROP|15ML": (19, 1078.06, 0, 0.00, 19, 6, 384.00, 13, 737.62, 0),
    "BRESOL SYRUP|200ML": (11, 1597.31, 28, 4065.88, 39, 2, 327.60, 37, 5372.77, 0),
    "BRESOL TAB|60'S": (21, 3517.71, 50, 8375.50, 71, 23, 4346.08, 48, 8040.48, 0),
    "CYSTONE TAB|60'S": (131, 21501.03, 0, 0.00, 131, 33, 6109.62, 98, 16084.74, 0),
    "CYSTONE FORTE TA|60'S": (47, 10094.66, 14, 3166.60, 61, 28, 6541.56, 33, 7087.74, 0),
    "GALACTOSURE GRA|200GM": (11, 2927.43, 0, 0.00, 11, 1, 300.20, 10, 2661.30, 0),
    "HIMPLASIA TAB|60'S": (49, 16217.53, 0, 0.00, 49, 7, 2613.38, 42, 13900.74, 0),
    "LASUNA TAB|60'S": (55, 12134.65, 0, 0.00, 55, 1, 247.62, 54, 11914.02, 0),
    "LIV 52 DROP|60ML": (76, 7443.44, 0, 0.00, 76, 2, 220.96, 74, 7247.56, 0),
    "LIV 52 DS TAB|60'S": (2083, 395353.40, 0, 0.00, 2083, 544, 111974.30, 1539, 292102.20, 0),
    "MENTAT DS S/F SYP|100ML": (53, 8053.88, 0, 0.00, 53, 0, 0.00, 53, 8053.88, 0),
    "SHUDDHA GUGGUL|60'S": (60, 13848.00, 0, 0.00, 60, 0, 0.00, 60, 13848.00, 0),
}


def _word(x0, x1, y, text):
    return {
        "x0": x0,
        "y0": y,
        "x1": x1,
        "y1": y + 12,
        "t": text,
        "yc": y + 6,
    }


class PortraitOpeningBalanceTests(unittest.TestCase):
    def test_detector_rejects_code_grid(self):
        text = (
            "Sales & Stock Statement PACKING Opening Bal Receipt/Pur "
            "Issue/Sales Closing Near Expiry"
        )
        self.assertTrue(_is_portrait_opening_balance_text(text))
        self.assertFalse(_is_portrait_opening_balance_text(text + " Code PRODUCT"))

    def test_packing_is_not_opening_qty(self):
        header = {
            "y": 100,
            "pack_x0": 250,
            "edges": [400, 520, 620, 760, 860, 960, 1100, 1200, 1340, 1460],
        }
        words = [
            _word(80, 160, 140, "ARJUNA"),
            _word(170, 220, 140, "TAB"),
            _word(260, 310, 140, "60'S"),
            _word(370, 400, 140, "19"),
            _word(450, 520, 140, "4191.97"),
            _word(600, 620, 140, "0"),
            _word(720, 760, 140, "0.00"),
            _word(830, 860, 140, "19"),
            _word(940, 960, 140, "4"),
            _word(1020, 1100, 140, "990.48"),
            _word(1160, 1200, 140, "15"),
            _word(1260, 1340, 140, "3309.45"),
            _word(1440, 1460, 140, "0"),
            _word(80, 180, 180, "SECOND"),
            _word(190, 230, 180, "ROW"),
            _word(260, 320, 180, "30ML"),
            _word(370, 400, 180, "1"),
        ]
        items = _portrait_balance_items(words, header, 400)
        arjuna = items[0]
        self.assertEqual(arjuna["product_name"], "ARJUNA TAB")
        self.assertEqual(arjuna["packing"], "60'S")
        self.assertEqual(arjuna["opening_qty"], 19)
        self.assertEqual(arjuna["opening_value"], 4191.97)
        self.assertEqual(arjuna["sales_qty"], 4)
        self.assertEqual(arjuna["closing_qty"], 15)
        self.assertEqual(arjuna["closing_value"], 3309.45)

    def test_hyphenated_name_stays_distinct(self):
        name, pack = _portrait_balance_split_pack("BONNISAN SYP- 100ML")
        self.assertEqual(name, "BONNISAN SYP-")
        self.assertEqual(pack, "100ML")

    def test_footer_qty_mapper_does_not_shift_portrait_totals(self):
        result = {
            "totals": {
                "opening_qty": 11593,
                "receipts_qty": 845,
                "sales_qty": 3086,
                "closing_qty": 9352,
                "extra": {"extraction_method": "portrait_opening_balance_columns"},
            }
        }
        text = (
            "Opening Receipt Issue Closing\n"
            "GRAND TOTAL 11593 1785244.65 845 105856.09 12438 3086 "
            "501888.88 9352 1434411.82 278"
        )
        kept = _apply_total_row_to_result(result, text)
        self.assertEqual(kept["totals"]["receipts_qty"], 845)
        self.assertEqual(kept["totals"]["opening_qty"], 11593)
        self.assertEqual(kept["totals"]["sales_qty"], 3086)

    def test_other_swil_text_detector_unchanged(self):
        swil = (
            "Sales & Stock Statement Opening Qty Receipt/Pur Value Total"
        )
        self.assertTrue(_is_swil_opening_receipt_value_statement(swil))
        self.assertFalse(_is_portrait_opening_balance_text(swil))


def _row_values(item):
    extra = item.get("extra") or {}
    return (
        item.get("opening_qty"),
        item.get("opening_value"),
        item.get("receipts_qty"),
        item.get("receipts_value"),
        extra.get("total_stock_qty"),
        item.get("sales_qty"),
        item.get("sales_value"),
        item.get("closing_qty"),
        item.get("closing_value"),
        extra.get("near_expiry_qty"),
    )


class AlayaPortraitStatementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _ALAYA_PDF.is_file():
            raise unittest.SkipTest("Alaya source PDF is not on this machine")
        cls.result = extract_sales_statement(_ALAYA_PDF.read_bytes(), _ALAYA_PDF.name)

    def test_ninety_three_rows_once(self):
        items = self.result["line_items"]
        self.assertEqual(len(items), 93)
        keys = [
            (
                item.get("product_name"),
                item.get("packing"),
                (item.get("extra") or {}).get("source_row"),
            )
            for item in items
        ]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(
            self.result["totals"]["extra"]["extraction_method"],
            "portrait_opening_balance_columns",
        )

    def test_printed_grand_total(self):
        totals = self.result["totals"]
        extra = totals["extra"]
        self.assertEqual(totals["opening_qty"], 11593)
        self.assertAlmostEqual(totals["opening_value"], 1785244.65, places=2)
        self.assertEqual(totals["receipts_qty"], 845)
        self.assertAlmostEqual(totals["receipts_value"], 105856.09, places=2)
        self.assertEqual(extra["total_stock_qty"], 12438)
        self.assertEqual(totals["sales_qty"], 3086)
        self.assertAlmostEqual(totals["sales_value"], 501888.88, places=2)
        self.assertEqual(totals["closing_qty"], 9352)
        self.assertAlmostEqual(totals["closing_value"], 1434411.82, places=2)
        self.assertEqual(extra["near_expiry_qty"], 278)

    def test_printed_example_rows(self):
        found = {}
        for item in self.result["line_items"]:
            key = f"{item.get('product_name')}|{item.get('packing')}"
            if key in _ALAYA_ROWS:
                found[key] = _row_values(item)
        missing = sorted(set(_ALAYA_ROWS) - set(found))
        self.assertEqual(missing, [])
        labels = (
            "opening_qty",
            "opening_value",
            "receipts_qty",
            "receipts_value",
            "total_qty",
            "sales_qty",
            "sales_value",
            "closing_qty",
            "closing_value",
            "near_expiry",
        )
        mismatches = []
        for key, expected in _ALAYA_ROWS.items():
            actual = found[key]
            diffs = [
                f"{label} {got}!={want}"
                for label, got, want in zip(labels, actual, expected)
                if abs(float(got or 0) - float(want)) > 0.02
            ]
            if diffs:
                mismatches.append(key + " " + ", ".join(diffs))
        self.assertEqual(mismatches, [])
