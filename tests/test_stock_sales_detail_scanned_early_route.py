"""Detail Op.Qty/Op.Val scanned PDF must beat camera_multipage/vision_table."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch


class StockSalesDetailScannedEarlyRouteTests(unittest.TestCase):
    def test_detail_scanned_early_returns_before_camera_multipage(self):
        locked = {
            "line_items": [
                {
                    "product_name": f"PROD {i}",
                    "opening_qty": 1.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 0.0,
                    "closing_qty": 1.0,
                    "closing_value": 10.0,
                }
                for i in range(6)
            ],
            "totals": {
                "extra": {
                    "stock_vision_locked": True,
                    "extraction_method": "stock_sales_op_qty_val_vision",
                    "layout": "stock_sales_op_qty_val",
                }
            },
        }

        fake_doc = MagicMock()
        fake_doc.__enter__ = MagicMock(return_value=fake_doc)
        fake_doc.__exit__ = MagicMock(return_value=False)
        fake_doc.__iter__ = MagicMock(return_value=iter([]))
        fake_doc.page_count = 2

        with patch.dict(
            os.environ, {"STOCK_SALES_DETAIL_SCANNED_EARLY": "1"}, clear=False
        ):
            with patch("fitz.open", return_value=fake_doc):
                with patch(
                    "services.stock_vision_table.is_scanned_pdf_bytes",
                    return_value=True,
                ):
                    with patch(
                        "services.stock_direct_vision.extract_stock_sales_op_qty_val_scanned_pdf",
                        return_value=locked,
                    ) as detail_mock:
                        with patch(
                            "services.stock_direct_vision.is_stock_vision_locked",
                            return_value=True,
                        ):
                            with patch(
                                "services.stock_multipage.parse_camera_multipage_stock_pdf"
                            ) as multi_mock:
                                with patch(
                                    "services.sales_statement_extractor._parse_marg_sale_purchase_analysis_text",
                                    return_value=None,
                                ):
                                    with patch(
                                        "services.sales_statement_extractor._group_wise_division_pages_enabled",
                                        return_value=False,
                                    ):
                                        from services.sales_statement_extractor import (
                                            _parse_pdf,
                                        )

                                        result = _parse_pdf(
                                            b"%PDF-1.4 fake", "RSM oct.pdf"
                                        )
        self.assertIs(result, locked)
        detail_mock.assert_called_once()
        multi_mock.assert_not_called()
        self.assertEqual(
            (result.get("totals") or {}).get("extra", {}).get("statement_count"),
            1,
        )

    def test_detail_scanned_early_flag_off_allows_camera_multipage(self):
        multipage = {
            "line_items": [{"product_name": "X", "closing_qty": 1}],
            "totals": {"extra": {"page_count": 2}},
        }
        fake_doc = MagicMock()
        fake_doc.__enter__ = MagicMock(return_value=fake_doc)
        fake_doc.__exit__ = MagicMock(return_value=False)
        fake_doc.__iter__ = MagicMock(return_value=iter([]))
        fake_doc.page_count = 2
        fake_page = MagicMock()
        fake_page.get_text.return_value = ""
        fake_doc.__getitem__ = MagicMock(return_value=fake_page)

        with patch.dict(
            os.environ, {"STOCK_SALES_DETAIL_SCANNED_EARLY": "0"}, clear=False
        ):
            with patch("fitz.open", return_value=fake_doc):
                with patch(
                    "services.stock_vision_table.is_scanned_pdf_bytes",
                    return_value=True,
                ):
                    with patch(
                        "services.stock_direct_vision.extract_stock_sales_op_qty_val_scanned_pdf"
                    ) as detail_mock:
                        with patch(
                            "services.stock_multipage.parse_camera_multipage_stock_pdf",
                            return_value=multipage,
                        ) as multi_mock:
                            with patch(
                                "services.sales_statement_extractor._parse_marg_sale_purchase_analysis_text",
                                return_value=None,
                            ):
                                with patch(
                                    "services.sales_statement_extractor._group_wise_division_pages_enabled",
                                    return_value=False,
                                ):
                                    from services.sales_statement_extractor import (
                                        _parse_pdf,
                                    )

                                    result = _parse_pdf(
                                        b"%PDF-1.4 fake", "RSM oct.pdf"
                                    )
        self.assertIs(result, multipage)
        detail_mock.assert_not_called()
        multi_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
