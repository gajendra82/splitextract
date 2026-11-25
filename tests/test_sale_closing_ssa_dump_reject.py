"""SALE+CLOSING-only sheets must not use SSA OPENING/RECEIPT dump."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _is_sale_closing_only_header,
    _result_looks_like_sale_closing_only,
    _sale_closing_only_weak_signal,
    _ssa_dump_items_look_like_sale_closing_only,
    _ssa_dump_portrait_noise_candidate,
    _ssa_dump_result_looks_like_sale_closing_only,
    _stamp_sale_closing_only_result,
    empty_line_item,
    empty_result,
)


SALE_CLOSING_CHROME = """
RAGHU ENTERPRISES & MARKETING CO.
STOCK & SALES ANALYSIS (HIMALAYA[ZEAL]) 01/10/2026 - 31/10/2026
ITEM DESCRIPTION          <===SALE===>   <==CLOSING==>  CHAL
                          QTY. VALUE     QTY. VALUE
ABANA TAB 60'S Pcs 6 952 62 9674 0
"""

OPENING_DUMP = """
STOCK & SALES ANALYSIS
ITEM DESCRIPTION
OPENING RECEIPT ISSUE CLOSING DUMP
"""


def _item(
    *,
    sales=6.0,
    sales_value=952.0,
    closing=62.0,
    closing_value=9674.0,
    opening=0.0,
    receipts=0.0,
    flags=None,
    dump_qty=0.0,
):
    item = empty_line_item()
    item["product_name"] = "ABANA TAB"
    item["packing"] = "60'S"
    item["opening_qty"] = opening
    item["receipts_qty"] = receipts
    item["sales_qty"] = sales
    item["sales_value"] = sales_value
    item["closing_qty"] = closing
    item["closing_value"] = closing_value
    item["extra"] = {
        "dump_qty": dump_qty,
        "flags": list(flags or []),
        "field_source": {},
    }
    return item


class SaleClosingSsaDumpRejectTests(unittest.TestCase):
    def test_header_and_weak_signal(self):
        self.assertTrue(_is_sale_closing_only_header(SALE_CLOSING_CHROME))
        self.assertTrue(_sale_closing_only_weak_signal(SALE_CLOSING_CHROME))
        self.assertFalse(_is_sale_closing_only_header(OPENING_DUMP))
        self.assertFalse(_sale_closing_only_weak_signal(OPENING_DUMP))
        self.assertFalse(_sale_closing_only_weak_signal(""))

    def test_portrait_noise_skips_sale_closing_chrome(self):
        # Portrait buffer + SALE/CLOSING chrome must not force SSA dump.
        from PIL import Image
        import io

        img = Image.new("RGB", (800, 1400), color=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        data = buf.getvalue()
        self.assertFalse(
            _ssa_dump_portrait_noise_candidate(data, SALE_CLOSING_CHROME)
        )

    def test_dump_result_receipt_as_opening_is_sale_closing(self):
        items = [
            _item(flags=["SSA_DUMP_RECEIPT_AS_OPENING"], opening=6.0, receipts=0.0),
            _item(
                sales=2.0,
                sales_value=318.0,
                closing=0.0,
                closing_value=0.0,
                flags=["SSA_DUMP_RECEIPT_AS_OPENING"],
                opening=2.0,
            ),
            _item(
                sales=146.0,
                sales_value=24786.0,
                closing=142.0,
                closing_value=22858.0,
                flags=["SSA_DUMP_RECEIPT_AS_OPENING"],
                opening=146.0,
            ),
            _item(
                sales=26.0,
                sales_value=2819.0,
                closing=74.0,
                closing_value=7478.0,
                flags=["SSA_DUMP_RECEIPT_AS_OPENING"],
                opening=26.0,
            ),
            _item(
                sales=33.0,
                sales_value=3333.0,
                closing=61.0,
                closing_value=5450.0,
                flags=["SSA_DUMP_RECEIPT_AS_OPENING"],
                opening=33.0,
            ),
        ]
        self.assertTrue(_ssa_dump_items_look_like_sale_closing_only(items))
        self.assertTrue(
            _ssa_dump_result_looks_like_sale_closing_only(
                {
                    "report_title": "STOCK & SALES ANALYSIS",
                    "line_items": items,
                }
            )
        )

    def test_real_dump_column_not_sale_closing(self):
        items = [
            _item(dump_qty=1.0, opening=10.0, receipts=2.0, sales=3.0, closing=9.0),
            _item(dump_qty=2.0, opening=10.0, receipts=2.0, sales=3.0, closing=9.0),
            _item(dump_qty=0.0, opening=10.0, receipts=2.0, sales=3.0, closing=9.0),
            _item(dump_qty=0.0, opening=10.0, receipts=2.0, sales=3.0, closing=9.0),
            _item(dump_qty=0.0, opening=10.0, receipts=2.0, sales=3.0, closing=9.0),
        ]
        self.assertFalse(_ssa_dump_items_look_like_sale_closing_only(items))

    def test_sale_parked_in_opening_with_identity_fails(self):
        # Empty-OCR dump copies SALE into opening while still reading SALE+CLOSING.
        items = [
            _item(opening=6.0, receipts=0.0, sales=6.0, sales_value=952.0, closing=62.0, closing_value=9674.0),
            _item(opening=2.0, receipts=0.0, sales=2.0, sales_value=318.0, closing=0.0, closing_value=0.0),
            _item(opening=146.0, receipts=0.0, sales=146.0, sales_value=24786.0, closing=142.0, closing_value=22858.0),
            _item(opening=26.0, receipts=0.0, sales=26.0, sales_value=2819.0, closing=74.0, closing_value=7478.0),
            _item(opening=33.0, receipts=0.0, sales=33.0, sales_value=3333.0, closing=61.0, closing_value=5450.0),
            _item(
                opening=2.0,
                receipts=0.0,
                sales=0.0,
                sales_value=0.0,
                closing=0.0,
                closing_value=0.0,
                flags=["SSA_DUMP_RECEIPT_AS_OPENING"],
            ),
        ]
        self.assertTrue(_ssa_dump_items_look_like_sale_closing_only(items))

    def test_header_driven_sale_closing_shift(self):
        from services.sales_statement_extractor import (
            _header_driven_looks_like_sale_closing_shift,
        )

        result = {
            "line_items": [
                _item(opening=6.0, sales=62.0, sales_value=9674.0, closing=0.0),
                _item(opening=2.0, sales=4.0, sales_value=776.0, closing=0.0),
                _item(opening=146.0, sales=142.0, sales_value=22858.0, closing=0.0),
                _item(opening=26.0, sales=74.0, sales_value=7478.0, closing=0.0),
                _item(opening=33.0, sales=61.0, sales_value=5450.0, closing=0.0),
            ]
        }
        self.assertTrue(_header_driven_looks_like_sale_closing_shift(result))

    def test_stamp_nulls_opening_and_sets_kind(self):
        result = empty_result("133.jpg", "jpg")
        result["report_title"] = "STOCK & SALES ANALYSIS"
        result["company_name"] = "HIMALAYA [ZEAL]"
        for _ in range(5):
            result["line_items"].append(_item(opening=0.0, receipts=0.0))
        self.assertTrue(_result_looks_like_sale_closing_only(result))
        stamped = _stamp_sale_closing_only_result(result)
        item = stamped["line_items"][0]
        self.assertIsNone(item.get("opening_qty"))
        self.assertIsNone(item.get("receipts_qty"))
        fs = (item.get("extra") or {}).get("field_source") or {}
        self.assertEqual(fs.get("opening_qty"), "missing")
        self.assertEqual(fs.get("sales_qty"), "printed")
        self.assertEqual(
            stamped["totals"]["extra"].get("stock_identity_kind"),
            "sale_closing_only",
        )


if __name__ == "__main__":
    unittest.main()
