import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import albion_craft_planner as acp  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")


def row(sell=0, buy=0):
    return {"sell_price_min": sell, "sell_price_min_date": FRESH,
            "buy_price_max": buy, "buy_price_max_date": FRESH}


def cfg(**kw):
    base = {"tax": 0.0, "max_age": 6, "sell_mode": "instant",
            "refine_rrr": 0.0, "craft_rrr": 0.0, "craft_rrr_focus": 0.5}
    base.update(kw)
    return base


class IdTests(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(acp.hide_id(5, 0), "T5_HIDE")
        self.assertEqual(acp.leather_id(5, 2), "T5_LEATHER_LEVEL2@2")
        self.assertEqual(acp.jacket_id(6, 1, "SET2"), "T6_ARMOR_LEATHER_SET2@1")
        self.assertEqual(acp.jacket_id(6, 0, "SET2"), "T6_ARMOR_LEATHER_SET2")


class SaleTests(unittest.TestCase):
    def test_listing_capped_by_history_and_needs_history(self):
        prices = {("X", "Martlock"): row(sell=1000, buy=500)}
        c = cfg(sell_mode="listed")
        # No sales history: only the buy order counts
        self.assertEqual(acp.best_sale("X", ["Martlock"], prices, {}, c, NOW)[0], 500)
        # Listing above the 7-day average is valued at the average
        hist = {("X", "Martlock"): (10.0, 800.0)}
        sale = acp.best_sale("X", ["Martlock"], prices, hist, c, NOW)
        self.assertEqual((sale[0], sale[2], sale[4]), (800 * 0.975, "listed", 10.0))


class PlanTests(unittest.TestCase):
    def prices(self, jacket):
        m = "Martlock"
        return {
            ("T5_HIDE", m): row(buy=100),          # sell raw: 100/hide
            ("T4_LEATHER", m): row(sell=200),      # input for each refine
            ("T5_LEATHER", m): row(sell=500, buy=500),
            ("T5_ARMOR_LEATHER_SET1", m): row(buy=jacket),
        }

    def test_per_hide_values(self):
        p = acp.plan_tier(5, 0, self.prices(9000), {}, cfg(), NOW)
        by = {x["path"]: x["per_hide"] for x in p["paths"]}
        self.assertEqual(by["sell"], 100)
        self.assertEqual(by["refine"], (500 - 200) / 3)          # 3 hides + 1 T4 leather
        self.assertEqual(by["craft"], (9000 - 16 * 200) / 48)    # 16 leather, no return
        self.assertEqual(by["craft_focus"], (9000 - 8 * 200) / 24)
        self.assertEqual(p["pick"]["path"], "craft_focus")
        self.assertEqual(p["jackets"][0]["gain_vs_leather_focus"], 9000 - 8 * 500)

    def test_focus_needs_clear_edge(self):
        # Focus pays ~8% more than plain crafting: not worth spending focus
        p = acp.plan_tier(5, 0, self.prices(10000), {},
                          cfg(craft_rrr_focus=0.05), NOW)
        self.assertEqual(p["paths"][0]["path"], "craft_focus")
        self.assertEqual(p["pick"]["path"], "craft")


class OutputTests(unittest.TestCase):
    def test_html(self):
        m = "Martlock"
        prices = {("T5_HIDE", m): row(buy=100), ("T4_LEATHER", m): row(sell=200)}
        plan = acp.plan_tier(5, 0, prices, {}, cfg(), NOW)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.html")
            acp.write_html(path, [plan], {"generated": "x", "server": "americas",
                                          "settings": {}})
            with open(path, encoding="utf-8") as f:
                page = f.read()
        blob = page.split('type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(blob)["plans"][0]["tier"], 5)


if __name__ == "__main__":
    unittest.main()
