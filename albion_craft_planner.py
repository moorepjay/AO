#!/usr/bin/env python3
"""
Albion Craft Planner (leather)
------------------------------
For a gatherer who refines and crafts their own hides along the chain

  gather and bank (Caerleon) -> refine (Martlock, hide bonus)
  -> craft jackets (Thetford, leather armor bonus) -> sell

what is each hide you gathered worth if you

  * sell it raw where it's banked,
  * take it to Martlock, refine it and sell the leather, or
  * refine it in Martlock, craft a leather jacket in Thetford and sell that?

Prices are opportunity costs: your own hides are valued at what you could sell
them for, and the lower-tier leather each refine needs at what it costs to buy.
Uses the same market data and helpers as albion_flip_scanner.

Examples
  python albion_craft_planner.py
  python albion_craft_planner.py --tiers 5,6 --enchants 0,1 --premium
  python albion_craft_planner.py --refine-rrr 0.539   # refine with focus too
  python albion_craft_planner.py --html plan.html
"""
import argparse
import json
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import albion_flip_scanner as afs

# Hides per refine (plus one leather of the tier below, except T2).
HIDES_PER_LEATHER = {2: 1, 3: 2, 4: 2, 5: 3, 6: 4, 7: 5, 8: 5}
LEATHER_PER_JACKET = 16
JACKETS = {"SET1": "Mercenary Jacket", "SET2": "Hunter Jacket",
           "SET3": "Assassin Jacket"}

# Where each step happens, and its resource return rate. Refining hides gets
# Martlock's bonus; crafting leather armor gets Thetford's. VERIFY in-game.
HOME = afs.CAERLEON
REFINE_CITY = "Martlock"
CRAFT_CITY = "Thetford"
REFINE_RRR = 0.367        # Martlock, no focus (0.539 with focus)
CRAFT_RRR = 0.248         # Thetford, no focus
CRAFT_RRR_FOCUS = 0.479   # Thetford, with focus
FOCUS_EDGE = 0.10  # recommend focus only when it pays at least 10% more


def ench_suffix(e):
    return f"_LEVEL{e}@{e}" if e else ""


def hide_id(t, e):
    return f"T{t}_HIDE{ench_suffix(e)}"


def leather_id(t, e):
    return f"T{t}_LEATHER{ench_suffix(e)}"


def jacket_id(t, e, s):
    return f"T{t}_ARMOR_LEATHER_{s}" + (f"@{e}" if e else "")


def label(t, e, what):
    return f"{what} {t}.{e}"


# --------------------------------------------------------------------------
# Market lookups
# --------------------------------------------------------------------------
def fetch_history(host, items, cities, quality=1, days=7, pause=0.4):
    """{(item, city): (avg items sold per day, avg sale price)} over `days`."""
    loc_q = urllib.parse.quote(",".join(cities), safe=",")
    suffix = f".json?locations={loc_q}&qualities={quality}&time-scale=24"
    base_len = len(host) + len("/api/v2/stats/history/") + len(suffix)
    out = {}
    for chunk in afs.chunk_items(items, base_len):
        url = f"{host}/api/v2/stats/history/{','.join(chunk)}{suffix}"
        try:
            rows = afs.get_json(url)
        except Exception as e:  # history only refines the estimate
            print(f"  history fetch failed: {e}", file=sys.stderr)
            continue
        for r in rows:
            data = (r.get("data") or [])[-days:]
            count = sum(d.get("item_count", 0) for d in data)
            if count:
                avg_price = sum(d.get("avg_price", 0) * d.get("item_count", 0)
                                for d in data) / count
                out[(r["item_id"], r["location"])] = (count / days, avg_price)
        time.sleep(pause)
    return out


