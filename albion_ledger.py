#!/usr/bin/env python3
"""
Albion Ledger
-------------
Prices a list of holdings (one or more CSV files) against recent market sales
and writes a ledger: value per item, per location and per character, how
easily each item sells, and where it sells best.

CSV columns: character, location, item_id, name, qty, quality, manual_price, note
  * quality: 1-5 to price one quality only; blank prices all qualities
  * manual_price: used when the market has no sales (or for non-items such as
    SILVER); it is what one unit is worth to you

Examples
  python albion_ledger.py ledger/alouris.csv
  python albion_ledger.py ledger/*.csv --html ledger.html
"""
import argparse
import csv
import json
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone

import albion_flip_scanner as afs

MARKETS = afs.ROYAL + [afs.CAERLEON, afs.BLACK_MARKET, "Brecilien"]
DAYS = 30
MIN_SALES = 3        # sales in DAYS needed before a market's price is trusted
EASY_PER_DAY = 5     # sells at least this many per day: easy to sell
SLOW_PER_DAY = 0.3   # below this: rarely trades


def read_holdings(paths):
    rows = []
    for path in paths:
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if not (r.get("item_id") or "").strip():
                    continue
                rows.append({
                    "character": r.get("character", "").strip() or "?",
                    "location": r.get("location", "").strip() or "?",
                    "item_id": r["item_id"].strip(),
                    "name": (r.get("name") or "").strip() or r["item_id"].strip(),
                    "qty": int(float(r.get("qty") or 1)),
                    "quality": int(r["quality"]) if (r.get("quality") or "").strip() else None,
                    "manual": float(r["manual_price"]) if (r.get("manual_price") or "").strip() else None,
                    "note": (r.get("note") or "").strip(),
                })
    return rows


def fetch_sales(host, items, days=DAYS, pause=0.4):
    """{(item, location, quality): (count, avg_price)} over the last `days`."""
    loc_q = urllib.parse.quote(",".join(MARKETS), safe=",")
    suffix = f".json?locations={loc_q}&time-scale=24"
    base_len = len(host) + len("/api/v2/stats/history/") + len(suffix)
    out = {}
    for chunk in afs.chunk_items(sorted(items), base_len):
        url = f"{host}/api/v2/stats/history/{','.join(chunk)}{suffix}"
        try:
            data = afs.get_json(url)
        except Exception as e:
            print(f"  history fetch failed: {e}", file=sys.stderr)
            continue
        for r in data:
            pts = (r.get("data") or [])[-days:]
            n = sum(p.get("item_count", 0) for p in pts)
            if n:
                avg = sum(p["avg_price"] * p["item_count"] for p in pts) / n
                out[(r["item_id"], r["location"], int(r.get("quality", 1)))] = (n, avg)
        time.sleep(pause)
    return out


def price_row(row, sales, days=DAYS):
    """Adds each, total, per_day, best_market and status to a holding row."""
    by_loc = defaultdict(lambda: [0, 0.0])
    for (item, loc, q), (n, avg) in sales.items():
        if item == row["item_id"] and (row["quality"] is None or q == row["quality"]):
            by_loc[loc][0] += n
            by_loc[loc][1] += n * avg
    total_sales = sum(n for n, _ in by_loc.values())
    trusted = {loc: (n, v) for loc, (n, v) in by_loc.items() if n >= MIN_SALES}
    pool = trusted or by_loc
    best = best_price = None
    if pool:
        # Value at the typical (volume-weighted) price; the best market is
        # where it sells for the most, shown separately.
        n_all = sum(n for n, _ in pool.values())
        each = sum(v for _, v in pool.values()) / n_all
        best = max(pool, key=lambda l: pool[l][1] / pool[l][0])
        best_price = pool[best][1] / pool[best][0]
        source = "market" if trusted else "thin"
    else:
        each, source = None, "none"
    if row["manual"] is not None and source != "market":
        each, source = row["manual"], "manual"

    per_day = total_sales / days
    if row["item_id"] == "SILVER":
        status = "silver"
    elif source in ("none", "manual") and not total_sales:
        status = "unknown" if source == "none" else "your price"
    elif per_day >= EASY_PER_DAY:
        status = "easy"
    elif per_day >= SLOW_PER_DAY:
        status = "list"
    else:
        status = "slow"
    row.update(each=each, total=(each or 0) * row["qty"], per_day=per_day,
               best_market=best, best_price=best_price, source=source, status=status)
    return row


