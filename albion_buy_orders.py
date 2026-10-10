#!/usr/bin/env python3
"""
Albion Buy Order Finder
-----------------------
Where should I place a buy order for this item?

For each market it works out what a winning buy order would cost (one silver
above the current top buy order, plus the setup fee), how much that saves
over buying from the cheapest sell order there, how many sell per day (how
fast the order is likely to fill), and what you could sell the item for
afterwards, so you can see the margin if you're flipping it.

Uses the same market data and helpers as albion_flip_scanner.

Examples
  python albion_buy_orders.py T4_BAG
  python albion_buy_orders.py T6_HIDE T6_LEATHER --premium
  python albion_buy_orders.py "Adept's Bag" --sort profit
  python albion_buy_orders.py T5_MAIN_SWORD --quality 2 --max-age 24
"""
import argparse
import sys
from datetime import datetime, timezone

import albion_flip_scanner as afs
import albion_craft_planner as acp

BRECILIEN = "Brecilien"
BUY_MARKETS = afs.ROYAL + [afs.CAERLEON, BRECILIEN]
SELL_MARKETS = BUY_MARKETS + [afs.BLACK_MARKET]


def resolve_items(queries, names):
    """Turn ids or display names into item ids. Unknown names are reported."""
    by_name = {}
    for item_id, name in names.items():
        by_name.setdefault(name.lower(), item_id)
    out = []
    for q in queries:
        q = q.strip()
        if not q:
            continue
        if q.upper() in names or "_" in q:
            out.append(q.upper() if q.upper() in names else q)
        elif q.lower() in by_name:
            out.append(by_name[q.lower()])
        else:
            hits = sorted(i for n, i in by_name.items() if q.lower() in n)
            if len(hits) == 1:
                out.append(hits[0])
            else:
                shown = ", ".join(f"{i} ({names[i]})" for i in hits[:8])
                print(f"'{q}' matches {len(hits)} items"
                      + (f": {shown}" if hits else "")
                      + ". Use an item id.", file=sys.stderr)
    return list(dict.fromkeys(out))


def order_options(item, prices, history, cfg, now):
    """
    One row per market where a buy order can be placed for `item`.

    The order price is one silver over the current top buy order, or the
    7-day average sale price when nobody has a buy order up. When that would
    reach the cheapest sell order, buying instantly is the better deal and
    the row says so.
    """
    sale = acp.best_sale(item, SELL_MARKETS, prices, history, cfg, now)
    rows = []
    for city in cfg.get("markets", BUY_MARKETS):
        row = prices.get((item, city))
        hist = history.get((item, city))
        top = afs.fresh_value(row, "buy_price_max", "buy_price_max_date",
                              now, cfg["max_age"])
        ask = afs.fresh_value(row, "sell_price_min", "sell_price_min_date",
                              now, cfg["max_age"])
        if top:
            price, basis, age = top[0] + 1, "outbid", top[1]
        elif hist:
            price, basis, age = round(hist[1]), "7-day avg", None
        else:
            continue
        r = {"item": item, "city": city, "basis": basis, "age": age,
             "top_buy": top[0] if top else None,
             "ask": ask[0] if ask else None,
             "volume": hist[0] if hist else None,
             "avg": hist[1] if hist else None}
        if ask and price >= ask[0]:
            r.update(how="instant buy", price=ask[0], cost=ask[0])
        else:
            r.update(how="buy order", price=price,
                     cost=price * (1 + afs.SETUP_FEE))
        r["saving"] = ask[0] - r["cost"] if ask else None
        # A top buy order far under what the item actually trades for is a
        # lowball nobody is filling; matching it won't get you items either.
        r["lowball"] = bool(r["avg"]) and r["price"] < r["avg"] * (
            1 - cfg.get("max_discount", 0.3))
        if sale:
            r.update(exit=sale[0], exit_city=sale[1], exit_how=sale[2],
                     profit=sale[0] - r["cost"])
        else:
            r.update(exit=None, exit_city=None, exit_how=None, profit=None)
        rows.append(r)
    return rows


def rank(rows, sort, min_volume):
    """Best first. Markets with too little trade, and lowball orders, sink to
    the bottom, since those buy orders may never fill."""
    def key(r):
        liquid = (r["volume"] or 0) >= min_volume and not r["lowball"]
        if sort == "profit":
            val = -(r["profit"] if r["profit"] is not None else float("-inf"))
        elif sort == "volume":
            val = -(r["volume"] or 0)
        else:
            val = r["cost"]
        return (not liquid, val)
    return sorted(rows, key=key)


def fmt(n):
    return "-" if n is None else f"{n:,.0f}"