def best_sale(item, cities, prices, history, cfg, now):
    """
    Best net silver per unit from selling `item`, as
    (net, city, how, age, sold_per_day). Sell orders are valued at the lower
    of the current listing and the recent average sale price, and only where
    the item has actually been selling, so troll listings don't count.
    """
    best = None
    for city in cities:
        row = prices.get((item, city))
        hist = history.get((item, city))
        vol = hist[0] if hist else None
        opts = []
        inst = afs.fresh_value(row, "buy_price_max", "buy_price_max_date",
                               now, cfg["max_age"])
        if inst:
            opts.append((inst[0] * (1 - cfg["tax"]), "instant", inst[1]))
        if cfg["sell_mode"] == "listed" and city != afs.BLACK_MARKET and hist:
            lst = afs.fresh_value(row, "sell_price_min", "sell_price_min_date",
                                  now, cfg["max_age"])
            if lst:
                price = min(lst[0], hist[1])
                opts.append((price * (1 - cfg["tax"] - afs.SETUP_FEE),
                             "listed", lst[1]))
        if not opts and hist:
            # No current snapshot (nobody with the data client opened this
            # market lately), but it is trading: use the 7-day average.
            opts.append((hist[1] * (1 - cfg["tax"]), "avg", None))
        for net, how, age in opts:
            if best is None or net > best[0]:
                best = (net, city, how, age, vol)
    return best


def cheapest_buy(item, cities, prices, cfg, now, history=None):
    """Lowest sell order for `item`, as (price, city, age); falls back to the
    7-day average sale price (age None) where there's no current listing."""
    best = None
    for city in cities:
        v = afs.fresh_value(prices.get((item, city)), "sell_price_min",
                            "sell_price_min_date", now, cfg["max_age"])
        if not v and history and (item, city) in history:
            v = (history[(item, city)][1], None)
        if v and (best is None or v[0] < best[0]):
            best = (v[0], city, v[1])
    return best


def _oldest(*ages):
    known = [a for a in ages if a is not None]
    return max(known) if known else None


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
def plan_tier(t, e, prices, history, cfg, now):
    """Per-hide value of each path for hides of tier t, enchant e."""
    r, rc = cfg["refine_rrr"], cfg["craft_rrr"]
    markets = cfg.get("markets", afs.ROYAL + [afs.CAERLEON])
    gear_markets = cfg.get("gear_markets", markets + [afs.BLACK_MARKET])
    n = HIDES_PER_LEATHER[t]

    raw = best_sale(hide_id(t, e), cfg.get("raw_markets", markets), prices,
                    history, cfg, now)
    # Leather only exists where you refine it, so it's bought and sold there
    leather_markets = cfg.get("leather_markets", markets)
    prev = (cheapest_buy(leather_id(t - 1, 0), leather_markets, prices, cfg, now, history)
            if t > 2 else (0, "", 0))
    leather_sale = best_sale(leather_id(t, e), leather_markets, prices, history, cfg, now)
    leather_buy = cheapest_buy(leather_id(t, e), leather_markets, prices, cfg, now, history)

    hides_per_leather = (1 - r) * n
    prev_per_leather = (1 - r) if t > 2 else 0

    paths = []
    if raw:
        paths.append({"path": "sell", "what": label(t, e, "Hide"),
                      "per_hide": raw[0], "city": raw[1], "how": raw[2],
                      "age": raw[3], "volume": raw[4]})
    if leather_sale and prev:
        per = (leather_sale[0] - prev_per_leather * prev[0]) / hides_per_leather
        paths.append({"path": "refine", "what": label(t, e, "Leather"),
                      "per_hide": per, "city": leather_sale[1],
                      "how": leather_sale[2], "age": _oldest(leather_sale[3], prev[2]),
                      "volume": leather_sale[4]})

    jackets = []
    for s, jname in JACKETS.items():
        sale = best_sale(jacket_id(t, e, s), gear_markets, prices, history, cfg, now)
        if not sale:
            continue
        j = {"item": jacket_id(t, e, s), "name": label(t, e, jname),
             "tier": t, "ench": e, "sell": sale[0], "city": sale[1],
             "how": sale[2], "age": sale[3], "volume": sale[4]}
        for key, path, rrr in (("", "craft", rc),
                               ("_focus", "craft_focus", cfg["craft_rrr_focus"])):
            leathers = LEATHER_PER_JACKET * (1 - rrr)
            j["leather_per" + key] = leathers
            if leather_buy:
                # Margin if you buy the leather instead of refining your own
                j["profit_bought" + key] = sale[0] - leathers * leather_buy[0]
            if leather_sale:
                # Extra silver per jacket compared with selling that leather
                j["gain_vs_leather" + key] = sale[0] - leathers * leather_sale[0]
            if prev:
                per = ((sale[0] - leathers * prev_per_leather * prev[0])
                       / (leathers * hides_per_leather))
                j["per_hide" + key] = per
                paths.append({"path": path, "what": j["name"], "per_hide": per,
                              "city": sale[1], "how": sale[2],
                              "age": _oldest(sale[3], prev[2]), "volume": sale[4]})
        jackets.append(j)

    paths.sort(key=lambda p: p["per_hide"], reverse=True)
    # Focus is scarce: only recommend it when it clearly beats every
    # no-focus option.
    plain = [p for p in paths if p["path"] != "craft_focus"]
    pick = plain[0] if plain else None
    if paths and paths[0]["path"] == "craft_focus" and (
            pick is None or paths[0]["per_hide"] >= pick["per_hide"] * (1 + FOCUS_EDGE)):
        pick = paths[0]
    return {
        "pick": pick,
        "tier": t, "ench": e, "hide": hide_id(t, e),
        "raw": raw[0] if raw else None,
        "prev_leather": prev[0] if t > 2 and prev else None,
        "leather_buy": leather_buy[0] if leather_buy else None,
        "paths": paths, "jackets": jackets,
    }


