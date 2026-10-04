"""Replay recorded streams and measure how often any cycle beats the fee hurdle.

    python analyze_recording.py --bookticker bt.jsonl.gz [--depth d20.jsonl.gz]

bookTicker replay: after every top-of-book update, re-scores only the cycles that
touch the updated symbol, then reports, per per-leg fee level, how often and for
how long the best cycle is net-positive, and how much size sits at the touch.
depth20 replay: for net-positive snapshots, runs the depth-aware sizer to get
the profit-maximising size and profit.
"""
import argparse
import gzip
import json
import math
import statistics
from collections import Counter, defaultdict

from arb_core import SYMBOLS, Book, enumerate_cycles, size_cycle, top_rate

FEES_BPS = (0.0, 1.0, 2.0, 5.0, 7.5, 10.0)


def load(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        try:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    return  # partial last line of an interrupted (or still running) recording
                yield rec["t"], rec["m"]["stream"], rec["m"]["data"]
        except EOFError:
            return  # truncated gzip stream, same cause


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


def usdt_prices(mid):
    return {"USDT": 1.0, "USDC": mid["USDCUSDT"], "BTC": mid["BTCUSDT"],
            "ETH": mid["ETHUSDT"], "BNB": mid["BNBUSDT"]}


def touch_capacity_usdt(cycle, tob, qty, px):
    """Smallest top-of-book size across the legs, in USDT (ignores the rate drift along the cycle)."""
    caps = []
    for leg in cycle.legs:
        bid, ask = tob[leg.symbol]
        bid_qty, ask_qty = qty[leg.symbol]
        cap_src = bid_qty if leg.side == "SELL" else ask * ask_qty  # in leg.src units
        caps.append(cap_src * px[leg.src])
    return min(caps)


def replay_bookticker(path, cycles):
    by_symbol = defaultdict(list)
    for i, c in enumerate(cycles):
        for s in c.symbols:
            by_symbol[s].append(i)
    idx_by_len = defaultdict(list)
    for i, c in enumerate(cycles):
        idx_by_len[len(c.legs)].append(i)
    fee_log = {f: math.log1p(-f / 1e4) for f in FEES_BPS}

    tob, qty, msgs, spread_bps = {}, {}, Counter(), defaultdict(list)
    per_second = defaultdict(Counter)
    edge = [0.0] * len(cycles)
    cyc_max = [-math.inf] * len(cycles)
    cyc_pos_ns = [0] * len(cycles)
    open_ep = {f: None for f in FEES_BPS}
    episodes = {f: [] for f in FEES_BPS}
    pos_ns = Counter()
    t0 = t_prev = None
    ready = False

    for t, _, d in load(path):
        s = d["s"]
        msgs[s] += 1
        per_second[t // 1_000_000_000][s] += 1
        bid, ask = float(d["b"]), float(d["a"])
        tob[s], qty[s] = (bid, ask), (float(d["B"]), float(d["A"]))
        spread_bps[s].append((ask - bid) / ((ask + bid) / 2) * 1e4)
        if not ready:
            if len(tob) < len(SYMBOLS):
                continue
            ready, t0, t_prev = True, t, t
            dirty = range(len(cycles))
        else:
            dirty = by_symbol[s]
        dt = t - t_prev
        # Time-weight the state that held since the previous event.
        for i in range(len(cycles)):
            if edge[i] > 0:
                cyc_pos_ns[i] += dt
        for f in FEES_BPS:
            if open_ep[f] is not None:
                pos_ns[f] += dt
        for i in dirty:
            e = sum(math.log(top_rate(leg, *tob[leg.symbol])) for leg in cycles[i].legs)
            edge[i] = e
            cyc_max[i] = max(cyc_max[i], e)
        best = {L: max((edge[i], i) for i in idx) for L, idx in idx_by_len.items()}
        mid = {k: (b + a) / 2 for k, (b, a) in tob.items()}
        for f in FEES_BPS:
            net, i = max((e + L * fee_log[f], i) for L, (e, i) in best.items())
            ep = open_ep[f]
            if net > 0:
                if ep is None:
                    ep = open_ep[f] = {"start": t, "peak": -1.0, "opened_by": s}
                if net > ep["peak"]:
                    ep.update(peak=net, cycle=cycles[i].path,
                              cap=touch_capacity_usdt(cycles[i], tob, qty, usdt_prices(mid)))
            elif ep is not None:
                ep["ms"] = (t - ep["start"]) / 1e6
                ep["closed_by"] = s
                episodes[f].append(ep)
                open_ep[f] = None
        t_prev = t

    for f, ep in open_ep.items():  # close episodes still open when the recording ends
        if ep is not None:
            ep["ms"] = (t_prev - ep["start"]) / 1e6
            ep["closed_by"] = "(end of recording)"
            episodes[f].append(ep)
    span_s = (t_prev - t0) / 1e9
    return dict(span_s=span_s, msgs=msgs, per_second=per_second, spread_bps=spread_bps,
                cyc_max=cyc_max, cyc_pos_ns=cyc_pos_ns, episodes=episodes, pos_ns=pos_ns)


def replay_depth(path, cycles):
    by_symbol = defaultdict(list)
    for i, c in enumerate(cycles):
        for s in c.symbols:
            by_symbol[s].append(i)
    books, gross, snaps = {}, [0.0] * len(cycles), 0
    hits = {f: [] for f in FEES_BPS}
    for _, stream, d in load(path):
        sym = stream.split("@")[0].upper()
        books[sym] = Book(bids=[(float(p), float(q)) for p, q in d["bids"]],
                          asks=[(float(p), float(q)) for p, q in d["asks"]])
        if len(books) < len(SYMBOLS):
            continue
        dirty = by_symbol[sym] if snaps else range(len(cycles))
        snaps += 1
        tob = {k: (b.bids[0][0], b.asks[0][0]) for k, b in books.items()}
        for i in dirty:
            gross[i] = sum(math.log(top_rate(leg, *tob[leg.symbol])) for leg in cycles[i].legs)
        px = usdt_prices({k: (b + a) / 2 for k, (b, a) in tob.items()})
        for f in FEES_BPS:
            fee, best = f / 1e4, None
            for i, c in enumerate(cycles):
                if gross[i] + len(c.legs) * math.log1p(-fee) <= 0:
                    continue  # cheap top-of-book screen first; size only the survivors
                sz = size_cycle(c, books, fee)
                start_px = px[c.legs[0].src]
                cand = (sz.profit * start_px, sz.start_in * start_px, c.path)
                best = max(best, cand) if best else cand
            if best:
                hits[f].append(best)
    return dict(snaps=snaps, hits=hits)


def report(bt, depth, cycles):
    print(f"## bookTicker replay: {bt['span_s']:.0f} s, {sum(bt['msgs'].values())} updates\n")
    print("| symbol | updates/s (mean) | updates/s (busiest second) | median spread (bps) |")
    print("|---|---:|---:|---:|")
    for s in SYMBOLS:
        peak = max(c[s] for c in bt["per_second"].values())
        print(f"| {s} | {bt['msgs'][s] / bt['span_s']:.1f} | {peak} | {statistics.median(bt['spread_bps'][s]):.2f} |")

    print("\n| fee per leg (bps) | 3-leg hurdle (bps) | net-positive episodes | per hour | % of time net-positive "
          "| median / p90 / max duration (ms) | median peak edge (bps) | median touch size (USDT) "
          "| best cycle had 3 / 4 / 5 legs |")
    print("|---:|---:|---:|---:|---:|---|---:|---:|---|")
    for f in FEES_BPS:
        eps = bt["episodes"][f]
        durs = [e["ms"] for e in eps]
        peaks = [e["peak"] * 1e4 for e in eps]
        caps = [e["cap"] for e in eps]
        frac = bt["pos_ns"][f] / (bt["span_s"] * 1e9) * 100
        if eps:
            dur = f"{pct(durs, .5):.0f} / {pct(durs, .9):.0f} / {max(durs):.0f}"
            legs = Counter(e["cycle"].count("->") for e in eps)
            extra = (f"{statistics.median(peaks):.2f} | {statistics.median(caps):,.0f} "
                     f"| {legs[3]} / {legs[4]} / {legs[5]}")
        else:
            dur, extra = "-", "- | - | -"
        print(f"| {f:g} | {3 * f:g} | {len(eps)} | {len(eps) / bt['span_s'] * 3600:.0f} | {frac:.3f} | {dur} | {extra} |")

    for f in FEES_BPS:
        eps = bt["episodes"][f]
        if f == 0 or not eps:
            continue  # at zero fees some cycle is positive almost all the time
        print(f"\nWhich update opened and closed the net-positive episodes at {f:g} bps per leg:\n")
        print("| opened by | closed by | episodes | median duration (ms) |")
        print("|---|---|---:|---:|")
        pairs = defaultdict(list)
        for e in eps:
            pairs[(e["opened_by"], e["closed_by"])].append(e["ms"])
        for (o, c), ms in sorted(pairs.items(), key=lambda kv: -len(kv[1]))[:6]:
            print(f"| {o} | {c} | {len(ms)} | {statistics.median(ms):.0f} |")

    print("\nCycles that came closest (gross edge at the touch, before fees):\n")
    print("| cycle | legs | max gross edge (bps) | % of time gross > 0 |")
    print("|---|---:|---:|---:|")
    order = sorted(range(len(cycles)), key=lambda i: -bt["cyc_max"][i])
    for i in order[:12]:
        print(f"| {cycles[i].path} | {len(cycles[i].legs)} | {bt['cyc_max'][i] * 1e4:.2f} "
              f"| {bt['cyc_pos_ns'][i] / (bt['span_s'] * 1e9) * 100:.3f} |")

    if depth:
        print(f"\n## depth20@100ms replay: {depth['snaps']} combined snapshots\n")
        print("| fee per leg (bps) | snapshots with a net-positive cycle | median / p90 / max optimal size (USDT) "
              "| median / p90 / max profit (USDT) |")
        print("|---:|---:|---|---|")
        for f in FEES_BPS:
            h = depth["hits"][f]
            if not h:
                print(f"| {f:g} | 0 | - | - |")
                continue
            prof, size = [x[0] for x in h], [x[1] for x in h]
            print(f"| {f:g} | {len(h)} ({len(h) / depth['snaps'] * 100:.2f}%) "
                  f"| {pct(size, .5):,.0f} / {pct(size, .9):,.0f} / {max(size):,.0f} "
                  f"| {pct(prof, .5):.3f} / {pct(prof, .9):.3f} / {max(prof):.3f} |")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bookticker", required=True)
    ap.add_argument("--depth")
    args = ap.parse_args()
    cycles = enumerate_cycles()
    bt = replay_bookticker(args.bookticker, cycles)
    depth = replay_depth(args.depth, cycles) if args.depth else None
    report(bt, depth, cycles)