def print_item(item, name, rows, min_volume):
    print(f"\n{name or item}" + (f"  ({item})" if name else ""))
    if not rows:
        print("  No recent prices or sales history in any market.")
        return
    hdr = ("Market", "Do", "Price", "Cost/unit", "vs ask", "Sold/day",
           "7d avg", "Best exit", "Profit", "Age h")
    table = []
    for r in rows:
        table.append((
            r["city"], r["how"] + (" *" if r["basis"] == "7-day avg"
                                   and r["how"] == "buy order" else "")
            + (" (lowball)" if r["lowball"] else ""),
            fmt(r["price"]), fmt(r["cost"]), fmt(r["saving"]),
            "-" if r["volume"] is None else f"{r['volume']:,.1f}",
            fmt(r["avg"]),
            f"{r['exit_city']} ({r['exit_how']})" if r["exit_city"] else "-",
            fmt(r["profit"]),
            "-" if r["age"] is None else f"{r['age']:.1f}",
        ))
    widths = [max(len(str(x)) for x in col) for col in zip(hdr, *table)]
    print("  " + "  ".join(h.ljust(w) for h, w in zip(hdr, widths)))
    print("  " + "-" * (sum(widths) + 2 * len(widths)))
    for t in table:
        print("  " + "  ".join(str(x).ljust(w) for x, w in zip(t, widths)))
    best = rows[0]
    if (best["volume"] or 0) < min_volume or best["lowball"]:
        print(f"  No market sells at least {min_volume:g}/day at a realistic "
              "price; any order may be slow to fill.")
    else:
        print(f"  -> {best['how']} in {best['city']} at {fmt(best['price'])}"
              + (f", ~{best['volume']:,.1f} sold/day" if best["volume"] else ""))
    if any(r["basis"] == "7-day avg" and r["how"] == "buy order" for r in rows):
        print("  * no buy orders seen there; priced at the 7-day average sale.")
    if any(r["lowball"] for r in rows):
        print("  (lowball) top buy order is far below the 7-day average sale, "
              "so matching it is unlikely to fill.")


def build_parser():
    p = argparse.ArgumentParser(
        description="Find the best market to place a buy order for an item")
    p.add_argument("items", nargs="+",
                   help="item ids (T4_BAG) or names (\"Adept's Bag\")")
    p.add_argument("--server", choices=afs.HOSTS, default="americas")
    p.add_argument("--quality", type=int, default=1, choices=[1, 2, 3, 4, 5])
    p.add_argument("--premium", action="store_true",
                   help="use premium tax rate (4%% instead of 8%%)")
    p.add_argument("--max-age", type=float, default=12.0,
                   help="ignore prices older than this many hours")
    p.add_argument("--min-volume", type=float, default=1.0,
                   help="markets selling fewer per day rank last")
    p.add_argument("--max-discount", type=float, default=0.3,
                   help="orders more than this share under the 7-day average "
                        "count as lowballs that rarely fill")
    p.add_argument("--sort", choices=["cost", "profit", "volume"],
                   default="cost",
                   help="cost = cheapest to buy, profit = best margin on resale")
    p.add_argument("--sell-mode", choices=["instant", "listed"],
                   default="listed",
                   help="how the resale is valued: sell order or into buy orders")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    host = afs.HOSTS[args.server]
    now = datetime.now(timezone.utc)
    cfg = {"tax": afs.TAX_PREMIUM if args.premium else afs.TAX_NO_PREMIUM,
           "max_age": args.max_age, "sell_mode": args.sell_mode,
           "max_discount": args.max_discount}

    names = {}
    try:
        names = afs.load_item_names()
    except Exception as e:
        print(f"Could not load item names ({e}); only built-in gear names "
              "are recognised.", file=sys.stderr)
        names = {i: afs.builtin_name(i)
                 for i in afs.builtin_items(range(1, 9))}
    items = resolve_items(args.items, names)
    if not items:
        sys.exit("No items to look up.")

    prices = afs.fetch_prices(host, items, SELL_MARKETS, args.quality,
                              progress=False)
    history = acp.fetch_history(host, items, SELL_MARKETS, args.quality)

    results = {}
    for item in items:
        rows = rank(order_options(item, prices, history, cfg, now),
                    args.sort, args.min_volume)
        name = names.get(item) or afs.builtin_name(item)
        print_item(item, name, rows, args.min_volume)
        results[item] = rows
    print("\nCost includes the 2.5% setup fee. Profit is after sales tax "
          f"({cfg['tax'] * 100:.0f}%) and doesn't count transport risk. "
          "Check prices in-game first.")
    return results


if __name__ == "__main__":
    main()
