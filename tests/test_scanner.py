"""Offline unit tests for albion_flip_scanner. No network access needed."""
import contextlib
import csv
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import albion_flip_scanner as afs  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def ts(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def cfg(**overrides):
    base = {
        "tax": afs.TAX_NO_PREMIUM, "max_age": 6.0, "min_profit": 5000,
        "min_roi": 5.0, "max_roi": 200.0, "sell_mode": "instant",
        "budget": 10_000_000, "risk_frac": 0.25, "med_frac": 0.5,
        "vol_share": 0.25,
    }
    base.update(overrides)
    return base


def row(sell_min=0, sell_age=1.0, buy_max=0, buy_age=1.0):
    return {
        "sell_price_min": sell_min, "sell_price_min_date": ts(sell_age),
        "buy_price_max": buy_max, "buy_price_max_date": ts(buy_age),
    }


class ParseTsTests(unittest.TestCase):
    def test_empty_and_zero_dates(self):
        self.assertIsNone(afs.parse_ts(""))
        self.assertIsNone(afs.parse_ts(None))
        self.assertIsNone(afs.parse_ts("0001-01-01T00:00:00"))

    def test_naive_becomes_utc(self):
        dt = afs.parse_ts("2026-01-01T10:00:00")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.hour, 10)

    def test_garbage(self):
        self.assertIsNone(afs.parse_ts("not a date"))


class ItemListTests(unittest.TestCase):
    def test_item_line_regex(self):
        m = afs.ITEM_LINE.match("  123: T4_BAG                : Adept's Bag")
        self.assertEqual(m.group(1), "T4_BAG")
        self.assertEqual(m.group(2).strip(), "Adept's Bag")

    def test_auto_items_filters_gear_and_tiers(self):
        names = {
            "T4_MAIN_SWORD": "", "T5_2H_BOW": "", "T7_HEAD_CLOTH_SET1": "",
            "T4_BAG": "", "T4_BAG_INSIGHT": "", "T4_WOOD": "",
            "T6_ARMOR_LEATHER_SET2@1": "", "T5_CAPE": "", "T4_BAGPACK": "",
        }
        self.assertEqual(afs.auto_items(names, {4, 5}),
                         ["T4_BAG", "T4_BAG_INSIGHT", "T4_MAIN_SWORD",
                          "T5_2H_BOW", "T5_CAPE"])

    def test_builtin_items(self):
        items = afs.builtin_items({4, 6})
        self.assertIn("T4_BAG", items)
        self.assertIn("T6_ARMOR_PLATE_SET1", items)
        self.assertEqual(len(items), 2 * len(afs.BUILTIN_GEAR))
        self.assertTrue(all(afs.GEAR_PATTERN.match(i) for i in items))

    def test_builtin_name(self):
        self.assertEqual(afs.builtin_name("T6_ARMOR_CLOTH_SET2"), "Master's Cleric Robe")
        self.assertEqual(afs.builtin_name("T5_HEAD_LEATHER_MORGANA"), "Expert's Stalker Hood")
        self.assertEqual(afs.builtin_name("T8_MAIN_SWORD@3"), "Elder's Broadsword .3")
        self.assertIsNone(afs.builtin_name("T4_WOOD"))

    def test_read_items_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "items.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# my list\nT4_BAG, T5_BAG\nT6_CAPE  # cape\n\n")
            self.assertEqual(afs.read_items_file(path),
                             ["T4_BAG", "T5_BAG", "T6_CAPE"])

    def test_load_item_names_uses_cache_when_offline(self):
        text = "  1: T4_BAG : Adept's Bag\n"
        real = afs.http_get
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "sub", "items.txt")
            try:
                afs.http_get = lambda url, retries=4: text
                self.assertEqual(afs.load_item_names(cache), {"T4_BAG": "Adept's Bag"})
                self.assertTrue(os.path.exists(cache))

                def offline(url, retries=4):
                    raise urllib.error.URLError("blocked")
                afs.http_get = offline
                self.assertEqual(afs.load_item_names(cache), {"T4_BAG": "Adept's Bag"})
                with self.assertRaises(urllib.error.URLError):
                    afs.load_item_names(os.path.join(d, "missing.txt"))
            finally:
                afs.http_get = real


