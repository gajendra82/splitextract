"""Medica OPSTK|SALE|SALE VAL usable gate + empty-OCR SSA dump skip."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.sales_statement_extractor import (
    _medica_result_looks_usable,
    _ssa_dump_portrait_noise_candidate,
)


def _row(
    opening: float,
    receipts: float,
    sales: float,
    closing: float,
    *,
    sales_value: float = 0.0,
) -> dict:
    return {
        "product_name": "SKU",
        "opening_qty": opening,
        "receipts_qty": receipts,
        "sales_qty": sales,
        "sales_value": sales_value,
        "closing_qty": closing,
        "closing_value": closing * 100.0,
    }


class TestMedicaSaleValUsable(unittest.TestCase):
    def test_rejects_when_sale_and_sale_val_dropped(self):
        # Printed SALE implied by open+purch-closing, but both sales fields 0.
        items = [
            _row(9, 0, 0, 7),
            _row(38, 0, 0, 37),
            _row(7, 50, 0, 49),
            _row(14, 0, 0, 9),
            _row(9, 100, 0, 83),
            _row(28, 50, 0, 74),
            _row(36, 0, 0, 26),
            _row(70, 210, 0, 197),
        ]
        result = {"line_items": items}
        self.assertFalse(_medica_result_looks_usable(result))

    def test_accepts_when_sale_and_sale_val_present(self):
        items = [
            _row(9, 0, 2, 7, sales_value=470),
            _row(38, 0, 1, 37, sales_value=186),
            _row(7, 50, 8, 49, sales_value=1018),
            _row(14, 0, 5, 9, sales_value=582),
            _row(9, 100, 16, 83, sales_value=2896),
            _row(28, 50, 4, 74, sales_value=834),
        ]
        result = {"line_items": items}
        self.assertTrue(_medica_result_looks_usable(result))

    def test_portrait_noise_skips_medica_red_header_on_empty_peek(self):
        with patch(
            "services.sales_statement_extractor._medica_red_header_plausible",
            return_value=True,
        ):
            self.assertFalse(
                _ssa_dump_portrait_noise_candidate(b"fake-jpeg-bytes", "")
            )


if __name__ == "__main__":
    unittest.main()