def collect_items(tiers, enchants):
    items = set()
    for t in tiers:
        items.add(leather_id(t - 1, 0))
        for e in enchants:
            items.update({hide_id(t, e), leather_id(t, e)})
            items.update(jacket_id(t, e, s) for s in JACKETS)
    return sorted(items)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
PATH_LABELS = {"sell": "sell hides", "refine": "sell leather",
               "craft": "craft", "craft_focus": "craft (focus)"}


def fmt(n):
    return "-" if n is None else f"{n:,.0f}"


def print_plan(plans):
    print("What to do with your hides (silver per hide)")
    hdr = ("Hide", "Sell raw", "Refine+sell", "Best jacket", "Jacket/hide",
           "w/ focus", "Do this", "Where")
    rows = []
    for p in plans:
        by = {}
        for x in p["paths"]:
            by.setdefault(x["path"], x)
        best = p["pick"]
        rows.append((
            f"{p['tier']}.{p['ench']}", fmt(p["raw"]),
            fmt(by["refine"]["per_hide"]) if "refine" in by else "-",
            by["craft"]["what"] if "craft" in by else "-",
            fmt(by["craft"]["per_hide"]) if "craft" in by else "-",
            fmt(by["craft_focus"]["per_hide"]) if "craft_focus" in by else "-",
            PATH_LABELS[best["path"]] if best else "no data",
            f"{best['city']} ({'7-day avg' if best['how'] == 'avg' else best['how']})" if best else "",
        ))
    widths = [max(len(str(x)) for x in col) for col in zip(hdr, *rows)]
    print("  ".join(h.ljust(w) for h, w in zip(hdr, widths)))
    print("-" * (sum(widths) + 2 * len(widths)))
    for r in rows:
        print("  ".join(str(x).ljust(w) for x, w in zip(r, widths)))


def write_html(path, scenarios, meta):
    data = dict(meta, scenarios=scenarios)
    blob = json.dumps(data).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE.replace("__PLAN_JSON__", blob))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="Albion leather craft planner")
    p.add_argument("--server", choices=afs.HOSTS, default="americas")
    p.add_argument("--tiers", default="4,5,6,7,8")
    p.add_argument("--enchants", default="0,1,2,3")
    p.add_argument("--premium", action="store_true",
                   help="use premium tax rate (4%% instead of 8%%)")
    p.add_argument("--home", choices=afs.ROYAL + [afs.CAERLEON], default=HOME,
                   help="where your gathered hides are banked")
    p.add_argument("--refine-rrr", type=float, default=REFINE_RRR,
                   help="refining return rate in Martlock (default: no focus)")
    p.add_argument("--craft-rrr", type=float, default=CRAFT_RRR,
                   help="jacket crafting return rate in Thetford, no focus")
    p.add_argument("--craft-rrr-focus", type=float, default=CRAFT_RRR_FOCUS,
                   help="jacket crafting return rate in Thetford, with focus")
    p.add_argument("--sell-mode", choices=["instant", "listed"], default="listed",
                   help="royal-city sales: list a sell order, or sell into buy orders")
    p.add_argument("--max-age", type=float, default=24.0,
                   help="ignore prices older than this many hours")
    p.add_argument("--vol-share", type=float, default=0.25,
                   help="suggest crafting at most this share of daily sales")
    p.add_argument("--html", help="also write a results page to this file")
    return p


