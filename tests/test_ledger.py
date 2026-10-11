import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import albion_ledger as al  # noqa: E402


def row(**kw):
    base = {"character": "A", "location": "Martlock", "item_id": "T5_HIDE", "name": "5.0 Hide",
            "qty": 10, "quality": None, "manual": None, "note": ""}
    base.update(kw)
    return base


class PriceTests(unittest.TestCase):
    def test_volume_weighted_value_and_best_market(self):
        sales = {("T5_HIDE", "Martlock", 1): (300, 500.0),
                 ("T5_HIDE", "Caerleon", 1): (100, 700.0),
                 ("T5_HIDE", "Lymhurst", 1): (1, 5000.0)}   # too few sales to trust
        r = al.price_row(row(), sales, days=10)
        self.assertEqual(r["each"], (300 * 500 + 100 * 700) / 400)
        self.assertEqual(r["best_market"], "Caerleon")
        self.assertEqual(r["total"], r["each"] * 10)
        self.assertEqual(r["status"], "easy")       # 40 per day

    def test_quality_filter(self):
        sales = {("X", "Martlock", 1): (10, 100.0), ("X", "Martlock", 4): (10, 300.0)}
        r = al.price_row(row(item_id="X", quality=4, qty=1), sales, days=30)
        self.assertEqual(r["each"], 300)

    def test_manual_price_when_no_market(self):
        r = al.price_row(row(item_id="X", manual=1_000_000.0, qty=2), {}, days=30)
        self.assertEqual((r["total"], r["status"], r["source"]), (2_000_000, "your price", "manual"))
        r = al.price_row(row(item_id="Y"), {}, days=30)
        self.assertEqual((r["total"], r["status"]), (0, "unknown"))

    def test_silver(self):
        r = al.price_row(row(item_id="SILVER", qty=1, manual=5e7), {}, days=30)
        self.assertEqual((r["total"], r["status"]), (5e7, "silver"))


class IoTests(unittest.TestCase):
    def test_read_and_html(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "h.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("character,location,item_id,name,qty,quality,manual_price,note\n"
                        "A,Martlock,T5_HIDE,5.0 Hide,85,,,\n"
                        "A,Caerleon,X,</script>,1,4,1400000,thin\n\n")
            rows = al.read_holdings([path])
            self.assertEqual([r["qty"] for r in rows], [85, 1])
            self.assertEqual((rows[1]["quality"], rows[1]["manual"]), (4, 1400000.0))
            for r in rows:
                al.price_row(r, {})
            out = os.path.join(d, "l.html")
            al.write_html(out, rows, {"generated": "x", "server": "americas", "days": 30})
            with open(out, encoding="utf-8") as f:
                page = f.read()
        blob = page.split('type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(blob)["rows"][1]["name"], "</script>")


if __name__ == "__main__":
    unittest.main()
