"""Which pairs are worth making markets on, judged by the rest of the graph.

    python analyze_making.py --bookticker bt.jsonl.gz [--trades aggtrades.jsonl.gz] \\
        --fees "7.5,USDCUSDT=0,BTCUSDC=7.125,ETHUSDC=7.125,BNBUSDC=7.125"

Three views, each compared with your VIP 2 maker fee (6 bps), with 0 (spot
maker programme) and with -0.8 bps (a top programme rebate):

1. Hedged at once. A quote joins P's best bid (or ask), and every fill is
   flattened immediately through the best other path in the graph (taker fees
   from --fees). Edge = hedge rate vs the quote, before P's maker fee.
2. Unhedged, against fair value. By default fair value is read from the USDT
   books: every asset priced off its USDT book, fair = p(base) / p(quote). That
   is the estimator compare_fair_values.py found best. `--fair routes` uses the
   median of P's three two-leg routes instead. Edge of a bid at the touch =
   fair / bid - 1; of an ask = ask / fair - 1.
3. Realized spread (needs --trades, an aggTrade recording made alongside). For
   every real trade, the passive side's P&L against fair value 1 s, 10 s and
   60 s later. This is what a maker at the touch actually earned, adverse
   selection included, so it is the number that decides a pair. A second table
   keeps only fills where the quote was already at least N bps better than
   fair value just before the trade. That is roughly what a maker who re-quotes
   off the graph's fair value would still have been filled on.
"""
import argparse
import bisect
import statistics
from collections import Counter, defaultdict

from analyze_recording import load, parse_fees
from arb_core import ASSETS, SYMBOLS, best_path_rate, make_leg

MAKER_BPS = (("VIP 2 maker 6", 6.0), ("maker 0", 0.0), ("rebate -0.8", -0.8))
HORIZONS_S = (1, 10, 60)


def path_name(legs):
    return "->".join([legs[0].src] + [leg.dst for leg in legs])


LEGS = {(a, b): make_leg(a, b) for a in ASSETS for b in ASSETS if a != b}


def route_fair_values(tob):
    """Graph-implied mid of every symbol: median of its three two-leg routes, at mids."""
    mid = {s: (b + a) / 2 for s, (b, a) in tob.items()}

    def rate(src, dst):
        leg = LEGS[src, dst]
        return mid[leg.symbol] if leg.side == "SELL" else 1.0 / mid[leg.symbol]

    return {s: statistics.median(rate(base, x) * rate(x, quote) for x in ASSETS if x not in (base, quote))
            for s, (base, quote) in SYMBOLS.items()}


def usdt_fair_values(tob):
    """USDT as the source of truth: every asset priced off its USDT book, fair = p(base) / p(quote)."""
    mid = {s: (b + a) / 2 for s, (b, a) in tob.items()}
    p = {"USDT": 1.0, "USDC": mid["USDCUSDT"], "BTC": mid["BTCUSDT"], "ETH": mid["ETHUSDT"], "BNB": mid["BNBUSDT"]}
    return {s: p[base] / p[quote] for s, (base, quote) in SYMBOLS.items()}


FAIR_VALUES = {"usdt": usdt_fair_values, "routes": route_fair_values}


def share_above(values, bps):
    return sum(v > bps for v in values) / len(values) * 100


def sample(tob, taker, k, out, fair_fn):
    """Record every symbol's edges at one time-grid point, k times (k grid points)."""
    fair = fair_fn(tob)
    for s, (base, quote) in SYMBOLS.items():
        bid, ask = tob[s]
        h, legs = best_path_rate(base, quote, tob, taker, exclude=s)  # flatten a bid fill: sell the base
        out["hedged"][s, "bid"].extend([(h / bid - 1) * 1e4] * k)
        out["paths"][s, "bid"][path_name(legs)] += k
        g, legs = best_path_rate(quote, base, tob, taker, exclude=s)  # flatten an ask fill: buy it back
        out["hedged"][s, "ask"].extend([(ask * g - 1) * 1e4] * k)
        out["paths"][s, "ask"][path_name(legs)] += k
        out["fair"][s, "bid"].extend([(fair[s] / bid - 1) * 1e4] * k)
        out["fair"][s, "ask"].extend([(ask / fair[s] - 1) * 1e4] * k)
        out["spread"][s].extend([(ask - bid) / ((ask + bid) / 2) * 1e4] * k)


