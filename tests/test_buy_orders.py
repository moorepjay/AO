"""Offline unit tests for albion_buy_orders. No network access needed."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import albion_buy_orders as abo  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")


def row(sell=0, buy=0):
    return {"sell_price_min": sell, "sell_price_min_date": FRESH,
            "buy_price_max": buy, "buy_price_max_date": FRESH}


def cfg(**kw):
    base = {"tax": 0.0, "max_age": 6, "sell_mode": "instant",
            "max_discount": 0.3, "markets": ["Martlock", "Lymhurst"]}
    base.update(kw)
    return base


class OrderOptionTests(unittest.TestCase):
    def options(self, prices, history, **kw):
        rows = abo.order_options("X", prices, history, cfg(**kw), NOW)
        return {r["city"]: r for r in rows}

    def test_outbids_top_order_and_adds_setup_fee(self):
        r = self.options({("X", "Martlock"): row(sell=1200, buy=1000)},
                         {("X", "Martlock"): (50, 1100)})["Martlock"]
        self.assertEqual((r["how"], r["price"]), ("buy order", 1001))
        self.assertAlmostEqual(r["cost"], 1001 * 1.025)
        self.assertAlmostEqual(r["saving"], 1200 - 1001 * 1.025)
        self.assertFalse(r["lowball"])

    def test_instant_buy_when_spread_is_closed(self):
        r = self.options({("X", "Martlock"): row(sell=1000, buy=1000)},
                         {})["Martlock"]
        self.assertEqual((r["how"], r["cost"]), ("instant buy", 1000))

    def test_falls_back_to_history_and_skips_unknown_markets(self):
        got = self.options({}, {("X", "Lymhurst"): (10, 900)})
        self.assertNotIn("Martlock", got)
        self.assertEqual((got["Lymhurst"]["basis"], got["Lymhurst"]["price"]),
                         ("7-day avg", 900))

    def test_lowball_and_profit(self):
        prices = {("X", "Martlock"): row(buy=500),
                  ("X", "Lymhurst"): row(buy=2000)}
        got = self.options(prices, {("X", "Martlock"): (50, 1000)})
        self.assertTrue(got["Martlock"]["lowball"])
        self.assertEqual(got["Martlock"]["exit_city"], "Lymhurst")
        self.assertAlmostEqual(got["Martlock"]["profit"], 2000 - 501 * 1.025)


class RankTests(unittest.TestCase):
    def test_illiquid_and_lowball_rank_last(self):
        rows = [
            {"city": "cheap-lowball", "cost": 10, "volume": 99, "lowball": True, "profit": 9},
            {"city": "cheap-dead", "cost": 20, "volume": 0, "lowball": False, "profit": 8},
            {"city": "ok", "cost": 50, "volume": 5, "lowball": False, "profit": 1},
            {"city": "best", "cost": 40, "volume": 5, "lowball": False, "profit": 2},
        ]
        self.assertEqual([r["city"] for r in abo.rank(rows, "cost", 1)][:2],
                         ["best", "ok"])
        self.assertEqual(abo.rank(rows, "profit", 1)[0]["city"], "best")


class ResolveTests(unittest.TestCase):
    def test_ids_and_names(self):
        names = {"T4_BAG": "Adept's Bag", "T5_BAG": "Expert's Bag"}
        got = abo.resolve_items(["t4_bag", "expert's bag", "T6_HIDE", "bag"],
                                names)
        self.assertEqual(got, ["T4_BAG", "T5_BAG", "T6_HIDE"])


if __name__ == "__main__":
    unittest.main()