def scenarios_for(args, base):
    """
    Session plans to compare. One for now: the gather -> Martlock -> Thetford
    chain. Hides sell raw only where they're banked (anything else means
    hauling them anyway); leather is made in the royal cities and is bought and
    sold there, never carried back to Caerleon; jackets sell wherever pays
    most, including the Black Market.
    """
    return [("chain", "Gather, refine, craft",
             f"Hides banked in {args.home}, refined in {REFINE_CITY}, jackets "
             f"crafted in {CRAFT_CITY}. Leather is bought and sold in royal cities; "
             "jackets sell wherever pays most.",
             dict(base, refine_rrr=args.refine_rrr, raw_markets=[args.home],
                  leather_markets=afs.ROYAL))]


def main(argv=None):
    args = build_parser().parse_args(argv)
    host = afs.HOSTS[args.server]
    now = datetime.now(timezone.utc)
    tiers = [int(t) for t in args.tiers.split(",")]
    enchants = [int(e) for e in args.enchants.split(",")]
    base = {"tax": afs.TAX_PREMIUM if args.premium else afs.TAX_NO_PREMIUM,
            "max_age": args.max_age, "sell_mode": args.sell_mode,
            "craft_rrr": args.craft_rrr, "craft_rrr_focus": args.craft_rrr_focus}

    items = collect_items(tiers, enchants)
    locations = afs.ROYAL + [afs.CAERLEON, afs.BLACK_MARKET]
    print(f"Pricing {len(items)} items on {args.server}...", file=sys.stderr)
    prices = afs.fetch_prices(host, items, locations, 1)
    print("Checking sales history...", file=sys.stderr)
    history = fetch_history(host, items, locations)

    scenarios = []
    for key, title, blurb, cfg in scenarios_for(args, base):
        plans = [plan_tier(t, e, prices, history, cfg, now)
                 for t in tiers for e in enchants]
        for p in plans:
            for j in p["jackets"]:
                v = j["volume"]
                j["suggest"] = int(v * args.vol_share) if v is not None else None
        print(f"\n== {title} ==")
        print_plan(plans)
        scenarios.append({
            "key": key, "title": title, "blurb": blurb, "plans": plans,
            "settings": {"tax": round(cfg["tax"] * 100, 1),
                         "home": args.home, "refine_city": REFINE_CITY,
                         "craft_city": CRAFT_CITY,
                         "refine_rrr": cfg["refine_rrr"],
                         "craft_rrr": cfg["craft_rrr"],
                         "craft_rrr_focus": cfg["craft_rrr_focus"],
                         "sell_mode": cfg["sell_mode"],
                         "max_age": args.max_age, "vol_share": args.vol_share},
        })

    if args.html:
        write_html(args.html, scenarios,
                   {"generated": now.isoformat(), "server": args.server})
        print(f"\nWrote plan page to {args.html}")
    return scenarios


