#!/usr/bin/env python3
"""
Albion Ledger
-------------
A balance sheet of your market buys and sells.

Reads trades from CSV, applies market fees, and reports:

  * silver in, silver out and the net
  * trading profit: what you sold minus what you paid for it (average cost)
  * income from selling things you never bought (gathered, looted, crafted)
  * what you bought and still hold, at cost and at today's market price

CSV columns: date, character, action, item_id, name, qty, price, how, city, fee, note
  * action: buy, sell, or use (consumed in crafting, e.g. energy into stews)
  * price: per unit, before fees
  * how: "order" (you placed an order) or "instant" (you took one); default instant
  * fee: total fee in silver; leave blank to have it worked out

Examples
  python albion_ledger.py ledger/trades.csv
  python albion_ledger.py ledger/trades.csv --html ledger.html --no-premium
"""
import argparse
import csv
import json
import sys
import time
import urllib.parse
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timezone

import albion_flip_scanner as afs

MARKETS = afs.ROYAL + [afs.CAERLEON, afs.BLACK_MARKET, "Brecilien"]
SETUP_FEE = afs.SETUP_FEE
MIN_SALES = 3   # sales in 30 days needed before a market's price is trusted


# --------------------------------------------------------------------------
# Trades
# --------------------------------------------------------------------------
def read_trades(paths):
    trades = []
    for path in paths:
        with open(path, newline="", encoding="utf-8") as f:
            for i, r in enumerate(csv.DictReader(f)):
                action = (r.get("action") or "").strip().lower()
                if action not in ("buy", "sell", "use"):
                    continue
                item = (r.get("item_id") or "").strip()
                name = (r.get("name") or "").strip() or item
                trades.append({
                    "seq": len(trades),
                    "date": (r.get("date") or "").strip(),
                    "character": (r.get("character") or "").strip() or "?",
                    "action": action,
                    "item_id": item,
                    "key": item or name,
                    "name": name,
                    "qty": float(r.get("qty") or 1),
                    "price": float(r.get("price") or 0),
                    "how": (r.get("how") or "instant").strip().lower(),
                    "city": (r.get("city") or "").strip(),
                    "fee": float(r["fee"]) if (r.get("fee") or "").strip() else None,
                    "note": (r.get("note") or "").strip(),
                })
    trades.sort(key=lambda t: (t["date"], t["seq"]))
    return trades


def trade_fee(t, tax):
    """Market fees for one trade: sales tax on sells, setup fee on orders."""
    if t["fee"] is not None:
        return t["fee"]
    gross = t["qty"] * t["price"]
    if t["action"] == "sell":
        return gross * (tax + (SETUP_FEE if t["how"] == "order" else 0))
    if t["action"] == "buy" and t["how"] == "order":
        return gross * SETUP_FEE
    return 0.0


def build_books(trades, tax):
    """
    Walk the trades in date order with average-cost accounting.
    Returns (totals, items) where items is {key: per-item summary}.
    """
    items = OrderedDict()
    totals = defaultdict(float)
    for t in trades:
        it = items.setdefault(t["key"], {
            "key": t["key"], "item_id": t["item_id"], "name": t["name"],
            "bought": 0.0, "spent": 0.0, "sold": 0.0, "received": 0.0,
            "used": 0.0, "used_cost": 0.0, "held": 0.0, "held_cost": 0.0,
            "profit": 0.0, "uncosted_qty": 0.0, "uncosted_income": 0.0,
        })
        gross = t["qty"] * t["price"]
        fee = trade_fee(t, tax)
        t["fee_calc"] = fee
        totals["fees"] += fee
        if t["action"] == "buy":
            cost = gross + fee
            t["cash"] = -cost
            it["bought"] += t["qty"]
            it["spent"] += cost
            it["held"] += t["qty"]
            it["held_cost"] += cost
            totals["spent"] += cost
        elif t["action"] == "sell":
            net = gross - fee
            t["cash"] = net
            it["sold"] += t["qty"]
            it["received"] += net
            totals["received"] += net
            matched = min(t["qty"], it["held"])
            if matched > 0:
                avg = it["held_cost"] / it["held"]
                net_each = net / t["qty"]
                it["profit"] += matched * (net_each - avg)
                totals["profit"] += matched * (net_each - avg)
                it["held"] -= matched
                it["held_cost"] -= matched * avg
            extra = t["qty"] - matched
            if extra > 0:
                income = extra * net / t["qty"]
                it["uncosted_qty"] += extra
                it["uncosted_income"] += income
                totals["uncosted"] += income
        else:  # use: consumed in crafting, cost moves out of holdings
            t["cash"] = 0.0
            used = min(t["qty"], it["held"])
            if used > 0:
                avg = it["held_cost"] / it["held"]
                it["used"] += used
                it["used_cost"] += used * avg
                totals["used_cost"] += used * avg
                it["held"] -= used
                it["held_cost"] -= used * avg
    for k in ("spent", "received", "profit", "uncosted", "used_cost", "fees"):
        totals[k] += 0.0
    totals["net"] = totals["received"] - totals["spent"]
    totals["held_cost"] = sum(i["held_cost"] for i in items.values())
    return dict(totals), items


