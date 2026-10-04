"""Scan every Binance spot pair for spreads a small maker could earn at retail fees.

    python scan_spreads.py --snapshots 6 --maker-bps 7.5

Takes a few top-of-book snapshots of all trading pairs (10 s apart) and their 24 h volume.
It keeps pairs whose median spread beats a maker round trip (2 x maker fee) and whose
daily volume is big enough to fill quotes but too small for large firms to bother with.
They are printed in two lists:

1. Anchored: books not quoted in USDT whose coin also trades on a USDT book with at least
   10x the volume and at most a third of the spread. That book gives the fair value to quote
   against, as for the cross pairs (PLAN.md §13.1): fair = coin/USDT mid / quote/USDT mid.
2. Everything else, which only has its own mid to go by.

Wide spreads usually come with toxic flow, so treat both lists as candidates for a markout
study (record bookTicker + aggTrade, run markouts.py), not as profits. The last lines list
the symbols to record for them (ops/recorders/spot-alts-*.env).
"""
import argparse
import json
import statistics
import time
import urllib.request

API = "https://data-api.binance.vision/api/v3"
QUOTES = ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB")


def get(path):
    with urllib.request.urlopen(f"{API}/{path}", timeout=30) as r:
        return json.loads(r.read())


def main(snapshots, maker_bps, min_vol, max_vol, top):
    info = {s["symbol"]: s for s in get("exchangeInfo?permissions=SPOT")["symbols"] if s["status"] == "TRADING"}
    spreads = {}
    for i in range(snapshots):
        for t in get("ticker/bookTicker"):
            bid, ask = float(t["bidPrice"]), float(t["askPrice"])
            if t["symbol"] in info and bid > 0 and ask > bid:
                spreads.setdefault(t["symbol"], []).append((ask - bid) / ((ask + bid) / 2) * 1e4)
        if i < snapshots - 1:
            time.sleep(10)
    day = {t["symbol"]: t for t in get("ticker/24hr")}
    usd = {"USDT": 1.0, "USDC": 1.0, "FDUSD": 1.0}
    for q in ("BTC", "ETH", "BNB"):
        usd[q] = float(day[q + "USDT"]["lastPrice"])

    med, vol = {}, {}
    for sym, sp in spreads.items():
        if info[sym]["quoteAsset"] in QUOTES and sym in day:
            med[sym] = statistics.median(sp)
            vol[sym] = float(day[sym]["quoteVolume"]) * usd[info[sym]["quoteAsset"]]

    def gross(sym):
        # $/day if the maker's fills were 1% of the pair's volume, each earning half the spread less a maker fee.
        return (med[sym] / 2 - maker_bps) / 1e4 * vol[sym] * 0.01

    wide = sorted((s for s in med if min_vol <= vol[s] <= max_vol and med[s] > 2 * maker_bps), key=gross, reverse=True)
    anchor = {}
    for s in wide:
        a = info[s]["baseAsset"] + "USDT"
        if info[s]["quoteAsset"] != "USDT" and a in med and vol[a] >= 10 * vol[s] and med[a] <= med[s] / 3:
            anchor[s] = a
    own = [s for s in wide if s not in anchor]

    print(f"{len(spreads)} trading pairs scanned; {len(wide)} have a median spread above {2 * maker_bps:g} bps "
          f"and ${min_vol / 1e6:g}M–${max_vol / 1e6:g}M of daily volume: {len(anchor)} with a USDT anchor, "
          f"{len(own)} without.\n")
    print("**With a USDT anchor**\n")
    print("| pair | median spread (bps) | 24 h volume (USD) | anchor | anchor spread (bps) | anchor volume (USD) "
          "| $/day at 1% of volume, before adverse selection |")
    print("|---|---:|---:|---|---:|---:|---:|")
    for s in list(anchor)[:top]:
        a = anchor[s]
        print(f"| {s} | {med[s]:.1f} | {vol[s]:,.0f} | {a} | {med[a]:.1f} | {vol[a]:,.0f} | {gross(s):,.2f} |")
    print("\n**Own mid only**\n")
    print("| pair | median spread (bps) | 24 h volume (USD) | spread left after 2 maker fees (bps) "
          "| $/day at 1% of volume, before adverse selection |")
    print("|---|---:|---:|---:|---:|")
    for s in own[:top]:
        print(f"| {s} | {med[s]:.1f} | {vol[s]:,.0f} | {med[s] - 2 * maker_bps:.1f} | {gross(s):,.2f} |")

    # Record each candidate's book and trades, plus the USDT books that price it (markouts.py uses
    # them for the anchored table whenever they exist).
    makes = list(anchor)[:top] + own[:top]
    books = set(makes)
    for s in makes:
        if info[s]["quoteAsset"] != "USDT":
            books |= {a + "USDT" for a in (info[s]["baseAsset"], info[s]["quoteAsset"]) if a + "USDT" in info}
    print("\nSymbols to record for these candidates (ops/recorders/spot-alts-*.env):\n")
    print("bookTicker:", ",".join(sorted(books)))
    print("aggTrade:  ", ",".join(sorted(makes)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshots", type=int, default=6)
    ap.add_argument("--maker-bps", type=float, default=7.5, help="your maker fee (VIP 0 with BNB = 7.5)")
    ap.add_argument("--min-vol", type=float, default=200_000, help="minimum 24 h volume, USD")
    ap.add_argument("--max-vol", type=float, default=20_000_000, help="maximum 24 h volume, USD")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    main(args.snapshots, args.maker_bps, args.min_vol, args.max_vol, args.top)
