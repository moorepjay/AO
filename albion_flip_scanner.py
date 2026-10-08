#!/usr/bin/env python3
"""
Albion Flip Scanner (MVP)
-------------------------
Scans Albion Online Data Project market data (Americas server by default) for
profitable flips:

  * Royal city -> Royal city arbitrage
  * Royal city -> Black Market (Caerleon)

Standard library only. Python 3.8+.

Examples
  python albion_flip_scanner.py                       # auto item list, T4-T6 gear
  python albion_flip_scanner.py --tiers 5,6 --top 30
  python albion_flip_scanner.py --items T4_BAG,T5_BAG,T6_BAG --premium
  python albion_flip_scanner.py --mode bm --budget 38617746

Data is crowd-sourced by players running the Albion Data Client, so rows can be
stale or wrong. Always sanity-check a flip in-game before committing silver.
"""
import argparse
import csv
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HOSTS = {
    "americas": "https://west.albion-online-data.com",
    "asia": "https://east.albion-online-data.com",
    "europe": "https://europe.albion-online-data.com",
}
ITEMS_TXT = ("https://raw.githubusercontent.com/ao-data/ao-bin-dumps/"
             "master/formatted/items.txt")

ROYAL = ["Bridgewatch", "Lymhurst", "Martlock", "Fort Sterling", "Thetford"]
CAERLEON = "Caerleon"
BLACK_MARKET = "Black Market"

# Market fees. VERIFY against the in-game market window; they can change.
TAX_PREMIUM = 0.04      # sales tax with premium
TAX_NO_PREMIUM = 0.08   # sales tax without premium
SETUP_FEE = 0.025       # fee for placing a sell/buy order

GEAR_PATTERN = re.compile(r"^T(\d)_(MAIN|2H|OFF|HEAD|ARMOR|SHOES|CAPEITEM|BAG)_[A-Z0-9_]+$")
ITEM_LINE = re.compile(r"^\s*\d+:\s+(\S+)\s*:\s*(.*)$")

MAX_URL = 3800  # API cap is 4096 chars; keep headroom


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def http_get(url, retries=4):
    req = urllib.request.Request(url, headers={
        "Accept-Encoding": "gzip",
        "User-Agent": "albion-flip-scanner-mvp/0.1",
    })
    delay = 2.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def get_json(url):
    return json.loads(http_get(url))


# --------------------------------------------------------------------------
# Item list
# --------------------------------------------------------------------------
def load_item_names():
    """Download items.txt and return {item_id: display_name}."""
    names = {}
    for line in http_get(ITEMS_TXT).splitlines():
        m = ITEM_LINE.match(line)
        if m:
            names[m.group(1)] = m.group(2).strip()
    return names


def auto_items(names, tiers):
    out = []
    for item_id in names:
        m = GEAR_PATTERN.match(item_id)
        if m and int(m.group(1)) in tiers:
            out.append(item_id)
    return sorted(out)


# --------------------------------------------------------------------------
# Price fetching
# --------------------------------------------------------------------------
def chunk_items(items, base_len):
    """Yield chunks of item ids so the final URL stays under MAX_URL."""
    chunk, size = [], base_len
    for it in items:
        add = len(it) + 1
        if chunk and size + add > MAX_URL:
            yield chunk
            chunk, size = [], base_len
        chunk.append(it)
        size += add
    if chunk:
        yield chunk