STATUS_TEXT = {
    "silver": "Silver", "easy": "Sells easily", "list": "List and wait",
    "slow": "Rarely trades", "your price": "Your price", "unknown": "No market data",
}


def print_ledger(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[(r["character"], r["location"])].append(r)
    grand = 0
    for (ch, loc), items in sorted(groups.items(), key=lambda kv: -sum(r["total"] for r in kv[1])):
        sub = sum(r["total"] for r in items)
        grand += sub
        print(f"\n{ch} / {loc}: {sub:,.0f}")
        for r in sorted(items, key=lambda r: -r["total"])[:12]:
            each = "-" if r["each"] is None else f"{r['each']:,.0f}"
            print(f"  {r['name'][:34]:34} x{r['qty']:<5} {each:>11}  {r['total']:>13,.0f}  "
                  f"{STATUS_TEXT[r['status']]:15} {r['best_market'] or ''}")
        if len(items) > 12:
            print(f"  ... {len(items) - 12} more")
    print(f"\nTotal: {grand:,.0f}")


def write_html(path, rows, meta):
    data = dict(meta, rows=rows)
    blob = json.dumps(data).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE.replace("__LEDGER_JSON__", blob))


def build_parser():
    p = argparse.ArgumentParser(description="Albion holdings ledger")
    p.add_argument("files", nargs="+", help="holdings CSV file(s)")
    p.add_argument("--server", choices=afs.HOSTS, default="americas")
    p.add_argument("--html", help="also write a ledger page to this file")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    rows = read_holdings(args.files)
    items = {r["item_id"] for r in rows if "_" in r["item_id"] and r["item_id"][0] == "T"}
    print(f"Pricing {len(items)} items from {len(rows)} holdings...", file=sys.stderr)
    sales = fetch_sales(afs.HOSTS[args.server], items)
    for r in rows:
        price_row(r, sales)
    print_ledger(rows)
    if args.html:
        now = datetime.now(timezone.utc)
        write_html(args.html, rows, {"generated": now.isoformat(), "server": args.server,
                                     "days": DAYS})
        print(f"\nWrote ledger page to {args.html}")
    return rows


