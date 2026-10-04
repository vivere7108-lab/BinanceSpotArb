"""Compare fair-value estimators for market making: which source of truth works best?

    python compare_fair_values.py --bookticker bt.jsonl.gz --trades aggtrades.jsonl.gz

Both recordings must be made at the same time. Estimators, all from top-of-book mids:

  own        the pair's own mid: the baseline every maker already has
  routes     median of the pair's three two-leg routes through the other assets (the plan's v1)
  usdt       USDT as the source of truth: every asset priced off its USDT book, so
             fair = p(base) / p(quote). For a USDT pair this is its own mid.
  routes-vw  the three routes averaged, each weighted by the traded notional of its thinner leg
  all-vw     routes-vw plus the pair's own mid, weighted by the pair's own traded notional

Two tests:

  1. Gap closure. Every second, take the gap between an estimator and the pair's own mid. How
     much of it has the mid closed 10 s later (regression slope; 1 = fully)? A pair that moves
     towards an estimator is reading its price from it.
  2. Filtering. A maker quotes only when its price is at least N bps better than fair value
     20 ms before the trade. Report the share of real fill notional it keeps, and the realized
     spread of those fills 10 s later. The P&L of a fill depends on how its inventory is
     unwound, so it is marked two ways:
       usdt  exit through the USDT books (or perps priced off them): value 10 s later in USDT
             terms. This matches the plan, which offloads inventory through the deep books.
       own   exit on the same book: the pair's own mid 10 s later (classic realized spread).

Volume weights come from the trades in the same recording.
"""
import argparse
import bisect
import math
import statistics
from collections import defaultdict

from analyze_recording import load
from arb_core import ASSETS, SYMBOLS, make_leg

ESTIMATORS = ("own", "routes", "usdt", "routes-vw", "all-vw")
LEGS = {(a, b): make_leg(a, b) for a in ASSETS for b in ASSETS if a != b}
PRE_NS = 20_000_000  # fair value 20 ms before a trade (the two recordings arrive on separate connections)
MARK_S = 10  # the gap-closure test uses the same 10 s horizon
THRESHOLDS = (0.0, 1.0, 2.0)


def route_rates(mid, base, quote):
    """{intermediate asset: implied base/quote rate} for the three two-leg routes, at mids."""
    def rate(src, dst):
        leg = LEGS[src, dst]
        return mid[leg.symbol] if leg.side == "SELL" else 1.0 / mid[leg.symbol]
    return {x: rate(base, x) * rate(x, quote) for x in ASSETS if x not in (base, quote)}


def estimates(tob, vol, symbols=SYMBOLS):
    """{symbol: {estimator: fair value}}."""
    mid = {s: (b + a) / 2 for s, (b, a) in tob.items()}
    p_usdt = {"USDT": 1.0, "USDC": mid["USDCUSDT"], "BTC": mid["BTCUSDT"], "ETH": mid["ETHUSDT"], "BNB": mid["BNBUSDT"]}
    out = {}
    for s in symbols:
        base, quote = SYMBOLS[s]
        routes = route_rates(mid, base, quote)
        w = {x: min(vol[LEGS[base, x].symbol], vol[LEGS[x, quote].symbol]) for x in routes}
        vw_num, vw_den = sum(w[x] * r for x, r in routes.items()), sum(w.values())
        median = statistics.median(routes.values())
        out[s] = {
            "own": mid[s],
            "routes": median,
            "usdt": p_usdt[base] / p_usdt[quote],
            "routes-vw": vw_num / vw_den if vw_den else median,
            "all-vw": (vw_num + vol[s] * mid[s]) / (vw_den + vol[s]) if vw_den + vol[s] else mid[s],
        }
    return out


def load_trades(path):
    trades = [(t, d["s"], float(d["p"]), float(d["q"]), 1.0 if d["m"] else -1.0) for t, _, d in load(path)]
    trades.sort()
    # Traded notional per symbol in USDT (BTC- and ETH-quoted pairs converted at median trade prices).
    px = {q: statistics.median(p for _, s, p, _, _ in trades if s == q + "USDT") for q in ("BTC", "ETH")}
    px.update(USDT=1.0, USDC=1.0)
    vol = defaultdict(float)  # a symbol with no trades gets weight 0
    for _, s, p, q, _ in trades:
        vol[s] += p * q * px[SYMBOLS[s][1]]
    return trades, vol, px


