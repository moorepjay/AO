import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import albion_ledger as al  # noqa: E402

HEADER = "date,character,action,item_id,name,qty,price,how,city,fee,note\n"


def books(text, tax=0.04):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write(HEADER + text)
        trades = al.read_trades([path])
    totals, items = al.build_books(trades, tax)
    return trades, totals, items


class FeeTests(unittest.TestCase):
    def test_fees(self):
        t = {"action": "sell", "how": "order", "qty": 1, "price": 1000, "fee": None}
        self.assertAlmostEqual(al.trade_fee(t, 0.04), 65)
        t.update(how="instant")
        self.assertAlmostEqual(al.trade_fee(t, 0.04), 40)
        t.update(action="buy", how="order")
        self.assertAlmostEqual(al.trade_fee(t, 0.04), 25)
        t.update(how="instant")
        self.assertEqual(al.trade_fee(t, 0.04), 0)
        t.update(fee=123.0)
        self.assertEqual(al.trade_fee(t, 0.04), 123)


class BookTests(unittest.TestCase):
    def test_average_cost_profit_and_holdings(self):
        _, totals, items = books(
            "2026-10-01,A,buy,X,X,2,1000,instant,M,,\n"
            "2026-10-02,A,buy,X,X,2,2000,instant,M,,\n"
            "2026-10-03,A,sell,X,X,1,3000,instant,M,0,\n")
        x = items["X"]
        self.assertEqual(x["held"], 3)
        self.assertEqual(x["profit"], 3000 - 1500)          # average cost 1500
        self.assertEqual(x["held_cost"], 4500)
        self.assertEqual(totals["net"], 3000 - 6000)

    def test_sales_without_cost_and_use(self):
        _, totals, items = books(
            "2026-10-01,A,sell,L,Loot,1,1000,instant,M,0,\n"
            "2026-10-01,A,buy,E,Energy,10,100,instant,M,,\n"
            "2026-10-02,A,use,E,Energy,10,0,,,,\n")
        self.assertEqual(totals["uncosted"], 1000)
        self.assertEqual(totals["profit"], 0)
        self.assertEqual((items["E"]["held"], totals["used_cost"]), (0, 1000))

    def test_selling_more_than_bought(self):
        _, totals, items = books(
            "2026-10-01,A,buy,X,X,1,100,instant,M,,\n"
            "2026-10-02,A,sell,X,X,3,200,instant,M,0,\n")
        self.assertEqual(items["X"]["profit"], 100)
        self.assertEqual(items["X"]["uncosted_income"], 400)

    def test_weekly_and_html(self):
        trades, totals, items = books(
            "2026-10-05,A,buy,X,X,1,100,instant,M,,\n"
            "2026-10-07,A,sell,X,</script>,1,300,instant,M,0,\n")
        weeks = al.weekly(trades)
        self.assertEqual(weeks["2026-10-05"], {"in": 300, "out": 100})
        al.value_holdings(items, {})
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "l.html")
            al.write_html(out, trades, totals, items, weeks,
                          {"generated": "x", "server": "americas", "tax": 0.04, "setup_fee": 0.025})
            with open(out, encoding="utf-8") as f:
                page = f.read()
        blob = page.split('type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(blob)["totals"]["net"], 200)


if __name__ == "__main__":
    unittest.main()
