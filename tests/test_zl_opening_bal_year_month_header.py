"""Portal Opening_bal Year|Month|Div: data-as-header must not steal Ob/Primary/Cb."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import _finalize_zl_opening_bal_sheet
from services.stock_vision_table import (
    _vision_header_cells_look_like_zl_opening_bal_year_month,
    map_vision_table,
)


class TestZlOpeningBalYearMonthHeader(unittest.TestCase):
    def test_detects_year_month_div_data_header(self):
        cells = [
            {"col_index": i, "text": t}
            for i, t in enumerate(
                [
                    "BATHEJA AGENCIES",
                    "7002270",
                    "KARELA TABLETS 60 S INDIA",
                    "300",
                    "203.66 20",
                    "0.00",
                    "15",
                    "2026",
                    "10",
                    "ZA",
                ]
            )
        ]
        self.assertTrue(_vision_header_cells_look_like_zl_opening_bal_year_month(cells))

    def test_rejects_medica_opstk_headers(self):
        cells = [
            {"col_index": i, "text": t}
            for i, t in enumerate(
                [
                    "PRODUCT DESCRIPTION",
                    "PACKING",
                    "OPSTK",
                    "PURCH",
                    "SALE",
                    "SALE VAL",
                    "IN/OT",
                    "STOCK",
                    "STK VAL",
                ]
            )
        ]
        self.assertFalse(_vision_header_cells_look_like_zl_opening_bal_year_month(cells))

    def test_map_restores_opening_from_rate_glue(self):
        headers = [
            {"col_index": i, "text": t}
            for i, t in enumerate(
                [
                    "BATHEJA AGENCIES",
                    "7002270",
                    "KARELA TABLETS 60 S INDIA",
                    "300",
                    "203.66 20",
                    "0.00",
                    "15",
                    "2026",
                    "10",
                    "ZA",
                ]
            )
        ]
        table = {
            "tables": [
                {
                    "column_count": 10,
                    "header_rows": [headers],
                    "rows": [
                        {
                            "row_index": 0,
                            "cells": [
                                "BATHEJA AGENCIES",
                                "7002262",
                                "LASUNA TABLETS 60 S INDIA",
                                "325",
                                "220.63 10",
                                "0.00",
                                "8",
                                "2026",
                                "10",
                                "ZA",
                            ],
                        },
                        {
                            "row_index": 1,
                            "cells": [
                                "BATHEJA AGENCIES",
                                "7000251",
                                "LIV.52 DROPS 60ml",
                                "145",
                                "97.94 112",
                                "0.00",
                                "100",
                                "2026",
                                "10",
                                "ZA",
                            ],
                        },
                    ],
                }
            ]
        }
        mapped = map_vision_table(table, request_id="zl-ym-test")
        items = mapped.get("line_items") or []
        self.assertEqual(len(items), 3)
        karela = next(
            i for i in items if "KARELA" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(karela.get("opening_qty"), 20.0)
        self.assertEqual(karela.get("receipts_qty"), 0.0)
        self.assertEqual(karela.get("closing_qty"), 15.0)
        lasuna = next(
            i for i in items if "LASUNA" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(lasuna.get("opening_qty"), 10.0)
        self.assertEqual(lasuna.get("closing_qty"), 8.0)
        liv = next(
            i for i in items if "LIV.52 DROPS" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(liv.get("opening_qty"), 112.0)
        self.assertEqual(liv.get("closing_qty"), 100.0)

    def test_large_closing_stays_qty_and_secondary_sales_fill(self):
        """Closing_bal_qty >= 500 is qty. Sales = Ob + Primary − Cb."""
        headers = [
            {"col_index": i, "text": t}
            for i, t in enumerate(
                [
                    "BATHEJA AGENCIES",
                    "7002270",
                    "KARELA TABLETS 60 S INDIA",
                    "300",
                    "203.66 20",
                    "0.00",
                    "15",
                    "2026",
                    "10",
                    "ZA",
                ]
            )
        ]
        table = {
            "tables": [
                {
                    "column_count": 10,
                    "header_rows": [headers],
                    "rows": [
                        {
                            "row_index": 0,
                            "cells": [
                                "BATHEJA AGENCIES",
                                "7002239",
                                "LIV.52 DS SYRUP 200ML IND",
                                "351",
                                "237.07 1000",
                                "1994.00",
                                "1000",
                                "2026",
                                "10",
                                "ZA",
                            ],
                        },
                        {
                            "row_index": 1,
                            "cells": [
                                "BATHEJA AGENCIES",
                                "7000283",
                                "MENTAT SYRUP 200ml",
                                "257",
                                "173.58 5",
                                "-1.00",
                                "3",
                                "2026",
                                "10",
                                "ZA",
                            ],
                        },
                    ],
                }
            ]
        }
        mapped = map_vision_table(table, request_id="zl-ym-sales")
        for item in mapped.get("line_items") or []:
            extra = item.setdefault("extra", {})
            phys = extra.get("physical_cells") or {}
            rate_tok = phys.get("4")
            if rate_tok not in (None, ""):
                extra["unit_rate"] = float(rate_tok)
        done = _finalize_zl_opening_bal_sheet(
            mapped,
            extraction_method="zl_opening_bal_year_month_vision",
            derive_secondary_sales=True,
        )
        liv = next(
            i
            for i in done["line_items"]
            if "LIV.52 DS SYRUP 200" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(liv.get("opening_qty"), 1000.0)
        self.assertEqual(liv.get("receipts_qty"), 1994.0)
        self.assertEqual(liv.get("closing_qty"), 1000.0)
        self.assertIsNone(liv.get("sales_qty"))
        self.assertIsNone(liv.get("sales_value"))
        self.assertEqual(liv["extra"]["field_source"]["sales_qty"], "missing")
        self.assertEqual(liv["extra"]["field_source"]["sales_value"], "missing")
        liv_cand = next(
            c
            for c in liv["extra"]["candidates"]
            if c.get("source") == "zl_year_month_missing_sales"
        )
        self.assertEqual(liv_cand["fields"]["sales_qty"], 1994.0)
        self.assertEqual(liv_cand["fields"]["sales_value"], round(1994 * 237.07, 2))
        self.assertEqual(liv["extra"]["unit_rate"], 237.07)
        mentat = next(
            i
            for i in done["line_items"]
            if "MENTAT SYRUP" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(mentat.get("opening_qty"), 5.0)
        self.assertEqual(mentat.get("receipts_qty"), -1.0)
        self.assertEqual(mentat.get("closing_qty"), 3.0)
        self.assertIsNone(mentat.get("sales_qty"))
        self.assertIsNone(mentat.get("sales_value"))
        mentat_cand = next(
            c
            for c in mentat["extra"]["candidates"]
            if c.get("source") == "zl_year_month_missing_sales"
        )
        self.assertEqual(mentat_cand["fields"]["sales_qty"], 1.0)
        self.assertEqual(mentat_cand["fields"]["sales_value"], round(1 * 173.58, 2))
        karela = next(
            i
            for i in done["line_items"]
            if "KARELA" in str(i.get("product_name") or "").upper()
        )
        self.assertIsNone(karela.get("sales_qty"))
        self.assertIsNone(karela.get("sales_value"))
        karela_cand = next(
            c
            for c in karela["extra"]["candidates"]
            if c.get("source") == "zl_year_month_missing_sales"
        )
        self.assertEqual(karela_cand["fields"]["sales_qty"], 5.0)
        self.assertEqual(karela_cand["fields"]["sales_value"], round(5 * 203.66, 2))


if __name__ == "__main__":
    unittest.main()
