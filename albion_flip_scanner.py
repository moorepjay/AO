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
  python albion_flip_scanner.py --items-file my_items.txt

Data is crowd-sourced by players running the Albion Data Client, so rows can be
stale or wrong. Always sanity-check a flip in-game before committing silver.
"""
import argparse
import csv
import gzip
import json
import os
import re
import socket
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

GEAR_PATTERN = re.compile(
    r"^T(\d)_(?:(?:MAIN|2H|OFF|HEAD|ARMOR|SHOES|CAPEITEM|BAG)_[A-Z0-9_]+|BAG|CAPE)$")
ITEM_LINE = re.compile(r"^\s*\d+:\s+(\S+)\s*:\s*(.*)$")

MAX_URL = 3800  # API cap is 4096 chars; keep headroom
CACHE_FILE = os.path.join(os.path.expanduser("~"), ".cache",
                          "albion-flip-scanner", "items.txt")

# Fallback gear list with English names, used when items.txt can't be
# downloaded and there's no cached copy. Keys are ids without the "T<n>_"
# prefix; the tier word ("Adept's", "Expert's", ...) is added per tier.
TIER_WORDS = {1: "Beginner's", 2: "Novice's", 3: "Journeyman's", 4: "Adept's",
              5: "Expert's", 6: "Master's", 7: "Grandmaster's", 8: "Elder's"}
_ARMOR_SETS = {
    "PLATE": ({"SET1": "Soldier", "SET2": "Knight", "SET3": "Guardian",
               "UNDEAD": "Graveguard", "HELL": "Demon", "KEEPER": "Judicator"},
              ("Helmet", "Armor", "Boots")),
    "LEATHER": ({"SET1": "Mercenary", "SET2": "Hunter", "SET3": "Assassin",
                 "UNDEAD": "Specter", "HELL": "Hellion", "MORGANA": "Stalker"},
                ("Hood", "Jacket", "Shoes")),
    "CLOTH": ({"SET1": "Scholar", "SET2": "Cleric", "SET3": "Mage",
               "UNDEAD": "Cultist", "HELL": "Fiend", "KEEPER": "Druid"},
              ("Cowl", "Robe", "Sandals")),
}
BUILTIN_NAMES = {
    # swords, axes, maces, hammers
    "MAIN_SWORD": "Broadsword", "2H_CLAYMORE": "Claymore",
    "2H_DUALSWORD": "Dual Swords", "MAIN_SCIMITAR_MORGANA": "Clarent Blade",
    "2H_CLEAVER_HELL": "Carving Sword", "2H_DUALSCIMITAR_UNDEAD": "Galatine Pair",
    "MAIN_AXE": "Battleaxe", "2H_AXE": "Greataxe", "2H_HALBERD": "Halberd",
    "2H_HALBERD_MORGANA": "Carrioncaller", "2H_SCYTHE_HELL": "Infernal Scythe",
    "2H_DUALAXE_KEEPER": "Bear Paws",
    "MAIN_MACE": "Mace", "2H_MACE": "Heavy Mace", "2H_FLAIL": "Morning Star",
    "MAIN_ROCKMACE_KEEPER": "Bedrock Mace", "MAIN_MACE_HELL": "Incubus Mace",
    "2H_MACE_MORGANA": "Camlann Mace",
    "MAIN_HAMMER": "Hammer", "2H_POLEHAMMER": "Polehammer",
    "2H_HAMMER": "Great Hammer", "2H_HAMMER_UNDEAD": "Tombhammer",
    "2H_DUALHAMMER_HELL": "Forge Hammers", "2H_RAM_KEEPER": "Grovekeeper",
    # daggers, spears, quarterstaffs, war gloves
    "MAIN_DAGGER": "Dagger", "2H_DAGGERPAIR": "Dagger Pair", "2H_CLAWPAIR": "Claws",
    "MAIN_RAPIER_MORGANA": "Bloodletter", "MAIN_DAGGER_HELL": "Demonfang",
    "2H_DUALSICKLE_UNDEAD": "Deathgivers",
    "MAIN_SPEAR": "Spear", "2H_SPEAR": "Pike", "2H_GLAIVE": "Glaive",
    "MAIN_SPEAR_KEEPER": "Heron Spear", "2H_HARPOON_HELL": "Spirithunter",
    "2H_TRIDENT_UNDEAD": "Trinity Spear",
    "2H_QUARTERSTAFF": "Quarterstaff", "2H_IRONCLADEDSTAFF": "Iron-clad Staff",
    "2H_DOUBLEBLADEDSTAFF": "Double Bladed Staff",
    "2H_COMBATSTAFF_MORGANA": "Black Monk Stave", "2H_TWINSCYTHE_HELL": "Soulscythe",
    "2H_ROCKSTAFF_KEEPER": "Staff of Balance",
    "2H_KNUCKLES_SET1": "Brawler Gloves", "2H_KNUCKLES_SET2": "Battle Bracers",
    "2H_KNUCKLES_SET3": "Spiked Gauntlets", "2H_KNUCKLES_KEEPER": "Ursine Maulers",
    "2H_KNUCKLES_HELL": "Hellfire Hands", "2H_KNUCKLES_MORGANA": "Ravenstrike Cestus",
    # bows, crossbows
    "2H_BOW": "Bow", "2H_WARBOW": "Warbow", "2H_LONGBOW": "Longbow",
    "2H_LONGBOW_UNDEAD": "Whispering Bow", "2H_BOW_HELL": "Wailing Bow",
    "2H_BOW_KEEPER": "Bow of Badon",
    "2H_CROSSBOW": "Crossbow", "2H_CROSSBOWLARGE": "Heavy Crossbow",
    "MAIN_1HCROSSBOW": "Light Crossbow",
    "2H_REPEATINGCROSSBOW_UNDEAD": "Weeping Repeater",
    "2H_DUALCROSSBOW_HELL": "Boltcasters", "2H_CROSSBOWLARGE_MORGANA": "Siegebow",
    # staffs
    "MAIN_FIRESTAFF": "Fire Staff", "2H_FIRESTAFF": "Great Fire Staff",
    "2H_INFERNOSTAFF": "Infernal Staff", "MAIN_FIRESTAFF_KEEPER": "Wildfire Staff",
    "2H_FIRESTAFF_HELL": "Brimstone Staff", "2H_INFERNOSTAFF_MORGANA": "Blazing Staff",
    "MAIN_FROSTSTAFF": "Frost Staff", "2H_FROSTSTAFF": "Great Frost Staff",
    "2H_GLACIALSTAFF": "Glacial Staff", "MAIN_FROSTSTAFF_KEEPER": "Hoarfrost Staff",
    "2H_ICEGAUNTLETS_HELL": "Icicle Staff", "2H_ICECRYSTAL_UNDEAD": "Permafrost Prism",
    "MAIN_ARCANESTAFF": "Arcane Staff", "2H_ARCANESTAFF": "Great Arcane Staff",
    "2H_ENIGMATICSTAFF": "Enigmatic Staff",
    "MAIN_ARCANESTAFF_UNDEAD": "Witchwork Staff", "2H_ARCANESTAFF_HELL": "Occult Staff",
    "2H_ENIGMATICORB_MORGANA": "Malevolent Locus",
    "MAIN_HOLYSTAFF": "Holy Staff", "2H_HOLYSTAFF": "Great Holy Staff",
    "2H_DIVINESTAFF": "Divine Staff", "MAIN_HOLYSTAFF_MORGANA": "Lifetouch Staff",
    "2H_HOLYSTAFF_HELL": "Fallen Staff", "2H_HOLYSTAFF_UNDEAD": "Redemption Staff",
    "MAIN_NATURESTAFF": "Nature Staff", "2H_NATURESTAFF": "Great Nature Staff",
    "2H_WILDSTAFF": "Wild Staff", "MAIN_NATURESTAFF_KEEPER": "Druidic Staff",
    "2H_NATURESTAFF_HELL": "Blight Staff", "2H_NATURESTAFF_KEEPER": "Rampant Staff",
    "MAIN_CURSEDSTAFF": "Cursed Staff", "2H_CURSEDSTAFF": "Great Cursed Staff",
    "2H_DEMONICSTAFF": "Demonic Staff", "MAIN_CURSEDSTAFF_UNDEAD": "Lifecurse Staff",
    "2H_SKULLORB_HELL": "Cursed Skull", "2H_CURSEDSTAFF_MORGANA": "Damnation Staff",
    # off-hands
    "OFF_SHIELD": "Shield", "OFF_TOWERSHIELD_UNDEAD": "Sarcophagus",
    "OFF_SPIKEDSHIELD_MORGANA": "Caitiff Shield", "OFF_SHIELD_HELL": "Facebreaker",
    "OFF_BOOK": "Tome of Spells", "OFF_ORB_MORGANA": "Eye of Secrets",
    "OFF_DEMONSKULL_HELL": "Muisak", "OFF_TOTEM_KEEPER": "Taproot",
    "OFF_TORCH": "Torch", "OFF_HORN_KEEPER": "Mistcaller",
    "OFF_LAMP_UNDEAD": "Cryptcandle", "OFF_JESTERCANE_HELL": "Leering Cane",
    # capes and bags
    "CAPE": "Cape", "BAG": "Bag", "BAG_INSIGHT": "Satchel of Insight",
    "CAPEITEM_FW_BRIDGEWATCH": "Bridgewatch Cape",
    "CAPEITEM_FW_FORTSTERLING": "Fort Sterling Cape",
    "CAPEITEM_FW_LYMHURST": "Lymhurst Cape", "CAPEITEM_FW_MARTLOCK": "Martlock Cape",
    "CAPEITEM_FW_THETFORD": "Thetford Cape", "CAPEITEM_FW_CAERLEON": "Caerleon Cape",
}
for _kind, (_sets, _pieces) in _ARMOR_SETS.items():
    for _fam, _set_name in _sets.items():
        for _slot, _piece in zip(("HEAD", "ARMOR", "SHOES"), _pieces):
            BUILTIN_NAMES[f"{_slot}_{_kind}_{_fam}"] = f"{_set_name} {_piece}"
BUILTIN_GEAR = list(BUILTIN_NAMES)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _transient(err):
    """True for failures worth retrying (rate limit, server error, timeout)."""
    if isinstance(err, urllib.error.HTTPError):
        return err.code == 429 or err.code >= 500
    if isinstance(err, urllib.error.URLError):
        err = err.reason
    return isinstance(err, (socket.timeout, TimeoutError, ConnectionError))


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
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            if attempt < retries - 1 and _transient(e):
                time.sleep(delay)
                delay *= 2
                continue
            raise


def get_json(url):
    return json.loads(http_get(url))


# --------------------------------------------------------------------------
# Item list
# --------------------------------------------------------------------------
def parse_items_txt(text):
    names = {}
    for line in text.splitlines():
        m = ITEM_LINE.match(line)
        if m:
            names[m.group(1)] = m.group(2).strip()
    return names


def load_item_names(cache_file=CACHE_FILE):
    """
    Return {item_id: display_name} from items.txt. A successful download is
    cached; if the download fails, the cached copy is used instead.
    """
    try:
        text = http_get(ITEMS_TXT, retries=2)
    except Exception as e:
        if cache_file and os.path.exists(cache_file):
            print(f"Could not download item names ({e}); using cached copy "
                  f"{cache_file}.", file=sys.stderr)
            with open(cache_file, encoding="utf-8") as f:
                return parse_items_txt(f.read())
        raise
    names = parse_items_txt(text)
    if cache_file and names:
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass  # the cache is a convenience only
    return names


def builtin_name(item_id):
    """English name for a built-in gear id, e.g. T6_ARMOR_CLOTH_SET2@1 ->
    "Master's Cleric Robe .1", or None when the id isn't in the list."""
    m = re.match(r"^T(\d)_(.+?)(?:@(\d))?$", item_id)
    if not m or m.group(2) not in BUILTIN_NAMES:
        return None
    name = f"{TIER_WORDS[int(m.group(1))]} {BUILTIN_NAMES[m.group(2)]}"
    return f"{name} .{m.group(3)}" if m.group(3) else name