def weekly(trades):
    """{iso week start: {"in": silver, "out": silver}}"""
    out = OrderedDict()
    for t in trades:
        try:
            d = date.fromisoformat(t["date"])
        except ValueError:
            continue
        monday = date.fromordinal(d.toordinal() - d.weekday()).isoformat()
        w = out.setdefault(monday, {"in": 0.0, "out": 0.0})
        if t["cash"] > 0:
            w["in"] += t["cash"]
        else:
            w["out"] -= t["cash"]
    return out


# --------------------------------------------------------------------------
# Market value of what you still hold
# --------------------------------------------------------------------------
def market_values(host, item_ids, days=30, pause=0.4):
    """{item_id: typical sale price} (volume-weighted over `days`)."""
    loc_q = urllib.parse.quote(",".join(MARKETS), safe=",")
    suffix = f".json?locations={loc_q}&time-scale=24"
    base_len = len(host) + len("/api/v2/stats/history/") + len(suffix)
    agg = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    for chunk in afs.chunk_items(sorted(item_ids), base_len):
        url = f"{host}/api/v2/stats/history/{','.join(chunk)}{suffix}"
        try:
            rows = afs.get_json(url)
        except Exception as e:
            print(f"  history fetch failed: {e}", file=sys.stderr)
            continue
        for r in rows:
            for p in (r.get("data") or [])[-days:]:
                a = agg[r["item_id"]][r["location"]]
                a[0] += p.get("item_count", 0)
                a[1] += p.get("item_count", 0) * p.get("avg_price", 0)
        time.sleep(pause)
    out = {}
    for item, locs in agg.items():
        pool = {l: v for l, v in locs.items() if v[0] >= MIN_SALES} or locs
        n = sum(v[0] for v in pool.values())
        if n:
            out[item] = sum(v[1] for v in pool.values()) / n
    return out


def value_holdings(items, values):
    total = 0.0
    for it in items.values():
        each = values.get(it["item_id"]) if it["held"] > 0 else None
        it["market_each"] = each
        it["market_value"] = each * it["held"] if each is not None else None
        if it["market_value"] is not None:
            total += it["market_value"]
    return total


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def print_books(totals, items):
    f = lambda v: f"{v:,.0f}"
    print(f"Silver in   {f(totals.get('received', 0)):>15}")
    print(f"Silver out  {f(totals.get('spent', 0)):>15}")
    print(f"Net         {f(totals.get('net', 0)):>15}")
    print(f"  trading profit (sold minus cost)     {f(totals.get('profit', 0)):>13}")
    print(f"  sales of things you didn't buy       {f(totals.get('uncosted', 0)):>13}")
    print(f"  used in crafting (at cost)           {f(totals.get('used_cost', 0)):>13}")
    print(f"  still held (at cost)                 {f(totals.get('held_cost', 0)):>13}")
    if totals.get("held_value") is not None:
        print(f"  still held (at market)               {f(totals['held_value']):>13}")
    print(f"  fees paid                            {f(totals.get('fees', 0)):>13}")
    print("\nBy item")
    for it in sorted(items.values(), key=lambda i: -(i["profit"] + i["uncosted_income"])):
        print(f"  {it['name'][:32]:32} bought {it['bought']:>6,.0f} sold {it['sold']:>6,.0f}  "
              f"profit {it['profit']:>12,.0f}  other sales {it['uncosted_income']:>12,.0f}  "
              f"held {it['held']:>5,.0f}")