def parse_ts(s):
    """API timestamp -> aware datetime, or None when the field is empty."""
    if not s or s.startswith("0001"):
        return None
    try:
        dt = datetime.fromisoformat(s.rstrip("Z"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def fetch_prices(host, items, locations, quality, pause=0.4, progress=True):
    """Return {(item_id, city): row} for current prices."""
    loc_q = urllib.parse.quote(",".join(locations), safe=",")
    suffix = f".json?locations={loc_q}&qualities={quality}"
    base_len = len(host) + len("/api/v2/stats/prices/") + len(suffix)
    result = {}
    chunks = list(chunk_items(items, base_len))
    for i, chunk in enumerate(chunks, 1):
        url = f"{host}/api/v2/stats/prices/{','.join(chunk)}{suffix}"
        if progress:
            print(f"  prices: request {i}/{len(chunks)}", file=sys.stderr)
        for row in get_json(url):
            if int(row.get("quality", quality)) != quality:
                continue
            result[(row["item_id"], row["city"])] = row
        time.sleep(pause)
    return result


def fetch_volume(host, pairs, quality, pause=0.4):
    """
    Average daily items sold over recent history for (item, city) pairs.
    Returns {(item_id, city): avg_daily_count}.
    """
    by_city = {}
    for item, city in pairs:
        by_city.setdefault(city, set()).add(item)
    out = {}
    for city, item_set in by_city.items():
        loc_q = urllib.parse.quote(city)
        suffix = f".json?locations={loc_q}&qualities={quality}&time-scale=24"
        base_len = len(host) + len("/api/v2/stats/history/") + len(suffix)
        for chunk in chunk_items(sorted(item_set), base_len):
            url = f"{host}/api/v2/stats/history/{','.join(chunk)}{suffix}"
            try:
                rows = get_json(url)
            except Exception as e:  # volume is a nice-to-have, never fatal
                print(f"  history fetch failed: {e}", file=sys.stderr)
                continue
            for r in rows:
                data = (r.get("data") or [])[-7:]
                if data:
                    avg = sum(d.get("item_count", 0) for d in data) / len(data)
                    out[(r["item_id"], r.get("location", city))] = avg
            time.sleep(pause)
    return out


# --------------------------------------------------------------------------
# Flip evaluation
# --------------------------------------------------------------------------
def age_hours(ts, now):
    return (now - ts).total_seconds() / 3600.0 if ts else None


def fresh_value(row, price_field, date_field, now, max_age):
    """Return the price if it exists and is fresh enough, else None."""
    if not row:
        return None
    price = int(row.get(price_field) or 0)
    if price <= 0:
        return None
    age = age_hours(parse_ts(row.get(date_field)), now)
    if age is None or age > max_age:
        return None
    return price, age


def evaluate(item, src, dst, prices, cfg, now):
    """Return a candidate dict or None."""
    buy = fresh_value(prices.get((item, src)), "sell_price_min",
                      "sell_price_min_date", now, cfg["max_age"])
    if not buy:
        return None
    cost, buy_age = buy
    tax = cfg["tax"]
    drow = prices.get((item, dst))

    # Instant sell into an existing buy order (no setup fee, just tax)
    inst = fresh_value(drow, "buy_price_max", "buy_price_max_date",
                       now, cfg["max_age"])
    # Listing a sell order (undercut not modelled; tax + setup fee)
    listed = None
    if dst != BLACK_MARKET:
        listed = fresh_value(drow, "sell_price_min", "sell_price_min_date",
                             now, cfg["max_age"])

    inst_profit = round(inst[0] * (1 - tax) - cost) if inst else None
    list_profit = round(listed[0] * (1 - tax - SETUP_FEE) - cost) if listed else None

    if cfg["sell_mode"] == "listed" and list_profit is not None:
        sell_price, sell_age, profit = listed[0], listed[1], list_profit
        how = "listed"
    elif inst_profit is not None:
        sell_price, sell_age, profit = inst[0], inst[1], inst_profit
        how = "instant"
    else:
        return None

    roi = profit / cost * 100.0
    if profit < cfg["min_profit"] or roi < cfg["min_roi"] or roi > cfg["max_roi"]:
        return None

    return {
        "item": item, "src": src, "dst": dst, "how": how,
        "buy": cost, "sell": sell_price, "profit": profit, "roi": roi,
        "data_age_h": max(buy_age, sell_age),
        "risk": "HIGH" if CAERLEON in (src, dst) or BLACK_MARKET in (src, dst) else "MED",
    }


def size_position(c, cfg, volume):
    """Quantity: capital allocation for this risk tier, capped by volume."""
    frac = cfg["risk_frac"] if c["risk"] == "HIGH" else cfg["med_frac"]
    qty = int(cfg["budget"] * frac // c["buy"])
    vol = volume.get((c["item"], c["dst"]))
    if vol is not None:
        qty = min(qty, int(vol * cfg["vol_share"]))
    c["volume"] = vol
    c["qty"] = max(qty, 0)
    c["total_profit"] = c["qty"] * c["profit"]
    c["total_cost"] = c["qty"] * c["buy"]
    return c


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def fmt(n):
    return f"{n:,}" if isinstance(n, int) else str(n)


def print_table(rows, names):
    hdr = ("Item", "Route", "Buy", "Sell", "Profit", "ROI%", "Qty",
           "Total profit", "Vol/d", "Age(h)", "Risk")
    table = []
    for c in rows:
        name = names.get(c["item"], c["item"])
        label = f"{c['item'].split('_')[0]} {name}"[:34]
        vol = "?" if c["volume"] is None else f"{c['volume']:.0f}"
        table.append((
            label, f"{c['src']}->{c['dst']}", fmt(c["buy"]), fmt(c["sell"]),
            fmt(c["profit"]), f"{c['roi']:.0f}", fmt(c["qty"]),
            fmt(c["total_profit"]), vol, f"{c['data_age_h']:.1f}", c["risk"],
        ))
    widths = [max(len(str(x)) for x in col) for col in zip(hdr, *table)]
    line = "  ".join(h.ljust(w) for h, w in zip(hdr, widths))
    print(line)
    print("-" * len(line))
    for r in table:
        print("  ".join(str(x).ljust(w) for x, w in zip(r, widths)))


def write_csv(path, rows, names):
    cols = ["item", "name", "src", "dst", "how", "buy", "sell", "profit", "roi",
            "qty", "total_cost", "total_profit", "volume", "data_age_h", "risk"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for c in rows:
            w.writerow([names.get(c["item"], "") if k == "name" else c.get(k, "")
                        for k in cols])


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="Albion Online flip scanner (MVP)")
    p.add_argument("--server", choices=HOSTS, default="americas")
    p.add_argument("--mode", choices=["both", "royal", "bm"], default="both",
                   help="royal = city<->city, bm = Royal->Black Market")
    p.add_argument("--items", help="comma-separated item ids (skips auto list)")
    p.add_argument("--tiers", default="4,5,6", help="tiers for auto list")
    p.add_argument("--limit-items", type=int, default=0,
                   help="cap auto item count (for quick tests)")
    p.add_argument("--quality", type=int, default=1, choices=[1, 2, 3, 4, 5])
    p.add_argument("--premium", action="store_true",
                   help="use premium tax rate (4%% instead of 8%%)")
    p.add_argument("--budget", type=int, default=38_617_746,
                   help="total silver you're working with")
    p.add_argument("--risk-frac", type=float, default=0.25,
                   help="max share of budget on one HIGH-risk position")
    p.add_argument("--med-frac", type=float, default=0.5,
                   help="max share of budget on one MED-risk position")
    p.add_argument("--max-age", type=float, default=6.0,
                   help="drop price rows older than this many hours")
    p.add_argument("--min-profit", type=int, default=5000)
    p.add_argument("--min-roi", type=float, default=5.0)
    p.add_argument("--max-roi", type=float, default=200.0,
                   help="drop absurd margins (usually bad data)")
    p.add_argument("--sell-mode", choices=["instant", "listed"], default="instant",
                   help="Royal sales: instant into buy order, or list a sell order")
    p.add_argument("--vol-share", type=float, default=0.25,
                   help="max share of avg daily volume to move in one trip")
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--sort", choices=["total", "profit", "roi"], default="total")
    p.add_argument("--no-volume", action="store_true", help="skip history lookups")
    p.add_argument("--csv", default="flips.csv")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    host = HOSTS[args.server]
    now = datetime.now(timezone.utc)
    cfg = {
        "tax": TAX_PREMIUM if args.premium else TAX_NO_PREMIUM,
        "max_age": args.max_age, "min_profit": args.min_profit,
        "min_roi": args.min_roi, "max_roi": args.max_roi,
        "sell_mode": args.sell_mode, "budget": args.budget,
        "risk_frac": args.risk_frac, "med_frac": args.med_frac,
        "vol_share": args.vol_share,
    }

    names = {}
    try:
        names = load_item_names()
    except Exception as e:
        print(f"Could not load item names ({e}); continuing with ids only.",
              file=sys.stderr)

    if args.items:
        items = [i.strip() for i in args.items.split(",") if i.strip()]
    else:
        if not names:
            sys.exit("Auto item list needs items.txt; pass --items instead.")
        tiers = {int(t) for t in args.tiers.split(",")}
        items = auto_items(names, tiers)
        if args.limit_items:
            items = items[:args.limit_items]
    print(f"Scanning {len(items)} items on {args.server} "
          f"(tax {cfg['tax']*100:.0f}%, quality {args.quality})", file=sys.stderr)

    locations = list(ROYAL)
    if args.mode in ("both", "bm"):
        locations.append(BLACK_MARKET)
    if args.mode in ("both", "royal"):
        locations.append(CAERLEON)
    prices = fetch_prices(host, items, locations, args.quality)

    # Build route list
    routes = []
    if args.mode in ("both", "royal"):
        sources = ROYAL + [CAERLEON]
        for s in sources:
            for d in sources:
                if s != d:
                    routes.append((s, d))
    if args.mode in ("both", "bm"):
        routes += [(s, BLACK_MARKET) for s in ROYAL]

    cands = []
    for item in items:
        for s, d in routes:
            c = evaluate(item, s, d, prices, cfg, now)
            if c:
                cands.append(c)
    if not cands:
        print("No flips passed the filters. Try a larger --max-age or lower --min-roi.")
        return []

    # Pre-rank, then pull volume only for the leaders (saves API calls)
    cands.sort(key=lambda c: c["profit"], reverse=True)
    shortlist = cands[: max(args.top * 4, 60)]
    volume = {}
    if not args.no_volume:
        print("Checking sales volume for shortlist...", file=sys.stderr)
        volume = fetch_volume(host, {(c["item"], c["dst"]) for c in shortlist},
                              args.quality)
    sized = [size_position(c, cfg, volume) for c in shortlist]
    sized = [c for c in sized if c["qty"] > 0]

    key = {"total": "total_profit", "profit": "profit", "roi": "roi"}[args.sort]
    sized.sort(key=lambda c: c[key], reverse=True)
    top = sized[: args.top]

    print()
    print_table(top, names)
    write_csv(args.csv, top, names)
    print(f"\nSaved {len(top)} rows to {args.csv}")
    print("Reminders: confirm prices in-game, check zone danger on your route, "
          "and don't carry more than your risk allocation.")
    return top


if __name__ == "__main__":
    main()