class TransientTests(unittest.TestCase):
    def http_error(self, code):
        return urllib.error.HTTPError("u", code, "x", {}, None)

    def test_retryable(self):
        self.assertTrue(afs._transient(self.http_error(429)))
        self.assertTrue(afs._transient(self.http_error(503)))
        self.assertTrue(afs._transient(urllib.error.URLError(socket.timeout())))
        self.assertTrue(afs._transient(ConnectionResetError()))

    def test_not_retryable(self):
        self.assertFalse(afs._transient(self.http_error(403)))
        self.assertFalse(afs._transient(self.http_error(404)))
        self.assertFalse(afs._transient(urllib.error.URLError(
            OSError("Tunnel connection failed: 403 Forbidden"))))


class ChunkTests(unittest.TestCase):
    def test_chunks_respect_max_url(self):
        items = ["T4_MAIN_SWORD_%03d" % i for i in range(1000)]
        base = 200
        chunks = list(afs.chunk_items(items, base))
        self.assertGreater(len(chunks), 1)
        self.assertEqual(sum(chunks, []), items)
        for c in chunks:
            self.assertLessEqual(base + sum(len(i) + 1 for i in c), afs.MAX_URL)

    def test_empty(self):
        self.assertEqual(list(afs.chunk_items([], 100)), [])


class FreshValueTests(unittest.TestCase):
    def test_missing_row_or_price(self):
        self.assertIsNone(afs.fresh_value(None, "sell_price_min",
                                          "sell_price_min_date", NOW, 6))
        self.assertIsNone(afs.fresh_value(row(sell_min=0), "sell_price_min",
                                          "sell_price_min_date", NOW, 6))

    def test_stale(self):
        r = row(sell_min=100, sell_age=7)
        self.assertIsNone(afs.fresh_value(r, "sell_price_min",
                                          "sell_price_min_date", NOW, 6))

    def test_fresh(self):
        price, age = afs.fresh_value(row(sell_min=100, sell_age=2),
                                     "sell_price_min", "sell_price_min_date",
                                     NOW, 6)
        self.assertEqual(price, 100)
        self.assertAlmostEqual(age, 2.0)


class EvaluateTests(unittest.TestCase):
    def test_instant_profit(self):
        prices = {
            ("X", "Lymhurst"): row(sell_min=100_000),
            ("X", "Martlock"): row(buy_max=150_000, buy_age=3),
        }
        c = afs.evaluate("X", "Lymhurst", "Martlock", prices, cfg(), NOW)
        self.assertEqual(c["how"], "instant")
        self.assertEqual(c["profit"], round(150_000 * 0.92 - 100_000))
        self.assertEqual(c["risk"], "MED")
        self.assertAlmostEqual(c["data_age_h"], 3.0)

    def test_listed_mode_uses_setup_fee(self):
        prices = {
            ("X", "Lymhurst"): row(sell_min=100_000),
            ("X", "Martlock"): row(sell_min=160_000, buy_max=120_000),
        }
        c = afs.evaluate("X", "Lymhurst", "Martlock", prices,
                         cfg(sell_mode="listed"), NOW)
        self.assertEqual(c["how"], "listed")
        expected = round(160_000 * (1 - afs.TAX_NO_PREMIUM - afs.SETUP_FEE)
                         - 100_000)
        self.assertEqual(c["profit"], expected)

    def test_black_market_never_listed(self):
        prices = {
            ("X", "Lymhurst"): row(sell_min=100_000),
            ("X", afs.BLACK_MARKET): row(sell_min=500_000, buy_max=130_000),
        }
        c = afs.evaluate("X", "Lymhurst", afs.BLACK_MARKET, prices,
                         cfg(sell_mode="listed"), NOW)
        self.assertEqual(c["how"], "instant")
        self.assertEqual(c["sell"], 130_000)
        self.assertEqual(c["risk"], "HIGH")

    def test_filters(self):
        prices = {
            ("X", "Lymhurst"): row(sell_min=100_000),
            ("X", "Martlock"): row(buy_max=110_000),
        }
        # 110k * 0.92 = 101.2k -> profit 1.2k, below min_profit
        self.assertIsNone(afs.evaluate("X", "Lymhurst", "Martlock", prices,
                                       cfg(), NOW))
        prices[("X", "Martlock")] = row(buy_max=1_000_000)
        self.assertIsNone(afs.evaluate("X", "Lymhurst", "Martlock", prices,
                                       cfg(), NOW))  # ROI > max_roi

    def test_premium_tax(self):
        prices = {
            ("X", "Lymhurst"): row(sell_min=100_000),
            ("X", "Martlock"): row(buy_max=150_000),
        }
        c = afs.evaluate("X", "Lymhurst", "Martlock", prices,
                         cfg(tax=afs.TAX_PREMIUM), NOW)
        self.assertEqual(c["profit"], round(150_000 * 0.96 - 100_000))