def write_html(path, trades, totals, items, weeks, meta):
    data = dict(meta, trades=trades, totals=totals, items=list(items.values()),
                weeks=[{"week": k, **v} for k, v in weeks.items()])
    blob = json.dumps(data).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE.replace("__LEDGER_JSON__", blob))


def build_parser():
    p = argparse.ArgumentParser(description="Albion buy/sell ledger")
    p.add_argument("files", nargs="+", help="trades CSV file(s)")
    p.add_argument("--server", choices=afs.HOSTS, default="americas")
    p.add_argument("--no-premium", action="store_true",
                   help="8%% sales tax instead of the 4%% premium rate")
    p.add_argument("--no-market", action="store_true",
                   help="don't look up market prices for what you still hold")
    p.add_argument("--html", help="also write a ledger page to this file")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    tax = afs.TAX_NO_PREMIUM if args.no_premium else afs.TAX_PREMIUM
    trades = read_trades(args.files)
    totals, items = build_books(trades, tax)
    totals["held_value"] = None
    held = {i["item_id"] for i in items.values() if i["held"] > 0 and i["item_id"]}
    if held and not args.no_market:
        print(f"Pricing {len(held)} items you still hold...", file=sys.stderr)
        totals["held_value"] = value_holdings(items, market_values(afs.HOSTS[args.server], held))
    else:
        value_holdings(items, {})
    print_books(totals, items)
    if args.html:
        now = datetime.now(timezone.utc)
        write_html(args.html, trades, totals, items, weekly(trades),
                   {"generated": now.isoformat(), "server": args.server,
                    "tax": tax, "setup_fee": SETUP_FEE})
        print(f"\nWrote ledger page to {args.html}")
    return totals, items