def replay(path, taker, sample_ms, fair_fn):
    out = {"hedged": defaultdict(list), "fair": defaultdict(list), "spread": defaultdict(list),
           "paths": defaultdict(Counter), "fair_series": defaultdict(lambda: ([], []))}
    tob, step, next_t = {}, sample_ms * 1_000_000, None
    for t, _, d in load(path):
        if len(tob) == len(SYMBOLS):
            # Grid points up to this update see the book as it was before it.
            if next_t is None:
                next_t = t
            k = 0
            while next_t <= t:
                k, next_t = k + 1, next_t + step
            if k:
                sample(tob, taker, k, out, fair_fn)
        tob[d["s"]] = (float(d["b"]), float(d["a"]))
        if len(tob) == len(SYMBOLS):
            for s, v in fair_fn(tob).items():  # fair value after every update, for markouts
                times, values = out["fair_series"][s]
                times.append(t)
                values.append(v)
    return out


def markouts(trades_path, fair_series, pre_ms=20, thresholds=(0.0, 1.0, 2.0)):
    """Notional-weighted P&L (bps) of the passive side of every trade, vs fair value later on.

    "at fill" uses fair value ``pre_ms`` before the trade (the two recordings arrive on
    separate connections). Fills are also split by that edge at fill, per threshold.
    """
    res = defaultdict(lambda: {"n": 0, "notional": 0.0, **{h: 0.0 for h in (0, *HORIZONS_S)},
                               "kept": {m: [0.0, 0.0] for m in thresholds}})  # m -> [notional, P&L at +10 s]
    pre = pre_ms * 1_000_000
    for t, _, d in load(trades_path):
        s = d["s"]
        times, values = fair_series[s]
        i = bisect.bisect_right(times, t - pre) - 1
        if i < 0 or not times or t + HORIZONS_S[-1] * 1_000_000_000 > times[-1]:
            continue  # need fair value before the trade and at the longest horizon
        price, qty = float(d["p"]), float(d["q"])
        side = 1.0 if d["m"] else -1.0  # buyer is maker -> the passive side bought
        w = price * qty
        at_fill = side * (values[i] / price - 1) * 1e4
        later = {}
        for h in HORIZONS_S:
            j = bisect.bisect_right(times, t + h * 1_000_000_000) - 1
            later[h] = side * (values[j] / price - 1) * 1e4
        r = res[s]
        r["n"] += 1
        r["notional"] += w
        r[0] += at_fill * w
        for h in HORIZONS_S:
            r[h] += later[h] * w
        for m in thresholds:
            if at_fill >= m:
                r["kept"][m][0] += w
                r["kept"][m][1] += later[10] * w
    return res


def requotes_per_minute(times, values, bps):
    """How often fair value drifts ``bps`` from the last quoted level (each time = re-quote both sides)."""
    ref, n = values[0], 0
    for v in values:
        if abs(v / ref - 1) * 1e4 >= bps:
            ref, n = v, n + 1
    return n / ((times[-1] - times[0]) / 60e9)


