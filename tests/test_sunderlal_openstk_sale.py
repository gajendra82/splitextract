"""STOCK & SALE STATEMENT: OPENSTK / PURSTK / SALESTK / CLOSESTK.

Column position decides the field. A printed zero stays in that column.
NEAR EXPIRY REPORT rows are not stock products.
"""

import unittest
from pathlib import Path

from services.sales_statement_extractor import extract_sales_statement


_PDF = Path(
    r"C:\Users\Prerana Bhalerao\Downloads\ZA_2026_August"
    r"\0000736231_2026_08_ZA_13_239_05092026114532.pdf"
)


def _money(item, field):
    if field == "purchase_value":
        return (item.get("extra") or {}).get("purchase_value")
    return item.get(field)


class TestSunderlalOpenstkSale(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _PDF.is_file():
            raise unittest.SkipTest(f"source PDF missing: {_PDF}")
        cls.result = extract_sales_statement(_PDF.read_bytes(), _PDF.name)
        cls.by_name = {item["product_name"]: item for item in cls.result["line_items"]}

    def _qty(self, name, opening, purchase, sales, closing):
        item = self.by_name[name]
        self.assertEqual(
            (
                item.get("opening_qty"),
                item.get("receipts_qty"),
                item.get("sales_qty"),
                item.get("closing_qty"),
            ),
            (opening, purchase, sales, closing),
            name,
        )
        self.assertEqual(item.get("purchase_qty"), purchase)

    def _values(self, name, opening, purchase, sales, closing):
        item = self.by_name[name]
        self.assertAlmostEqual(_money(item, "opening_value"), opening, places=2)
        self.assertAlmostEqual(_money(item, "purchase_value"), purchase, places=2)
        self.assertAlmostEqual(_money(item, "sales_value"), sales, places=2)
        self.assertAlmostEqual(_money(item, "closing_value"), closing, places=2)

    def test_critical_quantity_rows(self):
        self._qty("BRESOL SYRUP 200ML", 24, 0, 13, 11)
        self._qty("GERIFORTE TABS", 2, 10, 2, 10)
        self._qty("LIV 52 DS SYRUP", 18, 70, 14, 74)
        self.assertEqual(self.by_name["LIV 52 DS SYRUP"].get("packing"), "100 ML")
        self._qty("LIV 52 DS TAB", 50, 0, 27, 23)
        self._qty("MENOSAN TAB", 0, 20, 5, 15)
        self._qty("SEPTILIN TAB", 91, 0, 5, 86)

    def test_page1_sales_qty_stays_in_salestk(self):
        expected = {
            "BONNISAN DROPS": 33,
            "BONNISAN LIQUID": 6,
            "BONNISPASZ DROPS": 8,
            "BRESOL NS DROPS": 12,
            "BRESOL SYRUP 200ML": 13,
            "BRESOL TAB": 7,
            "CYSTONE FORTE TAB": 9,
            "CYSTONE SYRUP [BIG]": 5,
            "CYSTONE TABS": 35,
            "EVECARE CAP": 6,
            "EVECARE FORTE SYRUP": 1,
            "EVECARE FORTE TAB": 9,
            "EVECARE SYRUP": 1,
            "HIMPLASIA TAB": 2,
            "LIV 52 DS SYP 200ML": 3,
        }
        for name, sales in expected.items():
            self.assertEqual(self.by_name[name].get("sales_qty"), sales, name)
        drops = [
            item
            for item in self.result["line_items"]
            if item.get("product_name") == "LIV 52 DROPS"
        ]
        self.assertEqual(sorted(item.get("sales_qty") for item in drops), [10, 20])

    def test_monetary_columns(self):
        self._values("GERIFORTE TABS", 224.92, 1124.60, 250.00, 1124.60)
        self._values("LIV 52 DS TAB", 9490.00, 0.00, 5780.70, 4365.40)

    def test_main_table_excludes_near_expiry_and_footer(self):
        names = [item.get("product_name") for item in self.result["line_items"]]
        self.assertEqual(len(names), 54)
        self.assertNotIn("LUKOL 200 ML", names)
        self.assertIn("LUKOL SYRUP", names)
        self.assertIn("LUKOL TAB", names)
        for name in (
            "ARJUNA CAP",
            "BRESOL SYRUP 100ML",
            "CYSTONE SYRUP",
            "EVECARE SYRUP [BIG]",
            "HIMCOSPAZ CAP",
        ):
            item = self.by_name[name]
            self.assertEqual(
                (
                    item.get("opening_qty"),
                    item.get("receipts_qty"),
                    item.get("sales_qty"),
                    item.get("closing_qty"),
                ),
                (0, 0, 0, 0),
                name,
            )

    def test_statement_totals_are_the_source_columns(self):
        extra = self.result["totals"]["extra"]
        self.assertEqual(extra.get("extraction_method"), "sunderlal_openstk_sale")
        self.assertEqual(
            (
                extra.get("opening_qty"),
                extra.get("receipts_qty"),
                extra.get("sales_qty"),
                extra.get("closing_qty"),
            ),
            (1650, 120, 283, 1487),
        )
        self.assertAlmostEqual(extra.get("opening_value"), 218148.89, places=2)
        self.assertAlmostEqual(extra.get("purchase_value"), 17240.70, places=2)
        self.assertAlmostEqual(extra.get("sales_value"), 41873.03, places=2)
        self.assertAlmostEqual(extra.get("closing_value"), 199513.24, places=2)
        self.assertEqual(self.result.get("period_from"), "2026-08-01")
        self.assertEqual(self.result.get("period_to"), "2026-08-31")


if __name__ == "__main__":
    unittest.main()
