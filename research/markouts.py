"""Realized spread of passive fills on any pairs: against their own mid and a USDT anchor.

    python markouts.py --bookticker bt.jsonl.gz --trades aggtrades.jsonl.gz --maker-bps 7.5

For every trade in the aggTrade recording, the passive side is credited the move from the
trade price to a fair value 1 s, 10 s and 60 s later, notional-weighted. "At fill" uses the
fair value 20 ms before the trade. A maker on the pair earns roughly the +10 s or +60 s
figure minus its maker fee per fill, before queue effects. A wide spread is an edge only if
that stays positive.

Two fair values are used:

* The pair's own mid. For a pair outside the 5-asset graph that is often all there is.
* The USDT anchor, for pairs not quoted in USDT whose coin and quote asset both have USDT
  books in the bookTicker recording. For ZECBTC it is ZECUSDT mid / BTCUSDT mid, as for the
  cross pairs (PLAN.md §13.1). The second table keeps only the fills at least N bps better
  than the anchor 20 ms before the trade, i.e. those of a maker who quotes only when the
  anchor says the price is good, and marks them to the anchor 60 s later.
"""
import argparse
import bisect
import math
from collections import defaultdict

from analyze_recording import load

HORIZONS_S = (1, 10, 60)
PRE_NS = 20_000_000
QUOTES = ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB")
STABLES = ("USDT", "USDC", "FDUSD")
THRESHOLDS_BPS = (0, 5, 10)


def split(symbol):
    """'ZECBTC' -> ('ZEC', 'BTC'), or None if the quote asset is not one of QUOTES."""
    for q in QUOTES:
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    return None


def at(series, t):
    """Last value of a (times, values) series at or before t, or None."""
    times, values = series
    i = bisect.bisect_right(times, t) - 1
    return values[i] if i >= 0 else None


def main(book_path, trades_path, maker_bps):
    mids = defaultdict(lambda: ([], []))
    for t, _, d in load(book_path):
        times, values = mids[d["s"]]
        times.append(t)
        values.append((float(d["b"]) + float(d["a"])) / 2)

    def usdt(asset, t):
        """USDT per unit of asset: its USDT mid if recorded, 1 for a stablecoin without one, else None."""
        if asset + "USDT" in mids:
            return at(mids[asset + "USDT"], t)
        return 1.0 if asset in STABLES else None

    def anchor(symbol, t):
        """(fair value in quote units, quote asset in USDT) from the USDT books, or None."""
        pair = split(symbol)
        if pair is None or pair[1] == "USDT" or any(a + "USDT" not in mids for a in pair):
            return None
        base, quote = (usdt(a, t) for a in pair)
        return (base / quote, quote) if base and quote else None

    res = defaultdict(lambda: {"n": 0, "w": 0.0, "usdt": 0.0, **{h: 0.0 for h in (0, *HORIZONS_S)}})
    anc = defaultdict(lambda: defaultdict(float))
    for t, _, d in load(trades_path):
        times, values = mids[d["s"]]
        i = bisect.bisect_right(times, t - PRE_NS) - 1
        if i < 0 or t + HORIZONS_S[-1] * 1_000_000_000 > times[-1]:
            continue
        p, w = float(d["p"]), float(d["p"]) * float(d["q"])
        side = 1.0 if d["m"] else -1.0  # buyer is maker -> the passive side bought
        pair = split(d["s"])
        rate = usdt(pair[1], t) if pair else None
        r = res[d["s"]]
        r["n"] += 1
        r["w"] += w
        r["usdt"] += w * rate if rate else math.nan
        r[0] += side * (values[i] / p - 1) * 1e4 * w
        for h in HORIZONS_S:
            j = bisect.bisect_right(times, t + h * 1_000_000_000) - 1
            r[h] += side * (values[j] / p - 1) * 1e4 * w

        before, after = anchor(d["s"], t - PRE_NS), anchor(d["s"], t + 60_000_000_000)
        if before and after:
            w_usdt = w * before[1]
            edge = side * (before[0] / p - 1) * 1e4
            pnl = side * (after[0] / p - 1) * 1e4 * w_usdt
            a = anc[d["s"]]
            a["n"] += 1
            a["w"] += w_usdt
            a["all"] += pnl
            for n in THRESHOLDS_BPS:
                if edge >= n:
                    a["w", n] += w_usdt
                    a["pnl", n] += pnl

    print(f"## Realized spread vs own mid (notional-weighted, bps); maker fee {maker_bps:g} bps per fill\n")
    print("| pair | trades | notional (USDT) | at fill | +1 s | +10 s | +60 s | +60 s after maker fee |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    total = defaultdict(float)
    for s, r in sorted(res.items(), key=lambda kv: -kv[1][60] / kv[1]["w"]):
        w = r["w"]
        cells = " | ".join(f"{r[h] / w:+.1f}" for h in (0, *HORIZONS_S))
        notional = "n/a" if math.isnan(r["usdt"]) else f"{r['usdt']:,.0f}"
        print(f"| {s} | {r['n']:,} | {notional} | {cells} | {r[60] / w - maker_bps:+.1f} |")
        if not math.isnan(r["usdt"]):  # pairs averaged by USDT notional
            total["n"] += r["n"]
            total["usdt"] += r["usdt"]
            for h in (0, *HORIZONS_S):
                total[h] += r[h] / w * r["usdt"]
    if total["usdt"]:
        u = total["usdt"]
        cells = " | ".join(f"{total[h] / u:+.1f}" for h in (0, *HORIZONS_S))
        print(f"| **all pairs** | {total['n']:,.0f} | {u:,.0f} | {cells} | {total[60] / u - maker_bps:+.1f} |")
    if not anc:
        return

    print("\n## Realized spread at +60 s vs the USDT anchor (notional-weighted, bps, before the maker fee)\n")
    print("Filtered columns: share of notional kept / its realized spread.\n")
    print("| pair | trades | notional (USDT) | all fills | " + " | ".join(f"≥ {n} bps better than anchor"
                                                                 for n in THRESHOLDS_BPS) + " |")
    print("|---|---:|---:|---:|" + "---:|" * len(THRESHOLDS_BPS))
    total = defaultdict(float)
    for s, a in sorted(anc.items(), key=lambda kv: -kv[1]["w"]):
        for k, v in a.items():
            total[k] += v
        print(f"| {s} | {a['n']:,.0f} | {a['w']:,.0f} | {a['all'] / a['w']:+.1f} | " + " | ".join(
            f"{a['w', n] / a['w']:.0%} / {a['pnl', n] / a['w', n]:+.1f}" if a["w", n] else "–"
            for n in THRESHOLDS_BPS) + " |")
    for label, fee in (("all pairs", 0.0), ("all pairs, after maker fee", maker_bps)):
        print(f"| **{label}** | {total['n']:,.0f} | {total['w']:,.0f} | {total['all'] / total['w'] - fee:+.1f} | "
              + " | ".join(f"{total['w', n] / total['w']:.0%} / {total['pnl', n] / total['w', n] - fee:+.1f}"
                           if total["w", n] else "–" for n in THRESHOLDS_BPS) + " |")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bookticker", required=True)
    ap.add_argument("--trades", required=True)
    ap.add_argument("--maker-bps", type=float, default=7.5)
    args = ap.parse_args()
    main(args.bookticker, args.trades, args.maker_bps)
