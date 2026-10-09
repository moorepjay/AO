# Albion Flip Scanner

A small command-line tool that scans [Albion Online Data Project](https://www.albion-online-data.com/)
market prices for profitable flips:

- **Royal city → Royal city** arbitrage (including Caerleon)
- **Royal city → Black Market** (Caerleon)

It uses only the Python standard library (Python 3.8+), so there's nothing to install.

> Market data is crowd-sourced by players running the Albion Data Client, so
> rows can be stale or wrong. Always check a flip in-game before you spend silver on it.

## Quick start

```bash
python albion_flip_scanner.py                        # auto item list, T4–T6 gear, Americas
python albion_flip_scanner.py --tiers 5,6 --top 30
python albion_flip_scanner.py --items T4_BAG,T5_BAG,T6_BAG --premium
python albion_flip_scanner.py --mode bm --budget 38617746
python albion_flip_scanner.py --items-file my_items.txt     # your own item list
python albion_flip_scanner.py --html flips.html             # also write a results page
python albion_flip_scanner.py --limit-items 50 --no-volume   # fast test run
```

Or install it as a command:

```bash
pip install .
albion-flip-scanner --server europe --premium
```

The results print as a table and are also saved to `flips.csv` (change this with `--csv`).

## How it works

1. **Item list:** downloads `items.txt` from [ao-bin-dumps](https://github.com/ao-data/ao-bin-dumps)
   (via `raw.githubusercontent.com`) and picks the gear items (weapons, off-hands, armor, capes, bags)
   for the tiers you ask for. A successful download is cached in `~/.cache/albion-flip-scanner/items.txt`
   and used whenever the download fails. With no download and no cache, the scanner falls back to a
   built-in list of common gear and shows item ids instead of names.
   Pass `--items` or `--items-file` to scan your own list instead.
2. **Prices:** calls `/api/v2/stats/prices` in batches, keeping each URL under the API's 4096-character limit.
3. **Evaluation:** for every route it buys at the source's cheapest sell order and sells at the destination either:
   - **instant:** into the highest buy order (sales tax only), or
   - **listed:** as a new sell order at the current lowest price (sales tax + setup fee; undercutting isn't modelled). The Black Market only supports instant sales.

   Prices older than `--max-age` hours are ignored. Flips are dropped when they fall below `--min-profit` or `--min-roi`, or rise above `--max-roi` (very large margins are usually bad data).
4. **Volume:** for the top candidates it pulls the last 7 days of daily history (`/api/v2/stats/history`) to estimate how many items sell per day at the destination.
   Flips with no sales history at the destination are dropped, since there's no evidence they would sell.
5. **Sizing:** quantity = `budget × risk fraction ÷ buy price`, capped at `--vol-share` of daily volume. Routes touching Caerleon or the Black Market count as **HIGH** risk (`--risk-frac`); everything else is **MED** (`--med-frac`).

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--server` | `americas` | `americas`, `europe` or `asia` |
| `--mode` | `both` | `royal` (city↔city), `bm` (Royal→Black Market), or `both` |
| `--items` | — | comma-separated item ids (skips the auto list) |
| `--items-file` | — | file of item ids, separated by commas or whitespace; `#` starts a comment |
| `--tiers` | `4,5,6` | tiers for the auto list |
| `--limit-items` | `0` | cap the auto item count (0 = no cap) |
| `--quality` | `1` | item quality 1–5 |
| `--premium` | off | use the 4% premium tax rate instead of 8% |
| `--budget` | `38617746` | total silver available |
| `--risk-frac` | `0.25` | max share of budget on one HIGH-risk position |
| `--med-frac` | `0.5` | max share of budget on one MED-risk position |
| `--max-age` | `6` | ignore price rows older than this many hours |
| `--min-profit` | `5000` | minimum profit per item |
| `--min-roi` / `--max-roi` | `5` / `200` | ROI % window |
| `--sell-mode` | `instant` | `instant` or `listed` for Royal-city sales |
| `--vol-share` | `0.25` | max share of average daily volume to move in one trip |
| `--top` | `25` | rows to show |
| `--sort` | `total` | `total` (total profit), `profit` (per item), or `roi` |
| `--no-volume` | off | skip the history lookups (faster, but no volume cap) |
| `--html` | — | also write a self-contained results page (sortable, filterable) |
| `--csv` | `flips.csv` | output file (written with just a header when nothing passes the filters) |

If nothing passes the filters, the data is probably thin for your tiers: try
`--max-age 24 --min-profit 1000`, or add more tiers.

## Craft planner (leather)

`albion_craft_planner.py` is for gatherers who run the loop
**gather and bank (Caerleon) → refine (Martlock, hide bonus) → craft jackets (Thetford, leather
armor bonus) → sell**. For every tier and enchant of hide (4.0 to 8.3) it works out what one
gathered hide is worth if you:

- sell it raw where it's banked,
- take it to Martlock, refine it and sell the leather, or
- refine it in Martlock and craft a Mercenary, Hunter or Assassin jacket in Thetford, with and
  without focus, then sell the jacket.

It recommends the best option and where to sell it (crafting with focus only when it pays at least
10% more, since focus is limited), and turns that into a run sheet: which hides to sell at home,
which to haul to Martlock, which leather to sell and where, which jackets to craft in Thetford, and
where the jackets sell (including hauling back to the Black Market). It also lists a jacket craft
queue ranked by how much more each jacket earns than selling its leather, with daily sales so you
don't flood a market.

```bash
python albion_craft_planner.py                         # T4-T8, enchants 0-3
python albion_craft_planner.py --tiers 5,6 --premium
python albion_craft_planner.py --home Lymhurst         # hides banked somewhere else
python albion_craft_planner.py --html plan.html        # also write a results page
```

Hides are valued at what you could sell them for where they're banked. Leather is only bought and
sold in royal cities (it's made in Martlock and never carried back to Caerleon); the lower-tier leather each
refine needs is priced at the cheapest sell order. Sell orders are valued at the lower of the
current listing and the 7-day average sale price, and only where the item has actually been
selling. Where a market is trading but has no current price snapshot (common in Caerleon), the
7-day average sale price is used and labelled. Return rates are settings: `--refine-rrr` (Martlock,
default 36.7% without focus), `--craft-rrr` and `--craft-rrr-focus` (Thetford, 24.8% and 47.9%).
Station fees and above-Normal item quality aren't counted.

## Fees

The tax and setup fee are constants at the top of `albion_flip_scanner.py`
(`TAX_PREMIUM`, `TAX_NO_PREMIUM`, `SETUP_FEE`). Check them against the in-game
market window, since the game can change them.

## Development

The tests run offline and don't need the network:

```bash
python -m unittest discover -s tests -v
```

GitHub Actions runs them on Python 3.8, 3.10, 3.12 and 3.13.