def run(book_path, trades_path):
    trades, vol, px = load_trades(trades_path)
    # Queries answered during one sweep over the book, each with the book as it stood at that time.
    queries = []  # (time, kind, ref)
    for i, (t, *_) in enumerate(trades):
        queries.append((t - PRE_NS, "signal", i))
        queries.append((t + MARK_S * 1_000_000_000, "mark", i))
    t0, t1 = trades[0][0], trades[-1][0]
    grid = list(range(t0 + 1_000_000_000, t1 - 10_000_000_000, 1_000_000_000))
    for g, tg in enumerate(grid):
        queries.append((tg, "grid", g))
        queries.append((tg + 1_000_000_000, "grid+1", g))
        queries.append((tg + 10_000_000_000, "grid+10", g))
    queries.sort()

    signal, mark, at_grid, mid_later = {}, {}, {}, defaultdict(dict)
    tob, qi = {}, 0

    def answer(upto):
        nonlocal qi
        while qi < len(queries) and queries[qi][0] < upto:
            _, kind, ref = queries[qi]
            qi += 1
            if len(tob) < len(SYMBOLS):
                continue
            if kind == "signal":
                s = trades[ref][1]
                signal[ref] = estimates(tob, vol, (s,))[s]
            elif kind == "mark":
                s = trades[ref][1]
                mark[ref] = estimates(tob, vol, (s,))[s]
            elif kind == "grid":
                at_grid[ref] = estimates(tob, vol)
            else:
                mid_later[kind][ref] = {s: (b + a) / 2 for s, (b, a) in tob.items()}

    for t, _, d in load(book_path):
        answer(t)
        tob[d["s"]] = (float(d["b"]), float(d["a"]))
    return trades, vol, px, signal, mark, at_grid, mid_later


def gap_closure(at_grid, mid_later, s, e):
    """Slope and correlation of the own mid's move over 10 s on the gap to estimator e."""
    gaps, moves = [], []
    for g, est in at_grid.items():
        later = mid_later["grid+10"].get(g)
        if later is None:
            continue
        own = est[s]["own"]
        gaps.append(est[s][e] / own - 1)
        moves.append(later[s] / own - 1)
    vg = statistics.pvariance(gaps)
    if vg < 1e-18:
        return None
    cov = statistics.fmean(g * m for g, m in zip(gaps, moves)) - statistics.fmean(gaps) * statistics.fmean(moves)
    return cov / vg, cov / math.sqrt(vg * statistics.pvariance(moves))


def filtering(trades, px, signal, mark, mark_by):
    """{symbol: {key: [notional, pnl]}} with keys "all" and (estimator, threshold)."""
    agg = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for i, (t, s, p, q, side) in enumerate(trades):
        if i not in signal or i not in mark:
            continue
        w = p * q * px[SYMBOLS[s][1]]
        realized = side * (mark[i][mark_by] / p - 1) * 1e4
        agg[s]["all"][0] += w
        agg[s]["all"][1] += realized * w
        for e in ESTIMATORS:
            edge = side * (signal[i][e] / p - 1) * 1e4
            for n in THRESHOLDS:
                if edge >= n:
                    agg[s][e, n][0] += w
                    agg[s][e, n][1] += realized * w
    return agg


def report(trades, vol, px, signal, mark, at_grid, mid_later):
    print("Traded notional used for weights (USDT): "
          + ", ".join(f"{s} {vol.get(s, 0) / 1e6:.2f}M" for s in SYMBOLS) + "\n")

    graph = [e for e in ESTIMATORS if e != "own"]
    print("## 1. Gap closure: share of the gap to each estimator the pair's mid closes in 10 s (slope, correlation)\n")
    print("| pair | " + " | ".join(graph) + " |")
    print("|---|" + "---:|" * len(graph))
    for s in SYMBOLS:
        cells = []
        for e in graph:
            r = gap_closure(at_grid, mid_later, s, e)
            cells.append("– (same as own)" if r is None else f"{r[0]:.2f} ({r[1]:+.2f})")
        print(f"| {s} | " + " | ".join(cells) + " |")

    cross = ("ETHBTC", "BNBBTC", "BNBETH")
    for mark_by in ("usdt", "own"):
        agg = filtering(trades, px, signal, mark, mark_by)
        for n in THRESHOLDS:
            print(f"\n## 2. Filtering, marked to {mark_by} 10 s later: quote only when at least {n:g} bps better "
                  f"than fair (kept share / realized spread, bps)\n")
            print("| pair | all fills | " + " | ".join(ESTIMATORS) + " |")
            print("|---|---:|" + "---:|" * len(ESTIMATORS))
            tot = {k: defaultdict(float) for k in ("cross", "all")}
            for s in SYMBOLS:
                a = agg[s]
                if not a["all"][0]:
                    continue
                cells = []
                for e in ESTIMATORS:
                    kept, pnl = a[e, n]
                    for k in ("cross", "all"):
                        if k == "all" or s in cross:
                            tot[k][e] += pnl / 1e4
                    cells.append(f"{kept / a['all'][0] * 100:.0f}% / {pnl / kept:+.2f}" if kept else "0% / –")
                for k in ("cross", "all"):
                    if k == "all" or s in cross:
                        tot[k]["all"] += a["all"][1] / 1e4
                print(f"| {s} | {a['all'][1] / a['all'][0]:+.2f} | " + " | ".join(cells) + " |")
            for k, label in (("cross", "cross pairs"), ("all", "all pairs")):
                print(f"| **P&L of kept fills, {label} (USDT)** | {tot[k]['all']:+,.0f} | "
                      + " | ".join(f"{tot[k][e]:+,.0f}" for e in ESTIMATORS) + " |")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bookticker", required=True)
    ap.add_argument("--trades", required=True)
    args = ap.parse_args()
    report(*run(args.bookticker, args.trades))