HTML_TEMPLATE = r"""<title>Alouris Ledger</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
/* Layout: one column — header, value tiles, location bar chart, filters, ledger grouped by location. */
:root {
  --bg: #f3f4f7; --panel: #ffffff; --fg: #1b2230; --muted: #5d6779; --line: #dde1e8;
  --accent: #b07a12; --accent-soft: #f6ead0;
  --easy: #1f7a4d; --easy-bg: #dff1e7; --list: #1d4f91; --list-bg: #e3eefb;
  --slow: #a3620a; --slow-bg: #fbeedb; --unk: #6b7280; --unk-bg: #eceef2;
  --display: "Archivo", system-ui, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #12161d; --panel: #1a2029; --fg: #e6e9ef; --muted: #98a2b3; --line: #2b3340;
  --accent: #e0aa45; --accent-soft: #3a2f19;
  --easy: #5fc796; --easy-bg: #183a2a; --list: #8ab8f0; --list-bg: #1f2d42;
  --slow: #e8a64a; --slow-bg: #3a2c17; --unk: #a0a7b4; --unk-bg: #262c36; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #12161d; --panel: #1a2029; --fg: #e6e9ef; --muted: #98a2b3; --line: #2b3340;
  --accent: #e0aa45; --accent-soft: #3a2f19;
  --easy: #5fc796; --easy-bg: #183a2a; --list: #8ab8f0; --list-bg: #1f2d42;
  --slow: #e8a64a; --slow-bg: #3a2c17; --unk: #a0a7b4; --unk-bg: #262c36; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font-family: var(--display); font-size: 14px; }
.wrap { max-width: 1150px; margin: 0 auto; padding-inline: 16px; padding-block: 24px 48px; display: grid; gap: 20px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 24px; }
h1 { font-weight: 800; font-size: 28px; margin: 0; letter-spacing: -0.01em; text-wrap: balance; }
h1 span { color: var(--accent); }
.stamp { color: var(--muted); font-family: var(--mono); font-size: 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 14px 16px; min-width: 0; }
.tile .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }
.tile .value { font-family: var(--mono); font-size: 26px; font-weight: 600; margin-top: 4px; font-variant-numeric: tabular-nums; }
.tile .sub { color: var(--muted); font-size: 12.5px; margin-top: 2px; }
.tile.lead .value { color: var(--accent); }
.bars { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 14px 16px; display: grid; gap: 8px; }
.bar { display: grid; grid-template-columns: minmax(110px, 180px) 1fr auto; gap: 10px; align-items: center; font-size: 13px; }
.bar .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar .track { height: 10px; background: var(--bg); border-radius: 3px; overflow: hidden; display: flex; }
.bar .seg { height: 100%; }
.bar .amt { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 12.5px; min-width: 64px; text-align: right; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--muted); }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; vertical-align: 0; }
.filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.filters .lbl { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin: 0 4px 0 8px; }
.filters .lbl:first-child { margin-left: 0; }
.chip { font: inherit; font-size: 13px; padding: 5px 11px; border-radius: 999px; border: 1px solid var(--line);
  background: var(--panel); color: var(--fg); cursor: pointer; }
.chip[aria-pressed="true"] { background: var(--accent-soft); border-color: var(--accent); font-weight: 600; }
.chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ledger { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; }
table { border-collapse: collapse; width: 100%; min-width: 860px; }
th, td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; white-space: normal; }
th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
td { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 13px; }
td.item { font-family: var(--display); font-size: 14px; min-width: 220px; }
td.item .id { display: block; font-family: var(--mono); font-size: 11px; color: var(--muted); }
td.item .note { display: block; font-size: 12px; color: var(--muted); font-style: italic; }
tr.group td { background: var(--bg); font-family: var(--display); font-weight: 800; font-size: 14px; padding-top: 14px; }
tr.group td .gsum { font-family: var(--mono); font-weight: 600; }
td.total { font-weight: 600; }
td.mkt { font-family: var(--display); font-size: 13px; }
td.mkt small { display: block; font-family: var(--mono); font-size: 11px; color: var(--muted); }
.pill { display: inline-block; font-family: var(--display); font-size: 11px; font-weight: 600; letter-spacing: 0.04em;
  padding: 2px 8px; border-radius: 3px; }
.pill.easy { background: var(--easy-bg); color: var(--easy); }
.pill.list { background: var(--list-bg); color: var(--list); }
.pill.slow { background: var(--slow-bg); color: var(--slow); }
.pill.unknown, .pill.yours, .pill.silver { background: var(--unk-bg); color: var(--unk); }
footer { color: var(--muted); font-size: 12.5px; line-height: 1.6; max-width: 80ch; }
@media (max-width: 560px) { h1 { font-size: 23px; } .tile .value { font-size: 22px; } }
</style>

<div class="wrap">
  <header>
    <h1>Alouris <span>Ledger</span></h1>
    <div class="stamp" id="stamp"></div>
  </header>
  <section class="tiles" id="tiles"></section>
  <section class="bars" id="bars" aria-label="Value by location"></section>
  <div class="filters" id="filters"></div>
  <div class="ledger"><table>
    <thead><tr><th>Item</th><th>Qty</th><th>Each</th><th>Total</th><th>Sold/day</th><th style="text-align:left">Best market</th><th style="text-align:left">Status</th></tr></thead>
    <tbody id="rows"></tbody>
  </table></div>
  <footer id="footer"></footer>
</div>

<script id="ledger" type="application/json">__LEDGER_JSON__</script>
<script>
(function () {
  var data = JSON.parse(document.getElementById("ledger").textContent);
  var rows = data.rows;
  var nf = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  var STATUS = { silver: ["Silver", "silver"], easy: ["Sells easily", "easy"], list: ["List and wait", "list"],
                 slow: ["Rarely trades", "slow"], "your price": ["Your price", "yours"], unknown: ["No market data", "unknown"] };
  var COLORS = { easy: "var(--easy)", list: "var(--list)", slow: "var(--slow)", other: "var(--unk)" };
  function el(t, c, x) { var e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; }
  function silver(n) {
    var a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e8 ? 0 : 1) + "M";
    if (a >= 1e4) return Math.round(n / 1e3) + "k";
    return nf.format(n);
  }
  function sum(list) { return list.reduce(function (s, r) { return s + r.total; }, 0); }
  function bucket(r) { return r.status === "easy" || r.status === "list" || r.status === "slow" ? r.status : "other"; }

  document.getElementById("stamp").textContent = data.server + " · priced " +
    new Date(data.generated).toISOString().slice(0, 16).replace("T", " ") + " UTC";

  // Tiles
  var tiles = document.getElementById("tiles");
  function tile(label, value, sub, lead) {
    var t = el("div", "tile" + (lead ? " lead" : ""));
    t.appendChild(el("div", "label", label)); t.appendChild(el("div", "value", value));
    if (sub) t.appendChild(el("div", "sub", sub));
    tiles.appendChild(t);
  }
  var total = sum(rows);
  var silv = sum(rows.filter(function (r) { return r.status === "silver"; }));
  var easy = sum(rows.filter(function (r) { return r.status === "easy"; }));
  var unsure = rows.filter(function (r) { return r.status === "slow" || r.status === "unknown" || r.status === "your price"; });
  tile("Total value", silver(total), rows.length + " holdings", true);
  tile("Silver", silver(silv), "already liquid");
  tile("Sells easily", silver(easy), "at least " + 5 + " sold per day");
  tile("Hard to price", silver(sum(unsure)), unsure.length + " items rarely trade or have no data");

  // Location bars, split by how easily the value sells
  var locs = {};
  rows.forEach(function (r) {
    if (r.status === "silver") return;
    var k = r.character + " · " + r.location;
    locs[k] = locs[k] || { easy: 0, list: 0, slow: 0, other: 0, total: 0 };
    locs[k][bucket(r)] += r.total; locs[k].total += r.total;
  });
  var keys = Object.keys(locs).sort(function (a, b) { return locs[b].total - locs[a].total; });
  var max = Math.max.apply(null, keys.map(function (k) { return locs[k].total; }).concat([1]));
  var bars = document.getElementById("bars");
  var lg = el("div", "legend");
  [["easy", "Sells easily"], ["list", "List and wait"], ["slow", "Rarely trades"], ["other", "Your price / no data"]].forEach(function (p) {
    var s = el("span"); var i = el("i"); i.style.background = COLORS[p[0]]; s.appendChild(i); s.appendChild(document.createTextNode(p[1])); lg.appendChild(s);
  });
  bars.appendChild(lg);
  keys.forEach(function (k) {
    var b = el("div", "bar"); b.appendChild(el("div", "name", k));
    var tr = el("div", "track");
    ["easy", "list", "slow", "other"].forEach(function (s) {
      if (!locs[k][s]) return;
      var seg = el("div", "seg"); seg.style.width = (locs[k][s] / max * 100) + "%"; seg.style.background = COLORS[s];
      seg.title = silver(locs[k][s]); tr.appendChild(seg);
    });
    b.appendChild(tr); b.appendChild(el("div", "amt", silver(locs[k].total)));
    bars.appendChild(b);
  });

  // Filters
  var state = { loc: "all", status: "all" };
  var filters = document.getElementById("filters");
  function group(label, key, values, names) {
    filters.appendChild(el("span", "lbl", label));
    ["all"].concat(values).forEach(function (v) {
      var c = el("button", "chip", v === "all" ? "All" : (names ? names[v] : v));
      c.type = "button"; c.dataset.key = key; c.dataset.val = v;
      c.setAttribute("aria-pressed", String(state[key] === v));
      c.addEventListener("click", function () {
        state[key] = v;
        filters.querySelectorAll('[data-key="' + key + '"]').forEach(function (x) { x.setAttribute("aria-pressed", String(x.dataset.val === v)); });
        render();
      });
      filters.appendChild(c);
    });
  }
  var locNames = Array.from(new Set(rows.map(function (r) { return r.location; })));
  group("Location", "loc", locNames);
  var stNames = {}; Object.keys(STATUS).forEach(function (k) { stNames[k] = STATUS[k][0]; });
  group("Status", "status", Object.keys(STATUS).filter(function (k) { return rows.some(function (r) { return r.status === k; }); }), stNames);

  function render() {
    var body = document.getElementById("rows"); body.textContent = "";
    var list = rows.filter(function (r) {
      return (state.loc === "all" || r.location === state.loc) && (state.status === "all" || r.status === state.status);
    });
    var groups = {};
    list.forEach(function (r) { var k = r.character + " · " + r.location; (groups[k] = groups[k] || []).push(r); });
    Object.keys(groups).sort(function (a, b) { return sum(groups[b]) - sum(groups[a]); }).forEach(function (k) {
      var g = el("tr", "group"); var td = el("td", null, k + "  "); td.colSpan = 3;
      g.appendChild(td); var s = el("td", "gsum", silver(sum(groups[k]))); g.appendChild(s);
      var pad = el("td"); pad.colSpan = 3; g.appendChild(pad); body.appendChild(g);
      groups[k].sort(function (a, b) { return b.total - a.total; }).forEach(function (r) {
        var tr = el("tr");
        var it = el("td", "item", r.name);
        if (r.item_id !== r.name && r.item_id !== "SILVER") it.appendChild(el("span", "id", r.item_id + (r.quality ? " · quality " + r.quality : "")));
        if (r.note) it.appendChild(el("span", "note", r.note));
        tr.appendChild(it);
        tr.appendChild(el("td", null, nf.format(r.qty)));
        tr.appendChild(el("td", null, r.each == null ? "–" : silver(r.each)));
        tr.appendChild(el("td", "total", silver(r.total)));
        tr.appendChild(el("td", null, r.per_day ? (r.per_day >= 10 ? nf.format(r.per_day) : r.per_day.toFixed(1)) : "–"));
        var m = el("td", "mkt"); m.style.textAlign = "left";
        if (r.best_market) { m.appendChild(document.createTextNode(r.best_market)); m.appendChild(el("small", null, silver(r.best_price) + " avg")); } else m.textContent = "–";
        tr.appendChild(m);
        var st = el("td"); st.style.textAlign = "left"; st.appendChild(el("span", "pill " + STATUS[r.status][1], STATUS[r.status][0]));
        tr.appendChild(st);
        body.appendChild(tr);
      });
    });
  }
  render();

  document.getElementById("footer").textContent = "Each item is valued at its typical price: the volume-weighted average of actual sales over the last " +
    data.days + " days across the royal cities, Caerleon, Brecilien and the Black Market, before tax. Best market is where it sold for the most. " +
    "Items with no sales use your own price where you gave one. Items read from screenshots may have the wrong set or enchant; the note says so. " +
    "Awakened weapons can't be sold, so they're not listed.";
})();
</script>
"""


if __name__ == "__main__":
    main()