def report(out, sample_ms, marks, quote_usdt):
    n = len(out["spread"]["BTCUSDT"])
    print(f"## 1. Making at the touch, each fill hedged at once through the graph ({n} samples, every {sample_ms} ms)\n")
    print("| pair | side | usual hedge path | median / p95 / max edge (bps) | "
          + " | ".join(f"% of time edge > {label}" for label, _ in MAKER_BPS) + " |")
    print("|---|---|---|---|" + "---:|" * len(MAKER_BPS))
    for s, side in sorted(out["hedged"], key=lambda k: -statistics.median(out["hedged"][k])):
        e = sorted(out["hedged"][s, side])
        shares = " | ".join(f"{share_above(e, m):.1f}" for _, m in MAKER_BPS)
        print(f"| {s} | {side} | {out['paths'][s, side].most_common(1)[0][0]} "
              f"| {statistics.median(e):.1f} / {e[len(e) * 19 // 20]:.1f} / {e[-1]:.1f} | {shares} |")

    print("\n## 2. Unhedged quote at the touch vs graph fair value (before adverse selection)\n")
    print("| pair | median spread (bps) | side | median / p95 edge vs fair (bps) | "
          + " | ".join(f"% of time edge > {label}" for label, _ in MAKER_BPS) + " |")
    print("|---|---:|---|---|" + "---:|" * len(MAKER_BPS))
    for s in sorted(SYMBOLS, key=lambda s: -statistics.median(out["spread"][s])):
        for side in ("bid", "ask"):
            e = sorted(out["fair"][s, side])
            shares = " | ".join(f"{share_above(e, m):.1f}" for _, m in MAKER_BPS)
            print(f"| {s} | {statistics.median(out['spread'][s]):.2f} | {side} "
                  f"| {statistics.median(e):.2f} / {e[len(e) * 19 // 20]:.2f} | {shares} |")

    print("\n## Re-quote load: fair-value moves per minute that would force a re-quote (2 new orders each)\n")
    print("| pair | moves of 0.5 bps / min | 1 bps / min | 2 bps / min |")
    print("|---|---:|---:|---:|")
    for s in SYMBOLS:
        times, values = out["fair_series"][s]
        print(f"| {s} | " + " | ".join(f"{requotes_per_minute(times, values, b):.1f}" for b in (0.5, 1, 2)) + " |")

    if marks is not None:
        order = sorted(marks, key=lambda s: -marks[s][10] / max(marks[s]["notional"], 1e-12))
        print("\n## 3. Realized spread of real passive fills vs graph fair value (notional-weighted, bps)\n")
        print("| pair | trades | notional (USDT) | at fill | +1 s | +10 s | +60 s |")
        print("|---|---:|---:|---:|---:|---:|---:|")
        for s in order:
            r = marks[s]
            w = r["notional"]
            print(f"| {s} | {r['n']:,} | {w * quote_usdt[s]:,.0f} | {r[0] / w:.2f} | "
                  + " | ".join(f"{r[h] / w:.2f}" for h in HORIZONS_S) + " |")
        thresholds = list(next(iter(marks.values()))["kept"])
        print("\nOnly fills where the quote was already at least N bps better than fair value "
              "(share of fill notional kept, and realized spread at +10 s):\n")
        print("| pair | " + " | ".join(f"N = {m:g}: kept / +10 s" for m in thresholds) + " |")
        print("|---|" + "---:|" * len(thresholds))
        for s in order:
            r = marks[s]
            cells = []
            for m in thresholds:
                kept, pnl = r["kept"][m]
                cells.append(f"{kept / r['notional'] * 100:.0f}% / {pnl / kept:.2f}" if kept else "0% / -")
            print(f"| {s} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bookticker", required=True)
    ap.add_argument("--trades", help="aggTrade recording made at the same time as --bookticker")
    ap.add_argument("--fees", required=True, help='per-pair taker fees in bps: "DEFAULT,SYMBOL=BPS,..."')
    ap.add_argument("--sample-ms", type=int, default=100)
    ap.add_argument("--fair", choices=sorted(FAIR_VALUES), default="usdt", help="fair-value estimator (default usdt)")
    args = ap.parse_args()
    out = replay(args.bookticker, parse_fees(args.fees), args.sample_ms, FAIR_VALUES[args.fair])
    marks = markouts(args.trades, out["fair_series"]) if args.trades else None
    # USDT value of one unit of each symbol's quote asset, from the last fair values.
    last = {s: v[-1] for s, (_, v) in out["fair_series"].items()}
    px = {"USDT": 1.0, "USDC": last["USDCUSDT"], "BTC": last["BTCUSDT"], "ETH": last["ETHUSDT"]}
    report(out, args.sample_ms, marks, {s: px[q] for s, (_, q) in SYMBOLS.items()})