class SizePositionTests(unittest.TestCase):
    def cand(self, risk="MED"):
        return {"item": "X", "dst": "Martlock", "buy": 100_000,
                "profit": 20_000, "risk": risk}

    def test_budget_cap(self):
        c = afs.size_position(self.cand(), cfg(), {})
        self.assertEqual(c["qty"], 50)  # 10M * 0.5 / 100k
        self.assertIsNone(c["volume"])
        self.assertEqual(c["total_profit"], 50 * 20_000)
        c = afs.size_position(self.cand("HIGH"), cfg(), {})
        self.assertEqual(c["qty"], 25)

    def test_volume_cap(self):
        c = afs.size_position(self.cand(), cfg(), {("X", "Martlock"): 40.0})
        self.assertEqual(c["qty"], 10)  # 40 * 0.25
        self.assertEqual(c["total_cost"], 10 * 100_000)


class OutputTests(unittest.TestCase):
    def test_csv_roundtrip(self):
        c = afs.size_position({
            "item": "T4_BAG", "src": "Lymhurst", "dst": "Martlock",
            "how": "instant", "buy": 1000, "sell": 2000, "profit": 840,
            "roi": 84.0, "data_age_h": 1.0, "risk": "MED",
        }, cfg(), {})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.csv")
            afs.write_csv(path, [c], {"T4_BAG": "Adept's Bag"})
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Adept's Bag")
        self.assertEqual(rows[0]["qty"], str(c["qty"]))

    def test_empty_csv_has_header(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.csv")
            afs.write_csv(path, [], {})
            with open(path, encoding="utf-8") as f:
                self.assertTrue(f.read().startswith("item,name,src,dst"))

    def test_html_embeds_rows(self):
        c = afs.size_position({
            "item": "T4_BAG", "src": "Lymhurst", "dst": "Martlock",
            "how": "instant", "buy": 1000, "sell": 2000, "profit": 840,
            "roi": 84.0, "data_age_h": 1.0, "risk": "MED",
        }, cfg(), {})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.html")
            afs.write_html(path, [c], {"T4_BAG": "</script>Bag"},
                           {"generated": "x", "server": "americas", "settings": {}})
            with open(path, encoding="utf-8") as f:
                page = f.read()
        self.assertNotIn("__SCAN_JSON__", page)
        blob = page.split('type="application/json">', 1)[1].split("</script>", 1)[0]
        data = json.loads(blob)
        self.assertEqual(data["rows"][0]["name"], "</script>Bag")
        self.assertEqual(data["rows"][0]["qty"], c["qty"])

    def test_table_label(self):
        c = afs.size_position({
            "item": "T4_BAG", "src": "Lymhurst", "dst": "Martlock",
            "how": "instant", "buy": 1000, "sell": 2000, "profit": 840,
            "roi": 84.0, "data_age_h": 1.0, "risk": "MED",
        }, cfg(), {})
        for names, want in (({"T4_BAG": "Adept's Bag"}, "Adept's Bag "),
                            ({}, "T4_BAG ")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                afs.print_table([c], names)
            self.assertTrue(out.getvalue().splitlines()[2].startswith(want))

    def test_parser_defaults(self):
        args = afs.build_parser().parse_args([])
        self.assertEqual(args.server, "americas")
        self.assertEqual(args.mode, "both")
        self.assertEqual(args.csv, "flips.csv")


if __name__ == "__main__":
    unittest.main()