HTML_TEMPLATE = r"""<title>Leather Session Plan</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
/* Layout: one column — header, how-to-read strip, hide decision ledger (one row per tier.enchant), jacket craft queue. */
:root {
  --bg: #f3f4f7; --panel: #ffffff; --fg: #1b2230; --muted: #5d6779; --line: #dde1e8;
  --accent: #b07a12; --accent-soft: #f6ead0;
  --good: #1f7a4d; --good-bg: #dff1e7; --bad: #b3261e;
  --sell: #6b7280; --refine: #8a5a2b; --craft: #1d4f91; --focus: #7a3fb0;
  --display: "Archivo", system-ui, sans-serif;
  --body: "Archivo", system-ui, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #12161d; --panel: #1a2029; --fg: #e6e9ef; --muted: #98a2b3; --line: #2b3340;
  --accent: #e0aa45; --accent-soft: #3a2f19;
  --good: #5fc796; --good-bg: #183a2a; --bad: #f08a80;
  --sell: #a0a7b4; --refine: #d79a5f; --craft: #8ab8f0; --focus: #c49af0; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #12161d; --panel: #1a2029; --fg: #e6e9ef; --muted: #98a2b3; --line: #2b3340;
  --accent: #e0aa45; --accent-soft: #3a2f19;
  --good: #5fc796; --good-bg: #183a2a; --bad: #f08a80;
  --sell: #a0a7b4; --refine: #d79a5f; --craft: #8ab8f0; --focus: #c49af0; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font-family: var(--body); font-size: 14px; }
.wrap { max-width: 1200px; margin: 0 auto; padding-inline: 16px; padding-block: 24px 48px; display: grid; gap: 20px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 24px; }
h1 { font-family: var(--display); font-weight: 800; font-size: 28px; margin: 0; letter-spacing: -0.01em; text-wrap: balance; }
h1 span { color: var(--accent); }
h2 { font-family: var(--display); font-size: 18px; margin: 8px 0 0; }
.lede { color: var(--muted); max-width: 75ch; line-height: 1.55; margin: 2px 0 0; }
.stamp { color: var(--muted); font-family: var(--mono); font-size: 12px; }
.settings { display: flex; flex-wrap: wrap; gap: 6px; }
.settings span { font-family: var(--mono); font-size: 11.5px; padding: 3px 8px; border: 1px solid var(--line);
  border-radius: 4px; color: var(--muted); background: var(--panel); }
.scen { display: flex; flex-wrap: wrap; gap: 0; border: 1px solid var(--line); border-radius: 8px;
  background: var(--panel); padding: 4px; width: fit-content; max-width: 100%; }
.scen button { font: inherit; font-weight: 600; font-size: 14px; padding: 8px 16px; border: 0; border-radius: 6px;
  background: transparent; color: var(--muted); cursor: pointer; }
.scen button[aria-selected="true"] { background: var(--accent-soft); color: var(--fg); }
.scen button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.runs { display: grid; grid-template-columns: repeat(auto-fit, minmax(205px, 1fr)); gap: 12px; }
.run { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 14px 16px; min-width: 0;
  display: grid; gap: 8px; align-content: start; }
.run .step { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }
.run h3 { margin: 0; font-family: var(--display); font-size: 16px; }
.run ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
.run li { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; border-top: 1px solid var(--line); padding-top: 6px; }
.run li b { font-family: var(--mono); font-weight: 600; }
.run li span:last-child { color: var(--muted); text-align: right; font-family: var(--mono); font-size: 12px; white-space: nowrap; }
.run .none { color: var(--muted); font-size: 13px; }
.filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.filters .lbl { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-right: 4px; }
.chip { font: inherit; font-size: 13px; padding: 5px 11px; border-radius: 999px; border: 1px solid var(--line);
  background: var(--panel); color: var(--fg); cursor: pointer; }
.chip[aria-pressed="true"] { background: var(--accent-soft); border-color: var(--accent); font-weight: 600; }
.chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ledger { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; }
table { border-collapse: collapse; width: 100%; }
#hides { min-width: 900px; } #queue { min-width: 940px; }
th, td { padding: 9px 10px; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; background: var(--panel); }
th .key { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 5px; vertical-align: 0; }
td { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 13px; }
td.tier { font-family: var(--display); font-weight: 800; font-size: 16px; }
td.tier small { display: block; font-family: var(--mono); font-weight: 400; font-size: 11px; color: var(--muted); }
td.opt { color: var(--muted); }
td.opt.win { color: var(--fg); font-weight: 600; background: var(--good-bg); }
td.opt .where { display: block; font-family: var(--body); font-size: 11px; color: var(--muted); font-weight: 400; }
td.left { text-align: left; font-family: var(--body); font-size: 13.5px; }
#hides td.left { white-space: normal; min-width: 210px; }
.verdict { display: inline-flex; align-items: center; gap: 6px; font-family: var(--body); font-weight: 600; font-size: 13px; }
.dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
.sell { background: var(--sell); } .refine { background: var(--refine); } .craft { background: var(--craft); } .craft_focus { background: var(--focus); }
.uplift { font-size: 12px; color: var(--good); font-weight: 600; }
.neg { color: var(--bad); } .pos { color: var(--good); font-weight: 600; }
.empty { padding: 32px 16px; text-align: center; color: var(--muted); }
footer { color: var(--muted); font-size: 12.5px; line-height: 1.6; max-width: 80ch; display: grid; gap: 6px; }
@media (max-width: 560px) { h1 { font-size: 23px; } }
</style>

<div class="wrap">
  <header>
    <h1>Leather <span>Session Plan</span></h1>
    <div class="stamp" id="stamp"></div>
  </header>
  <div class="scen" id="scen" role="tablist" aria-label="Session plan"></div>
  <p class="lede" id="blurb"></p>
  <div class="settings" id="settings"></div>

  <section>
    <h2>Run sheet</h2>
    <p class="lede">The legs of your loop, in order, and what to carry on each one. Built from the plan below.</p>
  </section>
  <div class="runs" id="runs"></div>

  <section>
    <h2>Your hides: what to do with them</h2>
    <p class="lede">Silver each gathered hide turns into, by path. The highlighted cell is the plan.
      Focus is only recommended when it beats every no-focus option by 10% or more.</p>
  </section>
  <div class="filters" id="filters"></div>
  <div class="ledger"><table id="hides">
    <thead><tr>
      <th>Hide</th>
      <th><span class="key sell"></span><span data-label="home">Sell raw</span></th>
      <th><span class="key refine"></span><span data-label="refine">Refine, sell leather</span></th>
      <th><span class="key craft"></span><span data-label="craft">Craft jacket</span></th>
      <th><span class="key craft_focus"></span>…with focus</th>
      <th style="text-align:left">Plan</th>
    </tr></thead>
    <tbody id="hideRows"></tbody>
  </table></div>

  <section>
    <h2>Jacket craft queue</h2>
    <p class="lede">Every jacket with a live price, ranked by how much more it earns than selling the leather it uses
      (with focus). "Max/day" is the share of daily sales you can add without crashing the price.</p>
  </section>
  <div class="ledger"><table id="queue">
    <thead><tr>
      <th>Jacket</th><th style="text-align:left">Sell at</th><th>Net sale</th><th>Leather used</th>
      <th>vs leather</th><th>vs leather (focus)</th><th>Sold/day</th><th>Max/day</th>
    </tr></thead>
    <tbody id="queueRows"></tbody>
  </table><div class="empty" id="queueEmpty" hidden></div></div>

  <footer>
    <div>Prices are what you'd actually get: sell orders are valued at the lower of the current listing and the
      7-day average sale price, after tax and setup fee; Black Market sales go into its buy orders. Where a market
      is trading but has no current price snapshot, the 7-day average sale price is used and marked "7-day avg". Lower-tier leather
      for refining is priced at the cheapest sell order. Station fees and item quality above Normal are not counted,
      so crafted jackets that roll better quality are worth a bit more than shown.</div>
    <div>Return rates are settings: refining in Martlock (hide bonus) and crafting jackets in Thetford
      (leather armor bonus). Change them to match your focus and any daily city bonuses. Prices come from players running the
      Albion Data Client, so check them in-game before a big session.</div>
  </footer>
</div>

<script id="plan" type="application/json">__PLAN_JSON__</script>
<script>
(function () {
  var data = JSON.parse(document.getElementById("plan").textContent);
  var nf = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  var PATH = { sell: "Sell hides", refine: "Sell leather", craft: "Craft", craft_focus: "Craft with focus" };
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function silver(n) {
    var a = Math.abs(n);
    if (a >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (a >= 1e4) return (n / 1e3).toFixed(1) + "k";
    return nf.format(n);
  }

  document.getElementById("stamp").textContent = data.server + " · priced " +
    new Date(data.generated).toISOString().slice(0, 16).replace("T", " ") + " UTC";
  var scen = data.scenarios[0];
  var scenBar = document.getElementById("scen");
  data.scenarios.forEach(function (sc) {
    var b = el("button", null, sc.title);
    b.type = "button";
    b.setAttribute("role", "tab");
    b.addEventListener("click", function () { scen = sc; showScenario(); });
    sc.button = b;
    scenBar.appendChild(b);
  });
  scenBar.hidden = data.scenarios.length < 2;
  function showScenario() {
    data.scenarios.forEach(function (sc) { sc.button.setAttribute("aria-selected", String(sc === scen)); });
    document.getElementById("blurb").textContent = scen.blurb;
    var s = scen.settings, box = document.getElementById("settings");
    box.textContent = "";
    document.querySelector('[data-label="home"]').textContent = "Sell raw in " + s.home;
    document.querySelector('[data-label="refine"]').textContent = "Refine in " + s.refine_city + ", sell leather";
    document.querySelector('[data-label="craft"]').textContent = "Craft in " + s.craft_city;
    ["tax " + s.tax + "%", "refine return " + Math.round(s.refine_rrr * 1000) / 10 + "%",
     "craft return " + Math.round(s.craft_rrr * 1000) / 10 + "%",
     "with focus " + Math.round(s.craft_rrr_focus * 1000) / 10 + "%",
     s.sell_mode === "instant" ? "instant sales only" : "list or instant",
     "prices ≤ " + s.max_age + "h old"]
      .forEach(function (t) { box.appendChild(el("span", null, t)); });
    render();
  }

  var tiers = Array.from(new Set(data.scenarios[0].plans.map(function (p) { return p.tier; })));
  var state = { tier: "all" };
  var filters = document.getElementById("filters");
  filters.appendChild(el("span", "lbl", "Tier"));
  ["all"].concat(tiers).forEach(function (t) {
    var b = el("button", "chip", t === "all" ? "All" : "T" + t);
    b.type = "button"; b.dataset.val = t;
    b.setAttribute("aria-pressed", String(state.tier === t));
    b.addEventListener("click", function () {
      state.tier = t;
      filters.querySelectorAll(".chip").forEach(function (c) {
        c.setAttribute("aria-pressed", String(c.dataset.val === String(t)));
      });
      render();
    });
    filters.appendChild(b);
  });

  function bestOf(p, kind) {
    var best = null;
    p.paths.forEach(function (x) { if (x.path === kind && (!best || x.per_hide > best.per_hide)) best = x; });
    return best;
  }
  function optCell(x, isPick, showWhat) {
    var td = el("td", "opt" + (isPick ? " win" : ""));
    if (!x) { td.textContent = "–"; return td; }
    td.appendChild(document.createTextNode(nf.format(x.per_hide)));
    var where = (showWhat ? x.what.replace(/ \d\.\d$/, "") + " · " : "") + x.city +
      (x.how === "avg" ? " · 7-day avg" : "");
    td.appendChild(el("span", "where", where));
    return td;
  }

  function verdict(pick) {
    var s = scen.settings, jacket = pick.what.replace(/ \d\.\d$/, "");
    if (pick.path === "sell") return "Sell raw in " + pick.city;
    if (pick.path === "refine") return "Refine in " + s.refine_city + ", sell leather in " + pick.city;
    return "Refine in " + s.refine_city + ", craft " + jacket + " in " + s.craft_city +
      (pick.path === "craft_focus" ? " with focus" : "") + ", sell in " + pick.city;
  }
  function li(list, left, right) {
    var item = el("li");
    var a = el("span"); a.appendChild(el("b", null, left[0])); a.appendChild(document.createTextNode(" " + left[1]));
    item.appendChild(a);
    item.appendChild(el("span", null, right));
    list.appendChild(item);
  }
  function renderRuns(plans) {
    var s = scen.settings, box = document.getElementById("runs");
    box.textContent = "";
    var picks = plans.filter(function (p) { return p.pick; });
    var legs = [
      { step: "At " + s.home, title: "Sell raw hides", rows: picks.filter(function (p) { return p.pick.path === "sell"; })
          .map(function (p) { return [[p.tier + "." + p.ench, "hides"], nf.format(p.pick.per_hide) + "/hide"]; }) },
      { step: s.home + " → " + s.refine_city, title: "Haul hides, refine", rows: picks.filter(function (p) { return p.pick.path !== "sell"; })
          .map(function (p) { return [[p.tier + "." + p.ench, "hides"], p.pick.path === "refine" ? "sell leather" : "keep leather"]; }) },
      { step: "Leather sales", title: "Sell the leather", rows: picks.filter(function (p) { return p.pick.path === "refine"; })
          .map(function (p) { return [[p.tier + "." + p.ench, "leather → " + p.pick.city], nf.format(p.pick.per_hide) + "/hide"]; }) },
      { step: s.refine_city + " → " + s.craft_city, title: "Craft jackets", rows: picks.filter(function (p) { return p.pick.path.indexOf("craft") === 0; })
          .map(function (p) { return [[p.tier + "." + p.ench, p.pick.what.replace(/ \d\.\d$/, "") + (p.pick.path === "craft_focus" ? " (focus)" : "")],
                                      nf.format(p.pick.per_hide) + "/hide"]; }) },
      { step: "Jacket sales", title: "Sell the jackets", rows: picks.filter(function (p) { return p.pick.path.indexOf("craft") === 0; })
          .map(function (p) { return [[p.tier + "." + p.ench, p.pick.city === "Black Market" ? "Black Market (haul to Caerleon)" : p.pick.city],
                                      p.pick.how === "instant" ? "instant" : p.pick.how === "avg" ? "7-day avg" : "list"]; }) }
    ];
    legs.forEach(function (leg) {
      var c = el("div", "run");
      c.appendChild(el("div", "step", leg.step));
      c.appendChild(el("h3", null, leg.title));
      if (leg.rows.length) {
        var ul = el("ul");
        leg.rows.forEach(function (r) { li(ul, r[0], r[1]); });
        c.appendChild(ul);
      } else c.appendChild(el("div", "none", "Nothing this time."));
      box.appendChild(c);
    });
  }
  function render() {
    renderRuns(scen.plans.filter(function (p) { return state.tier === "all" || p.tier === state.tier; }));
    var body = document.getElementById("hideRows");
    body.textContent = "";
    scen.plans.filter(function (p) { return state.tier === "all" || p.tier === state.tier; })
      .forEach(function (p) {
        var tr = el("tr");
        var td = el("td", "tier", p.tier + "." + p.ench);
        td.appendChild(el("small", null, p.hide));
        tr.appendChild(td);
        var pick = p.pick;
        ["sell", "refine", "craft", "craft_focus"].forEach(function (k) {
          var x = bestOf(p, k);
          tr.appendChild(optCell(x, pick && x && pick.path === k && pick.what === x.what, k.indexOf("craft") === 0));
        });
        var v = el("td", "left");
        if (pick) {
          var span = el("span", "verdict");
          span.appendChild(el("span", "dot " + pick.path));
          span.appendChild(document.createTextNode(verdict(pick)));
          v.appendChild(span);
          if (p.raw && pick.path !== "sell") {
            var up = (pick.per_hide / p.raw - 1) * 100;
            if (up >= 1) v.appendChild(el("div", "uplift", "+" + up.toFixed(0) + "% vs selling raw"));
          }
        } else {
          v.textContent = "No recent prices";
          v.style.color = "var(--muted)";
        }
        tr.appendChild(v);
        body.appendChild(tr);
      });

    var q = [];
    scen.plans.forEach(function (p) {
      if (state.tier !== "all" && p.tier !== state.tier) return;
      p.jackets.forEach(function (j) { q.push(j); });
    });
    q.sort(function (a, b) {
      var x = a.gain_vs_leather_focus, y = b.gain_vs_leather_focus;
      return (y == null ? -Infinity : y) - (x == null ? -Infinity : x);
    });
    var qb = document.getElementById("queueRows");
    qb.textContent = "";
    q.forEach(function (j) {
      var tr = el("tr");
      tr.appendChild(el("td", "left", j.name));
      tr.appendChild(el("td", "left", j.city + (j.how === "listed" ? " (list)" : j.how === "avg" ? " (7-day avg)" : "")));
      tr.appendChild(el("td", null, silver(j.sell)));
      tr.appendChild(el("td", null, j.leather_per_focus.toFixed(1) + " – " + j.leather_per.toFixed(1)));
      [j.gain_vs_leather, j.gain_vs_leather_focus].forEach(function (g) {
        tr.appendChild(el("td", g == null ? "" : g >= 0 ? "pos" : "neg",
          g == null ? "–" : (g >= 0 ? "+" : "") + silver(g)));
      });
      tr.appendChild(el("td", null, j.volume == null ? "–" : nf.format(j.volume)));
      tr.appendChild(el("td", null, j.suggest == null ? "–" : nf.format(j.suggest)));
      qb.appendChild(tr);
    });
    var qe = document.getElementById("queueEmpty");
    qe.hidden = q.length > 0;
    qe.textContent = "No jackets with recent prices for this tier.";
  }
  showScenario();
})();
</script>
"""


if __name__ == "__main__":
    main()