def builtin_items(tiers):
    return sorted(f"T{t}_{base}" for t in tiers for base in BUILTIN_GEAR)


def read_items_file(path):
    """Item ids separated by commas or whitespace; '#' starts a comment."""
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            items += re.split(r"[\s,]+", line.split("#", 1)[0].strip())
    return [i for i in items if i]


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
        label = (names.get(c["item"]) or c["item"])[:40]
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


def write_html(path, rows, names, meta):
    """Self-contained results page (sortable table + summary)."""
    cols = ["item", "src", "dst", "how", "buy", "sell", "profit", "roi", "qty",
            "total_cost", "total_profit", "volume", "data_age_h", "risk"]
    data = dict(meta, rows=[dict({k: c.get(k) for k in cols},
                                 name=names.get(c["item"], ""))
                            for c in rows])
    # "</" inside the JSON would end the <script> block early
    blob = json.dumps(data).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE.replace("__SCAN_JSON__", blob))


HTML_TEMPLATE = r"""<title>Albion Flip Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
/* Layout: one column — header strip, three summary tiles, filter chips, wide sortable ledger. */
:root {
  --bg: #f3f4f7; --panel: #ffffff; --fg: #1b2230; --muted: #5d6779; --line: #dde1e8;
  --accent: #b07a12; --accent-soft: #f6ead0;
  --good: #1f7a4d; --warn: #a3620a; --bad: #b3261e;
  --high-bg: #fbe3e1; --med-bg: #e3eefb; --med-fg: #1d4f91;
  --display: "Archivo", system-ui, sans-serif;
  --body: "Archivo", system-ui, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #12161d; --panel: #1a2029; --fg: #e6e9ef; --muted: #98a2b3; --line: #2b3340;
  --accent: #e0aa45; --accent-soft: #3a2f19;
  --good: #5fc796; --warn: #e8a64a; --bad: #f08a80;
  --high-bg: #3d2224; --med-bg: #1f2d42; --med-fg: #9cc3f5; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #12161d; --panel: #1a2029; --fg: #e6e9ef; --muted: #98a2b3; --line: #2b3340;
  --accent: #e0aa45; --accent-soft: #3a2f19;
  --good: #5fc796; --warn: #e8a64a; --bad: #f08a80;
  --high-bg: #3d2224; --med-bg: #1f2d42; --med-fg: #9cc3f5; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font-family: var(--body); font-size: 14px; }
.wrap { max-width: 1200px; margin: 0 auto; padding-inline: 16px; padding-block: 24px 48px;
  display: grid; gap: 20px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 24px; }
h1 { font-family: var(--display); font-weight: 800; font-size: 28px; margin: 0; letter-spacing: -0.01em; text-wrap: balance; }
h1 span { color: var(--accent); }
.stamp { color: var(--muted); font-family: var(--mono); font-size: 12px; }
.settings { display: flex; flex-wrap: wrap; gap: 6px; }
.settings span { font-family: var(--mono); font-size: 11.5px; padding: 3px 8px; border: 1px solid var(--line);
  border-radius: 4px; color: var(--muted); background: var(--panel); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 14px 16px; min-width: 0; }
.tile .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }
.tile .value { font-family: var(--mono); font-size: 26px; font-weight: 600; margin-top: 4px;
  font-variant-numeric: tabular-nums; }
.tile .value small { font-size: 13px; color: var(--muted); font-weight: 400; }
.tile .sub { color: var(--muted); font-size: 12.5px; margin-top: 2px; overflow-wrap: anywhere; }
.tile.lead .value { color: var(--accent); }
.filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.filters .lbl { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-right: 4px; }
.chip { font: inherit; font-size: 13px; padding: 5px 11px; border-radius: 999px; border: 1px solid var(--line);
  background: var(--panel); color: var(--fg); cursor: pointer; }
.chip[aria-pressed="true"] { background: var(--accent-soft); border-color: var(--accent); color: var(--fg); font-weight: 600; }
.chip:focus-visible, th button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ledger { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; }
table { border-collapse: collapse; width: 100%; min-width: 980px; }
th, td { padding: 9px 10px; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; }
th:nth-child(-n+3), td:nth-child(-n+3) { text-align: left; }
th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600;
  position: sticky; top: 0; background: var(--panel); }
th button { all: unset; cursor: pointer; }
th button[data-dir]::after { content: " ▾"; color: var(--accent); }
th button[data-dir="asc"]::after { content: " ▴"; }
td { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 13px; }
td.item { font-family: var(--body); font-size: 14px; }
td.item .id { display: block; font-family: var(--mono); font-size: 11px; color: var(--muted); }
td.rank { color: var(--muted); }
tbody tr:hover { background: var(--accent-soft); }
.route { font-family: var(--body); font-size: 13.5px; }
.route .arrow { color: var(--muted); padding: 0 4px; }
.pill { display: inline-block; font-family: var(--body); font-size: 10.5px; font-weight: 600; letter-spacing: 0.06em;
  padding: 2px 7px; border-radius: 3px; margin-left: 6px; vertical-align: 1px; }
.pill.HIGH { background: var(--high-bg); color: var(--bad); }
.pill.MED { background: var(--med-bg); color: var(--med-fg); }
.profit { color: var(--good); font-weight: 600; }
.total { font-weight: 600; }
.age-ok { color: var(--fg); } .age-warn { color: var(--warn); } .age-old { color: var(--bad); }
.empty { padding: 40px 16px; text-align: center; color: var(--muted); }
.empty strong { display: block; color: var(--fg); font-size: 16px; margin-bottom: 6px; }
footer { color: var(--muted); font-size: 12.5px; line-height: 1.6; max-width: 75ch; }
footer code { font-family: var(--mono); font-size: 12px; }
@media (max-width: 560px) { h1 { font-size: 23px; } .tile .value { font-size: 22px; } }
</style>

<div class="wrap">
  <header>
    <h1>Albion <span>Flip Board</span></h1>
    <div class="stamp" id="stamp"></div>
  </header>
  <div class="settings" id="settings"></div>
  <section class="tiles" id="tiles"></section>
  <div class="filters" id="filters"></div>
  <div class="ledger"><table>
    <thead><tr id="head"></tr></thead>
    <tbody id="rows"></tbody>
  </table><div class="empty" id="empty" hidden></div></div>
  <footer>
    Buy at the source city's lowest sell order, sell instantly into the destination's highest buy order
    (or list it, where shown), after market tax. Qty is capped by your risk allocation and by a share of
    the destination's average daily sales. Prices come from players running the Albion Data Client, so
    confirm them in-game, check the danger of your route, and don't carry more than you can lose.
  </footer>
</div>

<script id="scan" type="application/json">__SCAN_JSON__</script>
<script>
(function () {
  var scan = JSON.parse(document.getElementById("scan").textContent);
  var rows = scan.rows.map(function (r, i) { r.rank = i + 1; return r; });
  var nf = new Intl.NumberFormat("en-US");
  function silver(n) {
    var a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e8 ? 0 : 1) + "M";
    if (a >= 1e4) return Math.round(n / 1e3) + "k";
    return nf.format(n);
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  var when = new Date(scan.generated);
  document.getElementById("stamp").textContent =
    scan.server + " · scanned " + when.toISOString().slice(0, 16).replace("T", " ") + " UTC";
  var s = scan.settings;
  [s.items + " items", "tiers " + s.tiers, "tax " + s.tax + "%", "max age " + s.max_age + "h",
   "min profit " + nf.format(s.min_profit), "ROI " + s.min_roi + "–" + s.max_roi + "%",
   "budget " + silver(s.budget), "quality " + s.quality, "sell " + s.sell_mode]
    .forEach(function (t) { document.getElementById("settings").appendChild(el("span", null, t)); });

  // Summary tiles
  var tiles = document.getElementById("tiles");
  function tile(label, value, unit, sub, lead) {
    var t = el("div", "tile" + (lead ? " lead" : ""));
    t.appendChild(el("div", "label", label));
    var v = el("div", "value", value);
    if (unit) v.appendChild(el("small", null, " " + unit));
    t.appendChild(v);
    if (sub) t.appendChild(el("div", "sub", sub));
    tiles.appendChild(t);
  }
  if (rows.length) {
    var best = rows.reduce(function (a, b) { return b.total_profit > a.total_profit ? b : a; });
    tile("Best trip", silver(best.total_profit), "silver",
         best.qty + " × " + (best.name || best.item) + ", " + best.src + " → " + best.dst, true);
    var bestEa = rows.reduce(function (a, b) { return b.profit > a.profit ? b : a; });
    tile("Best per item", silver(bestEa.profit), "each",
         (bestEa.name || bestEa.item) + " at " + bestEa.roi.toFixed(0) + "% ROI");
    var ages = rows.map(function (r) { return r.data_age_h; }).sort(function (a, b) { return a - b; });
    tile("Flips found", String(rows.length), null,
         "median price age " + ages[Math.floor(ages.length / 2)].toFixed(1) + "h");
  } else {
    tile("Flips found", "0", null, "nothing passed the filters this scan", true);
  }

  // Filters
  var dests = Array.from(new Set(rows.map(function (r) { return r.dst; }))).sort();
  var state = { dst: "all", risk: "all", sort: "total_profit", dir: "desc" };
  var filters = document.getElementById("filters");
  function chipGroup(label, key, values) {
    if (values.length < 2) return;
    filters.appendChild(el("span", "lbl", label));
    ["all"].concat(values).forEach(function (v) {
      var b = el("button", "chip", v === "all" ? "All" : v);
      b.type = "button";
      b.dataset.key = key; b.dataset.val = v;
      b.setAttribute("aria-pressed", String(state[key] === v));
      b.addEventListener("click", function () {
        state[key] = v;
        filters.querySelectorAll('[data-key="' + key + '"]').forEach(function (c) {
          c.setAttribute("aria-pressed", String(c.dataset.val === v));
        });
        render();
      });
      filters.appendChild(b);
    });
  }
  chipGroup("Sell in", "dst", dests);
  chipGroup("Risk", "risk", Array.from(new Set(rows.map(function (r) { return r.risk; }))).sort());

  // Table
  var cols = [
    ["rank", "#"], ["item", "Item"], ["route", "Route"], ["buy", "Buy"], ["sell", "Sell"],
    ["profit", "Profit/ea"], ["roi", "ROI"], ["qty", "Qty"], ["total_cost", "Cost"],
    ["total_profit", "Trip profit"], ["volume", "Sold/day"], ["data_age_h", "Age"]
  ];
  var head = document.getElementById("head");
  cols.forEach(function (c) {
    var th = el("th");
    var b = el("button", null, c[1]);
    b.type = "button";
    b.dataset.sort = c[0];
    b.addEventListener("click", function () {
      if (state.sort === c[0]) state.dir = state.dir === "desc" ? "asc" : "desc";
      else { state.sort = c[0]; state.dir = (c[0] === "rank" || c[0] === "data_age_h" ||
                                             c[0] === "item" || c[0] === "route") ? "asc" : "desc"; }
      render();
    });
    th.appendChild(b);
    head.appendChild(th);
  });

  function key(r, k) {
    if (k === "item") return (r.name || r.item).toLowerCase();
    if (k === "route") return r.src + r.dst;
    return r[k] == null ? -Infinity : r[k];
  }
  function render() {
    head.querySelectorAll("button").forEach(function (b) {
      if (b.dataset.sort === state.sort) b.dataset.dir = state.dir; else delete b.dataset.dir;
    });
    var list = rows.filter(function (r) {
      return (state.dst === "all" || r.dst === state.dst) && (state.risk === "all" || r.risk === state.risk);
    }).sort(function (a, b) {
      var x = key(a, state.sort), y = key(b, state.sort);
      var d = x < y ? -1 : x > y ? 1 : 0;
      return state.dir === "asc" ? d : -d;
    });
    var body = document.getElementById("rows");
    body.textContent = "";
    list.forEach(function (r) {
      var tr = el("tr");
      tr.appendChild(el("td", "rank", String(r.rank)));
      var it = el("td", "item", r.name || r.item);
      if (r.name) it.appendChild(el("span", "id", r.item));
      tr.appendChild(it);
      var rt = el("td", "route");
      rt.appendChild(document.createTextNode(r.src));
      rt.appendChild(el("span", "arrow", "→"));
      rt.appendChild(document.createTextNode(r.dst + (r.how === "listed" ? " (list)" : "")));
      rt.appendChild(el("span", "pill " + r.risk, r.risk));
      tr.appendChild(rt);
      tr.appendChild(el("td", null, nf.format(r.buy)));
      tr.appendChild(el("td", null, nf.format(r.sell)));
      tr.appendChild(el("td", "profit", nf.format(r.profit)));
      tr.appendChild(el("td", null, r.roi.toFixed(0) + "%"));
      tr.appendChild(el("td", null, nf.format(r.qty)));
      tr.appendChild(el("td", null, silver(r.total_cost)));
      tr.appendChild(el("td", "total", silver(r.total_profit)));
      tr.appendChild(el("td", null, r.volume == null ? "–" : nf.format(Math.round(r.volume))));
      var age = r.data_age_h;
      tr.appendChild(el("td", age <= 2 ? "age-ok" : age <= 6 ? "age-warn" : "age-old", age.toFixed(1) + "h"));
      body.appendChild(tr);
    });
    var empty = document.getElementById("empty");
    empty.hidden = list.length > 0;
    if (!list.length) {
      empty.textContent = "";
      empty.appendChild(el("strong", null, rows.length ? "No flips match these filters" : "No flips this scan"));
      empty.appendChild(document.createTextNode(rows.length
        ? "Pick “All” above to see every route."
        : "Market data is thin for these settings. Rescan with a longer max age or a lower minimum profit."));
    }
  }
  render();
})();
</script>
"""


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="Albion Online flip scanner (MVP)")
    p.add_argument("--server", choices=HOSTS, default="americas")
    p.add_argument("--mode", choices=["both", "royal", "bm"], default="both",
                   help="royal = city<->city, bm = Royal->Black Market")
    p.add_argument("--items", help="comma-separated item ids (skips auto list)")
    p.add_argument("--items-file",
                   help="file of item ids, comma/whitespace separated (skips auto list)")
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
    p.add_argument("--html", help="also write a results page to this file")
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

    if args.items or args.items_file:
        items = []
        if args.items:
            items += [i.strip() for i in args.items.split(",") if i.strip()]
        if args.items_file:
            items += read_items_file(args.items_file)
        items = list(dict.fromkeys(items))
    else:
        tiers = {int(t) for t in args.tiers.split(",")}
        if names:
            items = auto_items(names, tiers)
        else:
            print("Using the built-in gear list instead of items.txt.",
                  file=sys.stderr)
            items = builtin_items(tiers)
        if args.limit_items:
            items = items[:args.limit_items]
    if not items:
        sys.exit("No items to scan.")
    if not names:
        names = {i: builtin_name(i) for i in items if builtin_name(i)}
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
    meta = {
        "generated": now.isoformat(), "server": args.server,
        "settings": {
            "items": len(items),
            "tiers": "custom" if (args.items or args.items_file) else args.tiers,
            "tax": round(cfg["tax"] * 100, 1), "max_age": args.max_age,
            "min_profit": args.min_profit, "min_roi": args.min_roi,
            "max_roi": args.max_roi, "budget": args.budget,
            "quality": args.quality, "sell_mode": args.sell_mode,
        },
    }
    if not cands:
        write_csv(args.csv, [], names)
        if args.html:
            write_html(args.html, [], names, meta)
        print(f"No flips passed the filters (max age {args.max_age}h, min profit "
              f"{args.min_profit:,}, ROI {args.min_roi:g}-{args.max_roi:g}%).\n"
              "Try e.g. --max-age 24 --min-profit 1000, or scan more items/tiers.\n"
              f"Wrote an empty {args.csv}.")
        return []

    # Pre-rank, then pull volume only for the leaders (saves API calls)
    cands.sort(key=lambda c: c["profit"], reverse=True)
    shortlist = cands[: max(args.top * 4, 60)]
    volume = {}
    if not args.no_volume:
        print("Checking sales volume for shortlist...", file=sys.stderr)
        volume = fetch_volume(host, {(c["item"], c["dst"]) for c in shortlist},
                              args.quality)
        # No sales history at the destination means we can't tell whether the
        # items would actually sell, so don't suggest buying them.
        known = [c for c in shortlist if (c["item"], c["dst"]) in volume]
        if len(known) < len(shortlist):
            print(f"  skipped {len(shortlist) - len(known)} flips with no sales "
                  "history", file=sys.stderr)
        shortlist = known
    sized = [size_position(c, cfg, volume) for c in shortlist]
    sized = [c for c in sized if c["qty"] > 0]

    key = {"total": "total_profit", "profit": "profit", "roi": "roi"}[args.sort]
    sized.sort(key=lambda c: c[key], reverse=True)
    top = sized[: args.top]

    print()
    print_table(top, names)
    write_csv(args.csv, top, names)
    if args.html:
        write_html(args.html, top, names, meta)
        print(f"Wrote results page to {args.html}")
    print(f"\nSaved {len(top)} rows to {args.csv}")
    print("Reminders: confirm prices in-game, check zone danger on your route, "
          "and don't carry more than your risk allocation.")
    return top


if __name__ == "__main__":
    main()
