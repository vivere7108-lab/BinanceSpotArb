"""Replay recorded streams and measure how often any cycle beats the fee hurdle.

    python analyze_recording.py --bookticker bt.jsonl.gz [--depth d20.jsonl.gz]
        [--fees "7.5,USDCUSDT=0,BTCUSDC=7.125,ETHUSDC=7.125,BNBUSDC=7.125"] [--packet-usdt 100]

bookTicker replay: after every top-of-book update, re-scores only the cycles that
touch the updated symbol. It then reports how often, and for how long, the best
cycle is net-positive. It does this for a grid of flat per-leg fees and, with
--fees, for your own per-pair taker schedule. The first --fees value is the
default rate in bps, followed by SYMBOL=bps overrides.
depth20 replay: for net-positive snapshots, runs the depth-aware sizer (capped at
--packet-usdt if given) to get the profit-maximising size and profit.
"""
import argparse
import gzip
import json
import math
import statistics
from collections import Counter, defaultdict

from arb_core import SYMBOLS, Book, enumerate_cycles, hurdle, size_cycle, top_rate

FEES_BPS = (0.0, 1.0, 2.0, 5.0, 7.5, 10.0)
NEAR_BPS = 5.0  # "within this many bps of breakeven", for the closest-cycles table


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


def parse_fees(spec):
    """'7.5,USDCUSDT=0,BTCUSDC=7.125' -> {symbol: fraction} covering every symbol."""
    default, *overrides = spec.split(",")
    fees = {s: float(default) / 1e4 for s in SYMBOLS}
    for item in overrides:
        sym, bps = (x.strip() for x in item.split("="))
        if sym.upper() not in SYMBOLS:
            raise SystemExit(f"--fees: unknown symbol {sym}")
        fees[sym.upper()] = float(bps) / 1e4
    return fees


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


def usdt_prices(tob):
    mid = {k: (b + a) / 2 for k, (b, a) in tob.items()}
    return {"USDT": 1.0, "USDC": mid["USDCUSDT"], "BTC": mid["BTCUSDT"],
            "ETH": mid["ETHUSDT"], "BNB": mid["BNBUSDT"]}


def touch_capacity_usdt(cycle, tob, qty):
    """Smallest top-of-book size across the legs, in USDT (ignores the rate drift along the cycle)."""
    px, caps = usdt_prices(tob), []
    for leg in cycle.legs:
        bid, ask = tob[leg.symbol]
        bid_qty, ask_qty = qty[leg.symbol]
        cap_src = bid_qty if leg.side == "SELL" else ask * ask_qty  # in leg.src units
        caps.append(cap_src * px[leg.src])
    return min(caps)


def gross_edge(cycle, tob):
    return sum(math.log(top_rate(leg, *tob[leg.symbol])) for leg in cycle.legs)


class Episodes:
    """Contiguous stretches during which the best net edge is above zero."""

    def __init__(self):
        self.open, self.done, self.pos_ns = None, [], 0

    def step(self, t, dt, net, cycle, capacity, symbol):
        if self.open is not None:  # time-weight the state that held since the previous event
            self.pos_ns += dt
        if net > 0:
            if self.open is None:
                self.open = {"start": t, "peak": -1.0, "opened_by": symbol}
            if net > self.open["peak"]:
                self.open.update(peak=net, cycle=cycle.path, cap=capacity(cycle))
        elif self.open is not None:
            self.close(t, symbol)

    def close(self, t, symbol):
        self.open.update(ms=(t - self.open["start"]) / 1e6, closed_by=symbol)
        self.done.append(self.open)
        self.open = None


