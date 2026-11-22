"""Reject SSA dump when most rows invent sales_qty=1 to fake identity."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _ssa_dump_items_look_like_unit_sale_slide,
    _ssa_dump_result_looks_like_unit_sale_slide,
)


def _row(name: str, oq: float, rq: float, sq: float, cq: float) -> dict:
    return {
        "product_name": name,
        "opening_qty": oq,
        "receipts_qty": rq,
        "sales_qty": sq,
        "closing_qty": cq,
        "sales_value": 0.0,
        "closing_value": 0.0,
        "extra": {},
    }


class SsaUnitSaleSlideRejectTests(unittest.TestCase):
    def test_detects_epidemic_unit_sale_slide(self):
        items = [
            _row(f"SKU{i}", 20.0 + i, 0.0, 1.0, 19.0 + i) for i in range(12)
        ]
        # A few real-looking rows do not mask the slide.
        items[0] = _row("ARJUNA CAP", 21.0, 28.0, 15.0, 34.0)
        items[1] = _row("BONNISAN SYP.", 44.0, 56.0, 18.0, 82.0)
        self.assertTrue(_ssa_dump_items_look_like_unit_sale_slide(items))
        self.assertTrue(
            _ssa_dump_result_looks_like_unit_sale_slide({"line_items": items})
        )

    def test_keeps_varied_sales_sheet(self):
        items = [
            _row("ARJUNA CAP", 34.0, 0.0, 5.0, 29.0),
            _row("BONNISAN SYP.", 38.0, 0.0, 9.0, 29.0),
            _row("BONNISAN DROP", 7.0, 0.0, 0.0, 7.0),
            _row("CYSTONE TAB", 26.0, 0.0, 4.0, 22.0),
            _row("EVECARE CAP", 63.0, 0.0, 0.0, 63.0),
            _row("GERIFORTE TAB", 18.0, 0.0, 6.0, 12.0),
            _row("HIORA GA", 37.0, 0.0, 3.0, 34.0),
            _row("KARELA CAP", 39.0, 0.0, 1.0, 38.0),
            _row("SEPTILIN TAB", 56.0, 0.0, 15.0, 41.0),
            _row("SPEMAN TAB", 58.0, 0.0, 6.0, 52.0),
        ]
        self.assertFalse(_ssa_dump_items_look_like_unit_sale_slide(items))

    def test_ignores_tiny_samples(self):
        items = [_row(f"SKU{i}", 20.0, 0.0, 1.0, 19.0) for i in range(5)]
        self.assertFalse(_ssa_dump_items_look_like_unit_sale_slide(items))


if __name__ == "__main__":
    unittest.main()
