"""Medica Ultimate OPSTK statement: STOCK is closing qty, IN/OT is not."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from services.sales_statement_extractor import (
    _medica_opstk_items_from_words,
    extract_sales_statement,
)
from services.stock_row_classifier import RowStatus, classify_row, read_row_fields


def _word(x0, x1, y, text):
    return (x0, y, x1, y + 8, text, 0, 0, 0)


# Right edges taken from a Medica Ultimate header (numbers share those edges).
_HEADER_Y = 155.0
_HEADER = [
    _word(23, 63, _HEADER_Y, "PRODUCT"),
    _word(65, 119, _HEADER_Y, "DESCRIPTION"),
    _word(177, 214, _HEADER_Y, "PACKING"),
    _word(221, 249, _HEADER_Y, "OPSTK"),
    _word(256, 284, _HEADER_Y, "PURCH"),
    _word(298, 319, _HEADER_Y, "SALE"),
    _word(324, 346, _HEADER_Y, "SALE"),
    _word(348, 364, _HEADER_Y, "VAL"),
    _word(368, 389, _HEADER_Y, "IN/OT"),
    _word(391, 419, _HEADER_Y, "STOCK"),
    _word(425, 441, _HEADER_Y, "STK"),
    _word(443, 459, _HEADER_Y, "VAL"),
]


def _qty(x1, y, text, width=5):
    return _word(x1 - width, x1, y, text)


class TestMedicaOpstkColumns(unittest.TestCase):
    def _rows(self):
        words = list(_HEADER)
        # EVECARE FORTE SYS: IN/OT 0 must not hide STOCK 4.
        y = 322.9
        words += [
            _word(23, 55, y, "EVECARE"),
            _word(61, 85, y, "FORTE"),
            _word(90, 110, y, "SYS"),
            _word(177, 196, y, "200"),
            _word(201, 216, y, "ML"),
            _qty(249, y, "5"),
            _qty(284, y, "0"),
            _qty(319, y, "1"),
            _qty(364, y, "195", 15),
            _qty(389, y, "0"),
            _qty(419, y, "4"),
            _qty(459, y, "694", 15),
        ]
        # BONNISAN DROP: STOCK 2 / STK VAL 135, not the IN/OT 0.
        y = 190.9
        words += [
            _word(23, 60, y, "BONNISAN"),
            _word(66, 90, y, "DROP"),
            _word(177, 190, y, "30"),
            _word(191, 206, y, "ML"),
            _qty(249, y, "4"),
            _qty(284, y, "5"),
            _qty(319, y, "7"),
            _qty(364, y, "523", 15),
            _qty(389, y, "0"),
            _qty(419, y, "2"),
            _qty(459, y, "135", 15),
        ]
        # BONNISAN SYRUP BIG: IN/OT 1 must not become closing qty. STOCK is 27.
        y = 202.9
        words += [
            _word(23, 60, y, "BONNISAN"),
            _word(66, 90, y, "SYRUP"),
            _word(95, 115, y, "BIG"),
            _word(177, 196, y, "200"),
            _word(201, 216, y, "ML"),
            _qty(249, y, "28", 10),
            _qty(284, y, "0"),
            _qty(319, y, "2"),
            _qty(364, y, "229", 15),
            _qty(389, y, "1"),
            _qty(419, y, "27", 10),
            _qty(459, y, "2735", 20),
        ]
        # BONNISPAZ DROP follows SYRUP SMALL and must keep its own zeros.
        y = 214.9
        words += [
            _word(23, 60, y, "BONNISAN"),
            _word(66, 90, y, "SYRUP"),
            _word(95, 120, y, "SMALL"),
            _word(177, 196, y, "100"),
            _word(201, 216, y, "ML"),
            _qty(249, y, "15", 10),
            _qty(284, y, "0"),
            _qty(319, y, "5"),
            _qty(364, y, "339", 15),
            _qty(389, y, "2"),
            _qty(419, y, "12", 10),
            _qty(459, y, "721", 15),
        ]
        y = 226.9
        words += [
            _word(23, 65, y, "BONNISPAZ"),
            _word(71, 95, y, "DROP"),
            _word(177, 190, y, "15"),
            _word(191, 206, y, "ML"),
            _qty(249, y, "1"),
            _qty(284, y, "3"),
            _qty(319, y, "4"),
            _qty(364, y, "256", 15),
            _qty(389, y, "0"),
            _qty(419, y, "0"),
            _qty(459, y, "0"),
        ]
        return _medica_opstk_items_from_words(words)

    def test_in_ot_is_not_closing_and_rows_stay_separate(self):
        items = {item["product_name"]: item for item in self._rows()}
        self.assertEqual(len(items), 5)

        forte = items["EVECARE FORTE SYS"]
        self.assertEqual(forte["packing"], "200 ML")
        self.assertEqual(forte["opening_qty"], 5)
        self.assertEqual(forte["receipts_qty"], 0)
        self.assertEqual(forte["sales_qty"], 1)
        self.assertEqual(forte["sales_value"], 195)
        self.assertEqual(forte["closing_qty"], 4)
        self.assertEqual(forte["closing_value"], 694)

        drop = items["BONNISAN DROP"]
        self.assertEqual(drop["opening_qty"], 4)
        self.assertEqual(drop["receipts_qty"], 5)
        self.assertEqual(drop["sales_qty"], 7)
        self.assertEqual(drop["sales_value"], 523)
        self.assertEqual(drop["closing_qty"], 2)
        self.assertEqual(drop["closing_value"], 135)

        big = items["BONNISAN SYRUP BIG"]
        self.assertEqual(big["opening_qty"], 28)
        self.assertEqual(big["receipts_qty"], 0)
        self.assertEqual(big["sales_qty"], 2)
        self.assertEqual(big["sales_value"], 229)
        self.assertEqual(big["closing_qty"], 27)
        self.assertEqual(big["closing_value"], 2735)

        small = items["BONNISAN SYRUP SMALL"]
        self.assertEqual(small["closing_qty"], 12)
        self.assertEqual(small["closing_value"], 721)

        paz = items["BONNISPAZ DROP"]
        self.assertEqual(paz["packing"], "15 ML")
        self.assertEqual(paz["opening_qty"], 1)
        self.assertEqual(paz["receipts_qty"], 3)
        self.assertEqual(paz["sales_qty"], 4)
        self.assertEqual(paz["sales_value"], 256)
        self.assertEqual(paz["closing_qty"], 0)
        self.assertEqual(paz["closing_value"], 0)


_FLAG = "STOCK_MEDICA_IN_OT_AS_FREE_IN"


def _in_ot_rows(flag_on):
    words = list(_HEADER)
    # BONNISAN LIQ. 100ML: 158 + 168 - 134 + 13 = 205.
    y = 190.9
    words += [
        _word(23, 60, y, "BONNISAN"),
        _word(66, 90, y, "LIQ."),
        _word(95, 125, y, "100ML"),
        _word(177, 190, y, "100"),
        _word(191, 206, y, "ML"),
        _qty(249, y, "158", 12),
        _qty(284, y, "168", 12),
        _qty(319, y, "134", 12),
        _qty(364, y, "9800", 20),
        _qty(389, y, "13"),
        _qty(419, y, "205", 12),
        _qty(459, y, "12000", 20),
    ]
    # CYSTONE (FC) 60TABS: 318 + 100 - 234 - 20 = 164.
    y = 202.9
    words += [
        _word(23, 55, y, "CYSTONE"),
        _word(61, 85, y, "(FC)"),
        _word(90, 125, y, "60TABS"),
        _word(177, 190, y, "60"),
        _word(191, 206, y, "TAB"),
        _qty(249, y, "318", 12),
        _qty(284, y, "100", 12),
        _qty(319, y, "234", 12),
        _qty(364, y, "20000", 20),
        _qty(389, y, "-20", 10),
        _qty(419, y, "164", 12),
        _qty(459, y, "9000", 20),
    ]
    with patch.dict(os.environ, {_FLAG: "true" if flag_on else "false"}):
        return {i["product_name"]: i for i in _medica_opstk_items_from_words(words)}


class TestMedicaInOtFreeIn(unittest.TestCase):
    def test_positive_in_ot_is_free_in_and_row_is_valid(self):
        item = _in_ot_rows(True)["BONNISAN LIQ. 100ML"]
        self.assertEqual(item["extra"]["free_in_qty"], 13)
        self.assertEqual(item["extra"]["field_source"]["free_in_qty"], "printed")
        self.assertEqual(item["receipts_qty"], 168)
        self.assertEqual(item["closing_qty"], 205)
        status, _ = classify_row(read_row_fields(item))
        self.assertEqual(status, RowStatus.VALID)

    def test_negative_in_ot_balances_to_stock(self):
        item = _in_ot_rows(True)["CYSTONE (FC) 60TABS"]
        self.assertEqual(item["extra"]["free_in_qty"], -20)
        self.assertEqual(item["receipts_qty"], 100)
        self.assertEqual(item["closing_qty"], 164)
        status, _ = classify_row(read_row_fields(item))
        self.assertEqual(status, RowStatus.VALID)

    def test_flag_off_keeps_free_in_absent(self):
        for item in _in_ot_rows(False).values():
            self.assertNotIn("free_in_qty", item.get("extra") or {})
            self.assertEqual(item["closing_qty"] in (205, 164), True)

    def test_printed_zero_in_ot_is_zero(self):
        words = list(_HEADER)
        y = 190.9
        words += [
            _word(23, 60, y, "EVECARE"),
            _word(177, 190, y, "30"),
            _qty(249, y, "5"),
            _qty(284, y, "0"),
            _qty(319, y, "1"),
            _qty(364, y, "195", 15),
            _qty(389, y, "0"),
            _qty(419, y, "4"),
            _qty(459, y, "694", 15),
        ]
        with patch.dict(os.environ, {_FLAG: "true"}):
            item = _medica_opstk_items_from_words(words)[0]
        self.assertEqual(item["extra"]["free_in_qty"], 0)
        self.assertEqual(item["extra"]["field_source"]["free_in_qty"], "printed")


_SOURCE = Path(
    r"C:\Users\Prerana Bhalerao\Downloads\ZA_2026_August"
    r"\0000736197_2026_08_ZA_13_5102_03092026161013.pdf"
)


class TestMedicaOpstkSource(unittest.TestCase):
    def test_source_statement_keeps_42_products(self):
        if not _SOURCE.is_file():
            self.skipTest("source PDF is not on this machine")
        result = extract_sales_statement(_SOURCE.read_bytes(), _SOURCE.name)
        items = result["line_items"]
        self.assertEqual(len(items), 42)
        by_name = {item["product_name"]: item for item in items}
        self.assertIn("EVECARE FORTE SYS", by_name)
        self.assertNotIn("EVICARE", by_name)
        self.assertEqual(by_name["EVECARE FORTE SYS"]["closing_qty"], 4)
        self.assertEqual(by_name["BONNISAN DROP"]["closing_qty"], 2)
        self.assertEqual(by_name["BONNISAN SYRUP BIG"]["closing_qty"], 27)
        self.assertEqual(by_name["BONNISPAZ DROP"]["closing_qty"], 0)
        self.assertEqual(by_name["BONNISPAZ DROP"]["opening_qty"], 1)
        self.assertEqual(by_name["BONNISAN DROP"]["closing_value"], 135)
        self.assertEqual(by_name["BONNISAN SYRUP BIG"]["closing_value"], 2735)
        self.assertEqual(by_name["BONNISPAZ DROP"]["closing_value"], 0)
        self.assertEqual(by_name["BRESOL SYS BIG"]["closing_qty"], 23)
        self.assertEqual(by_name["CYSTONE TAB"]["closing_qty"], 100)
        self.assertEqual(by_name["LIV 52 DS TAB"]["closing_qty"], 191)
        self.assertEqual(by_name["SHATAVARI CAP"]["closing_qty"], 67)
        self.assertNotEqual(by_name["BONNISAN SYRUP BIG"]["closing_qty"], 1)
        totals = result["totals"]
        self.assertNotEqual(
            totals["extra"]["extraction_method"], "zandra_stock_sale_vision"
        )
        # Column sums from the 42 rows. 1105 is opening+receipts-sales and is
        # not the OPSTK total; STOCK is 13 higher because of IN/OT.
        self.assertEqual(totals["opening_qty"], 1253)
        self.assertEqual(totals["receipts_qty"], 233)
        self.assertEqual(totals["sales_qty"], 381)
        self.assertEqual(totals["sales_value"], 69729)
        self.assertEqual(totals["closing_qty"], 1118)
        self.assertEqual(totals["closing_value"], 171604)
        self.assertEqual(totals["extra"]["stock_validation"]["calculated_closing"], 1118)
        self.assertEqual(totals["extra"]["stock_validation"]["extracted_closing"], 1118)
        self.assertEqual(
            totals["extra"]["extraction_method"], "medica_opstk_columns"
        )


if __name__ == "__main__":
    unittest.main()