def replay_bookticker(path, cycles, schedule=None):
    n = len(cycles)
    by_symbol, idx_by_len = defaultdict(list), defaultdict(list)
    for i, c in enumerate(cycles):
        idx_by_len[len(c.legs)].append(i)
        for s in c.symbols:
            by_symbol[s].append(i)
    fee_log = {f: math.log1p(-f / 1e4) for f in FEES_BPS}
    sched_h = [hurdle(c, schedule) for c in cycles] if schedule else None
    near = NEAR_BPS / 1e4

    tob, qty, msgs, spread_bps = {}, {}, Counter(), defaultdict(list)
    per_second = defaultdict(Counter)
    edge = [0.0] * n
    cyc_max, cyc_pos_ns, cyc_near_ns = [-math.inf] * n, [0] * n, [0] * n
    grid = {f: Episodes() for f in FEES_BPS}
    sched = Episodes()
    t0 = t_prev = None

    def capacity(c):
        return touch_capacity_usdt(c, tob, qty)

    for t, _, d in load(path):
        s = d["s"]
        msgs[s] += 1
        per_second[t // 1_000_000_000][s] += 1
        bid, ask = float(d["b"]), float(d["a"])
        tob[s], qty[s] = (bid, ask), (float(d["B"]), float(d["A"]))
        spread_bps[s].append((ask - bid) / ((ask + bid) / 2) * 1e4)
        if t0 is None:
            if len(tob) < len(SYMBOLS):
                continue
            t0 = t_prev = t
            dirty = range(n)
        else:
            dirty = by_symbol[s]
        dt = t - t_prev
        for i in range(n):  # time-weight the edges that held since the previous event
            if edge[i] > 0:
                cyc_pos_ns[i] += dt
            if sched_h and edge[i] - sched_h[i] > -near:
                cyc_near_ns[i] += dt
        for i in dirty:
            edge[i] = gross_edge(cycles[i], tob)
            cyc_max[i] = max(cyc_max[i], edge[i])
        best = {L: max((edge[i], i) for i in idx) for L, idx in idx_by_len.items()}
        for f in FEES_BPS:
            net, i = max((e + L * fee_log[f], i) for L, (e, i) in best.items())
            grid[f].step(t, dt, net, cycles[i], capacity, s)
        if sched_h:
            net, i = max((edge[j] - sched_h[j], j) for j in range(n))
            sched.step(t, dt, net, cycles[i], capacity, s)
        t_prev = t

    for ep in (*grid.values(), sched):
        if ep.open is not None:
            ep.close(t_prev, "(end of recording)")
    return dict(span_s=(t_prev - t0) / 1e9, msgs=msgs, per_second=per_second, spread_bps=spread_bps,
                cyc_max=cyc_max, cyc_pos_ns=cyc_pos_ns, cyc_near_ns=cyc_near_ns,
                grid=grid, sched=sched if schedule else None, sched_h=sched_h)


def replay_depth(path, cycles, schedule=None, packet_usdt=None):
    by_symbol = defaultdict(list)
    for i, c in enumerate(cycles):
        for s in c.symbols:
            by_symbol[s].append(i)
    scenarios = {f: f / 1e4 for f in FEES_BPS}
    if schedule:
        scenarios["schedule"] = schedule
    hurdles = {k: [hurdle(c, fee) for c in cycles] for k, fee in scenarios.items()}
    books, gross, snaps = {}, [0.0] * len(cycles), 0
    hits = {k: [] for k in scenarios}
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
            gross[i] = gross_edge(cycles[i], tob)
        px = usdt_prices(tob)
        for k, fee in scenarios.items():
            best = None
            for i, c in enumerate(cycles):
                if gross[i] <= hurdles[k][i]:
                    continue  # cheap top-of-book screen first; size only the survivors
                start_px = px[c.legs[0].src]
                cap = packet_usdt / start_px if packet_usdt else math.inf
                sz = size_cycle(c, books, fee, max_in=cap)
                cand = (sz.profit * start_px, sz.start_in * start_px, c.path)
                best = max(best, cand) if best else cand
            if best:
                hits[k].append(best)
    return dict(snaps=snaps, hits=hits)


def episode_row(eps, span_s):
    durs = [e["ms"] for e in eps.done]
    frac = eps.pos_ns / (span_s * 1e9) * 100
    if not durs:
        return f"0 | 0 | {frac:.3f} | - | - | - | -"
    legs = Counter(e["cycle"].count("->") for e in eps.done)
    return (f"{len(durs)} | {len(durs) / span_s * 3600:.0f} | {frac:.3f} "
            f"| {pct(durs, .5):.0f} / {pct(durs, .9):.0f} / {max(durs):.0f} "
            f"| {statistics.median(e['peak'] * 1e4 for e in eps.done):.2f} "
            f"| {statistics.median(e['cap'] for e in eps.done):,.0f} | {legs[3]} / {legs[4]} / {legs[5]}")


def attribution_table(title, eps):
    print(f"\n{title}\n")
    print("| opened by | closed by | episodes | median duration (ms) |")
    print("|---|---|---:|---:|")
    pairs = defaultdict(list)
    for e in eps.done:
        pairs[(e["opened_by"], e["closed_by"])].append(e["ms"])
    for (o, c), ms in sorted(pairs.items(), key=lambda kv: -len(kv[1]))[:6]:
        print(f"| {o} | {c} | {len(ms)} | {statistics.median(ms):.0f} |")


def depth_row(label, h, snaps):
    if not h:
        return f"| {label} | 0 | - | - |"
    prof, size = [x[0] for x in h], [x[1] for x in h]
    return (f"| {label} | {len(h)} ({len(h) / snaps * 100:.2f}%) "
            f"| {pct(size, .5):,.0f} / {pct(size, .9):,.0f} / {max(size):,.0f} "
            f"| {pct(prof, .5):.3f} / {pct(prof, .9):.3f} / {max(prof):.3f} |")


EPISODE_HDR = ("| net-positive episodes | per hour | % of time net-positive | median / p90 / max duration (ms) "
               "| median peak edge (bps) | median touch size (USDT) | best cycle had 3 / 4 / 5 legs |")


def report(bt, depth, cycles, packet_usdt):
    span = bt["span_s"]
    print(f"## bookTicker replay: {span:.0f} s, {sum(bt['msgs'].values())} updates\n")
    print("| symbol | updates/s (mean) | updates/s (busiest second) | median spread (bps) |")
    print("|---|---:|---:|---:|")
    for s in SYMBOLS:
        peak = max(c[s] for c in bt["per_second"].values())
        print(f"| {s} | {bt['msgs'][s] / span:.1f} | {peak} | {statistics.median(bt['spread_bps'][s]):.2f} |")

    print("\n| fee per leg (bps) | 3-leg hurdle (bps) " + EPISODE_HDR)
    print("|---:|---:|---:|---:|---:|---|---:|---:|---|")
    for f in FEES_BPS:
        print(f"| {f:g} | {3 * f:g} | {episode_row(bt['grid'][f], span)} |")
    for f in FEES_BPS:
        if f > 0 and bt["grid"][f].done:  # at zero fees some cycle is positive almost all the time
            attribution_table(f"Which update opened and closed the net-positive episodes at {f:g} bps per leg:",
                              bt["grid"][f])

    print("\nCycles that came closest (gross edge at the touch, before fees):\n")
    print("| cycle | legs | max gross edge (bps) | % of time gross > 0 |")
    print("|---|---:|---:|---:|")
    for i in sorted(range(len(cycles)), key=lambda i: -bt["cyc_max"][i])[:12]:
        print(f"| {cycles[i].path} | {len(cycles[i].legs)} | {bt['cyc_max'][i] * 1e4:.2f} "
              f"| {bt['cyc_pos_ns'][i] / (span * 1e9) * 100:.3f} |")

    if bt["sched"]:
        h = bt["sched_h"]
        print("\n## Your fee schedule\n")
        print("| hurdle (bps) | cycles | example |")
        print("|---:|---:|---|")
        fams = defaultdict(list)
        for i, c in enumerate(cycles):
            fams[round(h[i] * 1e4, 2)].append(c)
        for v in sorted(fams)[:8]:
            print(f"| {v:.2f} | {len(fams[v])} | {fams[v][0].path} |")
        print(f"\n{EPISODE_HDR}")
        print("|---:|---:|---:|---|---:|---:|---|")
        print(f"| {episode_row(bt['sched'], span)} |")
        if bt["sched"].done:
            attribution_table("Which update opened and closed them:", bt["sched"])
        print("\nClosest cycles net of your fees (max net edge at the touch):\n")
        print(f"| cycle | legs | hurdle (bps) | max gross (bps) | max net (bps) "
              f"| % of time within {NEAR_BPS:g} bps of breakeven |")
        print("|---|---:|---:|---:|---:|---:|")
        for i in sorted(range(len(cycles)), key=lambda i: -(bt["cyc_max"][i] - h[i]))[:10]:
            print(f"| {cycles[i].path} | {len(cycles[i].legs)} | {h[i] * 1e4:.2f} | {bt['cyc_max'][i] * 1e4:.2f} "
                  f"| {(bt['cyc_max'][i] - h[i]) * 1e4:.2f} | {bt['cyc_near_ns'][i] / (span * 1e9) * 100:.2f} |")

    if depth:
        cap = f", sizes capped at {packet_usdt:,.0f} USDT" if packet_usdt else ", no packet cap"
        print(f"\n## depth20@100ms replay: {depth['snaps']} combined snapshots{cap}\n")
        print("| fee per leg (bps) | snapshots with a net-positive cycle | median / p90 / max optimal size (USDT) "
              "| median / p90 / max profit (USDT) |")
        print("|---:|---:|---|---|")
        for f in FEES_BPS:
            print(depth_row(f"{f:g}", depth["hits"][f], depth["snaps"]))
        if "schedule" in depth["hits"]:
            print(depth_row("your schedule", depth["hits"]["schedule"], depth["snaps"]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bookticker", required=True)
    ap.add_argument("--depth")
    ap.add_argument("--fees", help='per-pair taker fees in bps: "DEFAULT,SYMBOL=BPS,..."')
    ap.add_argument("--packet-usdt", type=float, help="cap each sized cycle at this many USDT")
    args = ap.parse_args()
    cycles = enumerate_cycles()
    schedule = parse_fees(args.fees) if args.fees else None
    bt = replay_bookticker(args.bookticker, cycles, schedule)
    depth = replay_depth(args.depth, cycles, schedule, args.packet_usdt) if args.depth else None
    report(bt, depth, cycles, args.packet_usdt)