HTML_TEMPLATE = r"""<title>Silver Ledger</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
/* Layout: one column — header, balance tiles, weekly in/out chart, profit by item, trade log. */
:root {
  --bg: #f3f4f7; --panel: #ffffff; --fg: #1b2230; --muted: #5d6779; --line: #dde1e8;
  --accent: #b07a12; --accent-soft: #f6ead0;
  --in: #1f7a4d; --in-bg: #dff1e7; --out: #b3261e; --out-bg: #fbe3e1; --use: #6b7280; --use-bg: #eceef2;
  --display: "Archivo", system-ui, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #12161d; --panel: #1a2029; --fg: #e6e9ef; --muted: #98a2b3; --line: #2b3340;
  --accent: #e0aa45; --accent-soft: #3a2f19;
  --in: #5fc796; --in-bg: #183a2a; --out: #f08a80; --out-bg: #3d2224; --use: #a0a7b4; --use-bg: #262c36; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #12161d; --panel: #1a2029; --fg: #e6e9ef; --muted: #98a2b3; --line: #2b3340;
  --accent: #e0aa45; --accent-soft: #3a2f19;
  --in: #5fc796; --in-bg: #183a2a; --out: #f08a80; --out-bg: #3d2224; --use: #a0a7b4; --use-bg: #262c36; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font-family: var(--display); font-size: 14px; }
.wrap { max-width: 1100px; margin: 0 auto; padding-inline: 16px; padding-block: 24px 48px; display: grid; gap: 20px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 24px; }
h1 { font-weight: 800; font-size: 28px; margin: 0; letter-spacing: -0.01em; }
h1 span { color: var(--accent); }
h2 { font-size: 17px; margin: 6px 0 0; }
.stamp { color: var(--muted); font-family: var(--mono); font-size: 12px; }
.banner { background: var(--accent-soft); border: 1px solid var(--accent); border-radius: 6px; padding: 10px 14px; font-size: 13.5px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 14px 16px; min-width: 0; }
.tile .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }
.tile .value { font-family: var(--mono); font-size: 26px; font-weight: 600; margin-top: 4px; font-variant-numeric: tabular-nums; }
.tile .sub { color: var(--muted); font-size: 12.5px; margin-top: 4px; line-height: 1.5; }
.pos { color: var(--in); } .neg { color: var(--out); }
.chart { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 14px 16px; overflow-x: auto; }
.chart svg { display: block; }
.legend { display: flex; gap: 14px; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; }
.filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.filters .lbl { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin: 0 4px 0 8px; }
.filters .lbl:first-child { margin-left: 0; }
.chip { font: inherit; font-size: 13px; padding: 5px 11px; border-radius: 999px; border: 1px solid var(--line);
  background: var(--panel); color: var(--fg); cursor: pointer; }
.chip[aria-pressed="true"] { background: var(--accent-soft); border-color: var(--accent); font-weight: 600; }
.chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tbl { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; }
table { border-collapse: collapse; width: 100%; min-width: 760px; }
th, td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; }
th.l, td.l { text-align: left; }
th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
td { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 13px; }
td.name { font-family: var(--display); font-size: 14px; white-space: normal; min-width: 180px; }
td.name small { display: block; font-size: 12px; color: var(--muted); font-style: italic; }
.pill { display: inline-block; font-family: var(--display); font-size: 11px; font-weight: 600; letter-spacing: 0.04em; padding: 2px 8px; border-radius: 3px; }
.pill.sell { background: var(--in-bg); color: var(--in); } .pill.buy { background: var(--out-bg); color: var(--out); }
.pill.use { background: var(--use-bg); color: var(--use); }
.empty { padding: 28px 16px; text-align: center; color: var(--muted); }
footer { color: var(--muted); font-size: 12.5px; line-height: 1.6; max-width: 80ch; }
@media (max-width: 560px) { h1 { font-size: 23px; } .tile .value { font-size: 22px; } }
</style>

<div class="wrap">
  <header>
    <h1>Silver <span>Ledger</span></h1>
    <div class="stamp" id="stamp"></div>
  </header>
  <div class="banner" id="banner" hidden></div>
  <section class="tiles" id="tiles"></section>

  <h2>Silver in and out by week</h2>
  <div class="chart" id="chart"></div>

  <h2>By item</h2>
  <div class="tbl"><table>
    <thead><tr><th class="l">Item</th><th>Bought</th><th>Paid</th><th>Sold</th><th>Received</th><th>Trading profit</th><th>Other sales</th><th>Still held</th></tr></thead>
    <tbody id="items"></tbody>
  </table></div>

  <h2>Trades</h2>
  <div class="filters" id="filters"></div>
  <div class="tbl"><table>
    <thead><tr><th class="l">Date</th><th class="l">Character</th><th class="l">Action</th><th class="l">Item</th><th>Qty</th><th>Price</th><th>Fees</th><th>Silver</th><th class="l">Where</th></tr></thead>
    <tbody id="trades"></tbody>
  </table><div class="empty" id="empty" hidden>No trades match these filters.</div></div>
  <footer id="footer"></footer>
</div>

<script id="ledger" type="application/json">__LEDGER_JSON__</script>
<script>
(function () {
  var d = JSON.parse(document.getElementById("ledger").textContent);
  var T = d.totals, nf = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  function el(t, c, x) { var e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; }
  function silver(n) {
    if (n == null) return "–";
    var a = Math.abs(n), s = n < 0 ? "−" : "";
    if (a >= 1e9) return s + (a / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return s + (a / 1e6).toFixed(2) + "M";
    if (a >= 1e4) return s + Math.round(a / 1e3) + "k";
    return s + nf.format(a);
  }
  function signed(n) { return (n > 0 ? "+" : "") + silver(n); }

  document.getElementById("stamp").textContent = d.server + " · updated " +
    new Date(d.generated).toISOString().slice(0, 16).replace("T", " ") + " UTC";
  if (d.trades.some(function (t) { return /EXAMPLE/.test(t.note); })) {
    var b = document.getElementById("banner"); b.hidden = false;
    b.textContent = "Rows marked EXAMPLE are sample trades showing the format. They'll be replaced with your real buys and sells.";
  }

  // Tiles
  var tiles = document.getElementById("tiles");
  function tile(label, value, cls, sub) {
    var t = el("div", "tile"); t.appendChild(el("div", "label", label));
    t.appendChild(el("div", "value " + (cls || ""), value));
    if (sub) t.appendChild(el("div", "sub", sub)); tiles.appendChild(t);
  }
  var held = T.held_value != null ? T.held_value : T.held_cost;
  var worth = (T.net || 0) + (held || 0);
  tile("Net, counting what you still hold", signed(worth), worth >= 0 ? "pos" : "neg",
       "Silver in " + silver(T.received || 0) + ", out " + silver(T.spent || 0) + ", plus " + silver(held || 0) + " still held");
  tile("Trading profit", signed(T.profit || 0), (T.profit || 0) >= 0 ? "pos" : "neg", "Sold minus what you paid for it, after fees");
  tile("Sales of things you didn't buy", silver(T.uncosted || 0), "", "Gathered, looted or crafted");
  tile("Still held from purchases", silver(held || 0), "",
       "Cost " + silver(T.held_cost || 0) + (T.held_value != null ? " · market " + silver(T.held_value) : "") +
       (T.used_cost ? " · " + silver(T.used_cost) + " used in crafting" : ""));
  tile("Fees paid", silver(T.fees || 0), "", "Tax " + (d.tax * 100) + "% on sales, " + (d.setup_fee * 100) + "% to place orders");

  // Weekly chart (SVG, one scale)
  var weeks = d.weeks, box = document.getElementById("chart");
  if (!weeks.length) { box.textContent = "No dated trades yet."; } else {
    var lg = el("div", "legend");
    [["var(--in)", "Silver in"], ["var(--out)", "Silver out"]].forEach(function (p) {
      var s = el("span"); var i = el("i"); i.style.background = p[0]; s.appendChild(i); s.appendChild(document.createTextNode(p[1])); lg.appendChild(s);
    });
    box.appendChild(lg);
    var max = Math.max.apply(null, weeks.map(function (w) { return Math.max(w.in, w.out); }).concat([1]));
    var bw = 18, gap = 6, group = bw * 2 + gap + 28, h = 170, top = 18, bottom = 34, left = 56;
    var width = Math.max(left + weeks.length * group + 10, 320);
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("width", width); svg.setAttribute("height", h + top + bottom);
    svg.setAttribute("viewBox", "0 0 " + width + " " + (h + top + bottom));
    function txt(x, y, s, anchor) {
      var t = document.createElementNS(ns, "text"); t.setAttribute("x", x); t.setAttribute("y", y);
      t.setAttribute("fill", "var(--muted)"); t.setAttribute("font-size", "11"); t.setAttribute("font-family", "JetBrains Mono, monospace");
      t.setAttribute("text-anchor", anchor || "start"); t.textContent = s; svg.appendChild(t);
    }
    [0, 0.5, 1].forEach(function (f) {
      var y = top + h - f * h;
      var ln = document.createElementNS(ns, "line"); ln.setAttribute("x1", left); ln.setAttribute("x2", width - 4);
      ln.setAttribute("y1", y); ln.setAttribute("y2", y); ln.setAttribute("stroke", "var(--line)"); svg.appendChild(ln);
      txt(left - 6, y + 4, silver(max * f), "end");
    });
    weeks.forEach(function (w, i) {
      var x = left + 10 + i * group;
      [["in", "var(--in)", 0], ["out", "var(--out)", bw + gap]].forEach(function (p) {
        var v = w[p[0]], bh = v / max * h;
        var r = document.createElementNS(ns, "rect");
        r.setAttribute("x", x + p[2]); r.setAttribute("y", top + h - bh); r.setAttribute("width", bw);
        r.setAttribute("height", Math.max(bh, 0)); r.setAttribute("fill", p[1]); r.setAttribute("rx", 2);
        var tt = document.createElementNS(ns, "title"); tt.textContent = (p[0] === "in" ? "In " : "Out ") + nf.format(v); r.appendChild(tt);
        svg.appendChild(r);
      });
      txt(x + bw + gap / 2, top + h + 18, w.week.slice(5), "middle");
    });
    box.appendChild(svg);
  }

  // Items
  var ib = document.getElementById("items");
  d.items.slice().sort(function (a, b) { return (b.profit + b.uncosted_income) - (a.profit + a.uncosted_income); }).forEach(function (it) {
    var tr = el("tr");
    tr.appendChild(el("td", "name l", it.name));
    tr.appendChild(el("td", null, it.bought ? nf.format(it.bought) : "–"));
    tr.appendChild(el("td", null, it.spent ? silver(it.spent) : "–"));
    tr.appendChild(el("td", null, it.sold ? nf.format(it.sold) : "–"));
    tr.appendChild(el("td", null, it.received ? silver(it.received) : "–"));
    tr.appendChild(el("td", it.profit > 0 ? "pos" : it.profit < 0 ? "neg" : "", it.bought && it.sold ? signed(it.profit) : "–"));
    tr.appendChild(el("td", null, it.uncosted_income ? silver(it.uncosted_income) : "–"));
    var h = el("td", null, it.held > 0 ? nf.format(it.held) : "–");
    if (it.held > 0) h.appendChild(el("small", null, " " + (it.market_value != null ? silver(it.market_value) + " mkt" : silver(it.held_cost) + " cost")));
    tr.appendChild(h);
    ib.appendChild(tr);
  });

  // Trade log
  var state = { ch: "all", act: "all" };
  var filters = document.getElementById("filters");
  function chips(label, key, values) {
    if (values.length < 2 && key === "ch") return;
    filters.appendChild(el("span", "lbl", label));
    ["all"].concat(values).forEach(function (v) {
      var c = el("button", "chip", v === "all" ? "All" : v); c.type = "button"; c.dataset.key = key; c.dataset.val = v;
      c.setAttribute("aria-pressed", String(state[key] === v));
      c.addEventListener("click", function () {
        state[key] = v;
        filters.querySelectorAll('[data-key="' + key + '"]').forEach(function (x) { x.setAttribute("aria-pressed", String(x.dataset.val === v)); });
        renderTrades();
      });
      filters.appendChild(c);
    });
  }
  chips("Character", "ch", Array.from(new Set(d.trades.map(function (t) { return t.character; }))));
  chips("Action", "act", ["buy", "sell", "use"].filter(function (a) { return d.trades.some(function (t) { return t.action === a; }); }));
  function renderTrades() {
    var tb = document.getElementById("trades"); tb.textContent = "";
    var list = d.trades.filter(function (t) { return (state.ch === "all" || t.character === state.ch) && (state.act === "all" || t.action === state.act); })
      .slice().reverse();
    list.forEach(function (t) {
      var tr = el("tr");
      tr.appendChild(el("td", "l", t.date));
      tr.appendChild(el("td", "l", t.character));
      var a = el("td", "l"); a.appendChild(el("span", "pill " + t.action, t.action === "use" ? "used" : t.action)); tr.appendChild(a);
      var n = el("td", "name l", t.name); if (t.note) n.appendChild(el("small", null, t.note)); tr.appendChild(n);
      tr.appendChild(el("td", null, nf.format(t.qty)));
      tr.appendChild(el("td", null, t.action === "use" ? "–" : silver(t.price)));
      tr.appendChild(el("td", null, t.fee_calc ? silver(t.fee_calc) : "–"));
      tr.appendChild(el("td", t.cash > 0 ? "pos" : t.cash < 0 ? "neg" : "", t.cash ? signed(t.cash) : "–"));
      tr.appendChild(el("td", "l", (t.city || "–") + (t.how === "order" && t.action !== "use" ? " (order)" : "")));
      tb.appendChild(tr);
    });
    document.getElementById("empty").hidden = list.length > 0;
  }
  renderTrades();

  document.getElementById("footer").textContent = "Trading profit uses average cost: each sale is matched against what you paid on average for that item, after fees. " +
    "Sales of things you never bought count as other sales. Crafting inputs marked as used leave your holdings at cost. " +
    "Things you still hold are valued at the typical price of the last 30 days of sales.";
})();
</script>
"""


if __name__ == "__main__":
    main()
