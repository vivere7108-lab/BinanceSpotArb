#!/usr/bin/env python3
"""Synthetic min-size maker orders at the touch, replayed against L1 + trade data.

Replays the paper's "continuous quoting" experiment (Albers et al., Sec. 2): one buy at the top bid and
one sell at the top ask at all times. An order is cancelled and reposted when its side's top price
improves, and reposted after it fills. Queue position can't be observed in L1/L2 data, so each run picks
a queue exponent k: a share (ahead / (ahead + behind)) ** k of each cancellation at our level is taken
from orders ahead of us. Named settings (any number also works, e.g. --models 2,4):

    pess  k = inf  cancels come from behind us first   (lower bound on fills)
    prop  k = 1    cancels split pro rata ahead/behind
    opt   k = 0    cancels come from ahead of us first (upper bound on fills)

Fill rules for a buy at price p (sells are mirrored by negating prices):
    taker sell below p                      -> filled (it swept through our level)
    taker sell at p for more than queue ahead -> filled; otherwise queue ahead shrinks by its size
    best ask <= p                           -> filled (a crossing limit order matched us)
    best bid drops below p without a sweep  -> we'd be alone at the top; the next taker sell fills us
    best bid rises above p                  -> cancelled; repost at the new top
Same-millisecond ordering: trades, then book updates, then our own actions.

Latency: we act on an event lat_md ms after it, and the order rests lat_ord ms after that. An order that
would cross on arrival is rejected (post-only); one that arrives below a top that has moved up is
cancelled at once. Both are reposted. With --poll-ms N the bot instead only looks every N ms: an order
whose level drops below the top keeps its place until the next check, and reposts wait for a check.

With --size-lots N, orders are N lots; volume that reaches our place in the queue fills them partially.

Output, one row per order: results/sim/<SYMBOL>/<model>-L<latency>ms[-P<poll>ms][-S<lots>]/<date>.parquet

Usage:
    .venv/bin/python sim/queue_sim.py --symbol BTCUSDT --start 2024-02-12 --end 2024-02-18 \\
        --models pess,prop,opt --latency-ms 10 --poll-ms 0,5000
"""

import argparse
import glob
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
from numba import njit

# Queue exponent k: a cancel at our level was ahead of us with share (ahead / (ahead + behind)) ** k.
MODELS = {"pess": np.inf, "prop": 1.0, "opt": 0.0}
REASONS = ["start", "after_fill", "after_cancel", "after_stale", "after_reject"]
STATUSES = ["cancel", "fill", "reject", "end"]
FILL_KINDS = ["none", "trade", "sweep", "cross", "alone"]

R_START, R_FILL, R_CANCEL, R_STALE, R_REJECT = 0, 1, 2, 3, 4
S_CANCEL, S_FILL, S_REJECT, S_END = 0, 1, 2, 3
K_NONE, K_TRADE, K_SWEEP, K_CROSS, K_ALONE = 0, 1, 2, 3, 4
IDLE, PENDING, RESTING = 0, 1, 2
INF = np.iinfo(np.int64).max


@njit(cache=True)
def simulate_side(bt, bnp, bnq, bop, boq, tt, tp, tq, qexp, lat_md, lat_ord, poll_ms, size, cap):
    """Event loop for one side in its own price frame (higher near price = better for us).

    Book arrays: time, near price, near qty, opposite price, opposite qty (ticks / lots).
    Trade arrays: taker trades that hit our side only.
    qexp: queue exponent k (0 = optimistic, 1 = pro rata, inf = pessimistic), see MODELS.
    poll_ms: 0 reacts to every event; >0 models a bot that only checks its order every poll_ms.
    size: order size in lots. Taker volume that reaches our place in the queue fills us up to the
    remaining size; an order is "fill" once complete, otherwise it ends "cancel" with qty_filled >= 0.
    Returns the order count (or -1 if cap was too small) and per-order arrays.
    """
    o_tdec = np.zeros(cap, np.int64)
    o_tpost = np.zeros(cap, np.int64)
    o_tend = np.zeros(cap, np.int64)
    o_px = np.zeros(cap, np.int64)
    o_reason = np.zeros(cap, np.int8)
    o_status = np.zeros(cap, np.int8)
    o_kind = np.zeros(cap, np.int8)
    o_np_post = np.zeros(cap, np.int64)
    o_nq_post = np.zeros(cap, np.int64)
    o_oq_post = np.zeros(cap, np.int64)
    o_la_post = np.zeros(cap, np.float64)
    o_la_fill = np.zeros(cap, np.float64)
    o_lb_fill = np.zeros(cap, np.float64)
    o_nq_fill = np.zeros(cap, np.int64)
    o_oq_fill = np.zeros(cap, np.int64)
    o_qfill = np.zeros(cap, np.int64)
    o_tfirst = np.zeros(cap, np.int64)
    o_qcross = np.zeros(cap, np.int64)   # lots filled by crossing limit orders (resting liquidity, not taker volume)

    nb, nt = bt.shape[0], tt.shape[0]
    ib, it, n = 0, 0, 0
    cur_np, cur_nq, cur_op, cur_oq = 0, 0, 0, 0
    state, reason = IDLE, R_START
    t_decide = bt[0] if nb > 0 else INF
    t_arrive, p_dec, t_last = INF, 0, 0
    # resting-order state
    k, p = -1, 0
    alone = False
    above, t_check = False, INF  # polling mode: our level is below the top, check pending
    la, lb = 0.0, 0.0           # visible quantity ahead of / behind us (lots)
    level_qty, traded = 0, 0    # level qty at last book update, and taker volume at p since then
    rem, cross_seen = 0, 0      # lots still unfilled; crossing qty already matched against us

    while True:
        t_b = bt[ib] if ib < nb else INF
        t_t = tt[it] if it < nt else INF
        t_s = t_decide if state == IDLE else (t_arrive if state == PENDING else t_check)
        if t_b == INF and t_t == INF and t_s > t_last:   # data exhausted; later actions can't be simulated
            break
        amt, kind, la_rec = 0, K_NONE, la

        if t_t <= t_b and t_t <= t_s:                     # 1) taker trade hitting our side
            t, px, qty = t_t, tp[it], tq[it]
            it += 1
            t_last = t
            if state == RESTING:
                if px < p or (px == p and alone):         # it went through our price: we'd be hit first
                    amt, kind = min(rem, qty), (K_ALONE if alone else K_SWEEP)
                elif px == p:
                    traded += qty
                    if qty > la:                          # reaches our place in the queue
                        amt, kind = min(rem, int(np.ceil(qty - la))), K_TRADE
                        la = 0.0
                    else:
                        la -= qty

        elif t_b <= t_s:                                  # 2) book update
            t = t_b
            cur_np, cur_nq, cur_op, cur_oq = bnp[ib], bnq[ib], bop[ib], boq[ib]
            ib += 1
            t_last = t
            if state == RESTING:
                if cur_np > p:                            # no longer at the top
                    cross_seen = 0
                    if poll_ms == 0:
                        o_status[k], o_tend[k] = S_CANCEL, t
                        state, reason, t_decide = IDLE, R_CANCEL, t + lat_md
                    elif not above:                       # a polling bot only notices at its next check
                        above, t_check = True, (t // poll_ms + 1) * poll_ms
                elif cur_op <= p:                         # crossing sellers would have matched us
                    if cur_oq > cross_seen:
                        amt, kind = min(rem, cur_oq - cross_seen), K_CROSS
                    cross_seen = cur_oq
                elif cur_np == p:
                    above, t_check, cross_seen = False, INF, 0
                    if alone:                             # orders reappeared at p: all behind us
                        alone, la, lb = False, 0.0, float(cur_nq)
                    else:
                        delta = cur_nq - (level_qty - traded)
                        if delta > 0:
                            lb += delta
                        elif delta < 0 and la + lb > 0.0:
                            # share of cancelled qty that was ahead of us = (la / (la + lb)) ** qexp
                            c = float(-delta)
                            if qexp == 0.0:
                                share = 1.0
                            elif qexp == np.inf:
                                share = 0.0 if lb > 0.0 else 1.0
                            else:
                                share = (la / (la + lb)) ** qexp
                            from_a = c * share
                            from_b = c - from_a
                            if from_b > lb:               # not enough behind us: the rest was ahead
                                from_a += from_b - lb
                                from_b = lb
                            if from_a > la:               # not enough ahead: the rest was behind
                                from_b = min(from_b + from_a - la, lb)
                                from_a = la
                            la -= from_a
                            lb -= from_b
                        if la > cur_nq:
                            la = float(cur_nq)
                        lb = cur_nq - la
                    level_qty, traded = cur_nq, 0
                else:                                     # level vanished without a sweep
                    alone, la, lb, level_qty, traded = True, 0.0, 0.0, 0, 0
                    above, t_check, cross_seen = False, INF, 0

        else:                                             # 3) our own scheduled action
            t = t_s
            t_last = t
            if state == IDLE:                             # decide: target the current top
                p_dec, state, t_arrive = cur_np, PENDING, t + lat_ord
                t_dec_pending = t
            elif state == RESTING:                        # polling check while our level is below the top
                t_check = INF
                if cur_np > p:
                    o_status[k], o_tend[k] = S_CANCEL, t
                    state, reason, t_decide = IDLE, R_CANCEL, t
                above = False
            else:                                         # order arrives at the exchange
                if n >= cap:
                    return -1, o_tdec, o_tpost, o_tend, o_px, o_reason, o_status, o_kind, o_np_post, \
                        o_nq_post, o_oq_post, o_la_post, o_la_fill, o_lb_fill, o_nq_fill, o_oq_fill, \
                        o_qfill, o_tfirst, o_qcross
                k = n
                n += 1
                o_tdec[k], o_tpost[k], o_px[k], o_reason[k] = t_dec_pending, t, p_dec, reason
                o_np_post[k], o_nq_post[k], o_oq_post[k] = cur_np, cur_nq, cur_oq
                retry = t if poll_ms == 0 else (t // poll_ms + 1) * poll_ms
                if cur_op <= p_dec:                       # would cross: post-only reject
                    o_status[k], o_tend[k] = S_REJECT, t
                    state, reason, t_decide = IDLE, R_REJECT, retry
                elif cur_np > p_dec:                      # top moved up while in flight
                    o_status[k], o_tend[k] = S_CANCEL, t
                    state, reason, t_decide = IDLE, R_STALE, retry
                else:
                    state, p, rem, cross_seen = RESTING, p_dec, size, 0
                    above, t_check = False, INF
                    if cur_np == p:
                        alone, la, lb = False, float(cur_nq), 0.0
                    else:
                        alone, la, lb = True, 0.0, 0.0
                    level_qty, traded = cur_nq, 0
                    o_la_post[k] = la

        if amt > 0:
            if o_qfill[k] == 0:                           # first fill: record where we were in the queue
                o_tfirst[k], o_kind[k] = t, kind
                o_la_fill[k], o_lb_fill[k] = la_rec, lb
                o_nq_fill[k], o_oq_fill[k] = cur_nq, cur_oq
            o_qfill[k] += amt
            if kind == K_CROSS:
                o_qcross[k] += amt
            rem -= amt
            if rem == 0:
                o_status[k], o_tend[k] = S_FILL, t
                state, reason = IDLE, R_FILL
                t_decide = t + lat_md if poll_ms == 0 else (t // poll_ms + 1) * poll_ms
                above, t_check = False, INF

    if state == RESTING:
        o_status[k], o_tend[k] = S_END, t_last
    return n, o_tdec, o_tpost, o_tend, o_px, o_reason, o_status, o_kind, o_np_post, \
        o_nq_post, o_oq_post, o_la_post, o_la_fill, o_lb_fill, o_nq_fill, o_oq_fill, o_qfill, o_tfirst, o_qcross


def load_day(symbol, day, source="archive", root="."):
    """One UTC day of book + trades in a common schema, from the archive or the recorder's output."""
    root = Path(root)
    if source == "archive":
        book = pl.read_parquet(root / f"archive/bookTicker/{symbol}/{day}.parquet")
        trades = pl.read_parquet(root / f"archive/aggTrade/{symbol}/{day}.parquet")
    else:
        book = pl.read_parquet(glob.glob(str(root / f"data/bookTicker/{symbol}/{day}/*.parquet")))
        trades = pl.read_parquet(glob.glob(str(root / f"data/aggTrade/{symbol}/{day}/*.parquet")))
        book = book.unique("update_id")
        trades = trades.unique("agg_id")
        if "qty_nonrpi" in trades.columns:  # RPI fills don't deplete the visible queue
            trades = trades.with_columns(qty=pl.coalesce("qty_nonrpi", "qty"))
    book = book.sort("trans_ms", "update_id").select(
        t="trans_ms", bid_px="bid_px", bid_qty="bid_qty", ask_px="ask_px", ask_qty="ask_qty")
    trades = trades.sort("agg_id").select(t="trade_ms", price="price", qty="qty", buyer_maker="buyer_maker")
    return book, trades


def side_arrays(book, trades, side, tick, lot):
    """Integer arrays for one side, in a frame where a higher near price is better for the order."""
    ticks = lambda c: (pl.col(c) / tick).round().cast(pl.Int64)
    lots = lambda c: (pl.col(c) / lot).round().cast(pl.Int64)
    near, opp = ("bid", "ask") if side == 1 else ("ask", "bid")
    b = book.select("t", np_=ticks(f"{near}_px") * side, nq=lots(f"{near}_qty"),
                    op=ticks(f"{opp}_px") * side, oq=lots(f"{opp}_qty"))
    # buyer_maker=True means the taker sold, hitting bids
    tr = trades.filter(pl.col("buyer_maker") == (side == 1)).select("t", px=ticks("price") * side, q=lots("qty"))
    return [b[c].to_numpy() for c in b.columns] + [tr[c].to_numpy() for c in tr.columns]


def run_side(arrs, qexp, lat_md, lat_ord, poll_ms=0, size=1):
    bt, bnp, bnq, bop, boq, tt, tp, tq = arrs
    cap = 2 * int(np.count_nonzero(np.diff(bnp))) + len(tt) + 10_000
    while True:
        out = simulate_side(bt, bnp, bnq, bop, boq, tt, tp, tq, qexp, lat_md, lat_ord, poll_ms, size, cap)
        if out[0] >= 0:
            return out
        cap *= 2


def orders_frame(out, side, book, tick, lot):
    n = out[0]
    (tdec, tpost, tend, px, reason, status, kind, np_post, nq_post, oq_post,
     la_post, la_fill, lb_fill, nq_fill, oq_fill, qfill, tfirst, qcross) = (a[:n] for a in out[1:])
    price = px * side * tick
    bt = book["t"].to_numpy()
    bid, ask = book["bid_px"].to_numpy(), book["ask_px"].to_numpy()
    bq, aq = book["bid_qty"].to_numpy(), book["ask_qty"].to_numpy()
    filled = status == S_FILL   # completely filled
    got = qfill > 0             # any fill; markouts and queue position are taken at the first fill

    def asof(t):  # index of the last book row at or before t
        return np.clip(np.searchsorted(bt, t, side="right") - 1, 0, len(bt) - 1)

    cols = {}
    for h in (1, 5):
        i = asof(tfirst + h * 1000)
        mid = (bid[i] + ask[i]) / 2
        micro = (bq[i] * ask[i] + aq[i] * bid[i]) / (bq[i] + aq[i])
        cols[f"mo_mid_{h}s"] = np.where(got, side * (mid / price - 1) * 1e4, np.nan)
        cols[f"mo_micro_{h}s"] = np.where(got, side * (micro / price - 1) * 1e4, np.nan)
        j = asof(tend + h * 1000)
        cols[f"ret_mid_{h}s_from_end"] = side * ((bid[j] + ask[j]) / 2 / price - 1) * 1e4  # all orders (Fig. 11)

    # Paper's reversal label: filled, and the first near-side price change at/after the fill is favourable.
    near = bid if side == 1 else ask
    chg = np.flatnonzero(np.diff(near) != 0) + 1
    j = np.searchsorted(bt[chg], tend, side="left")
    ok = j < len(chg)
    nxt = np.where(ok, near[chg[np.minimum(j, len(chg) - 1)]], np.nan)
    phi = filled & ok & (side * (nxt - price) > 1e-9)

    tot = la_fill + lb_fill
    return pl.DataFrame({
        "side": np.full(n, side, np.int8), "t_dec": tdec, "t_post": tpost, "t_end": tend, "price": price,
        "reason": pl.Series(np.array(REASONS)[reason]).cast(pl.Categorical),
        "status": pl.Series(np.array(STATUSES)[status]).cast(pl.Categorical),
        "fill_kind": pl.Series(np.array(FILL_KINDS)[kind]).cast(pl.Categorical),
        "alone_post": np_post != px,
        "la_post": la_post * lot, "q_near_post": nq_post * lot, "q_opp_post": oq_post * lot,
        "qty_filled": qfill * lot, "qty_cross": qcross * lot, "t_first_fill": np.where(got, tfirst, -1),
        "la_fill": np.where(got, la_fill * lot, np.nan), "lb_fill": np.where(got, lb_fill * lot, np.nan),
        "qp_fill": np.where(got, np.divide(la_fill, tot, out=np.zeros(n), where=tot > 0), np.nan),
        "q_near_fill": np.where(got, nq_fill * lot, np.nan), "q_opp_fill": np.where(got, oq_fill * lot, np.nan),
        **cols, "phi": phi,
    })


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--source", default="archive", choices=["archive", "recorded"])
    ap.add_argument("--models", default="pess,prop,opt", help="names or queue exponents k, comma list")
    ap.add_argument("--latency-ms", default="10", help="comma list of total latencies (event -> resting)")
    ap.add_argument("--poll-ms", default="0", help="comma list; 0 = react to every event, N = check every N ms")
    ap.add_argument("--size-lots", default="1", help="comma list of order sizes in lots (1 lot = --lot)")
    ap.add_argument("--tick", type=float, default=0.1)
    ap.add_argument("--lot", type=float, default=0.001)
    ap.add_argument("--out", default="results/sim")
    args = ap.parse_args()

    d0, d1 = date.fromisoformat(args.start), date.fromisoformat(args.end)
    days = [str(d0 + timedelta(n)) for n in range((d1 - d0).days + 1)]
    configs = [(m, int(L), int(P), int(S)) for m in args.models.split(",") for L in args.latency_ms.split(",")
               for P in args.poll_ms.split(",") for S in args.size_lots.split(",")]
    for day in days:
        t0 = time.time()
        book, trades = load_day(args.symbol, day, args.source)
        sides = {s: side_arrays(book, trades, s, args.tick, args.lot) for s in (1, -1)}
        for model, L, P, S in configs:
            k = MODELS[model] if model in MODELS else float(model)
            frames = [orders_frame(run_side(sides[s], k, L // 2, L - L // 2, P, S), s, book,
                                   args.tick, args.lot) for s in (1, -1)]
            df = pl.concat(frames).sort("t_post")
            name = ((model if model in MODELS else f"k{k:g}") + f"-L{L}ms" + (f"-P{P}ms" if P else "")
                    + (f"-S{S}" if S != 1 else ""))
            out = Path(args.out) / args.symbol / name / f"{day}.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            df.write_parquet(out, compression="zstd")
            done = df.filter(pl.col("status").is_in(["fill", "cancel"]))
            print(f"{day} {name:22s} orders={len(done):7,d}  fill_rate={(done['status'] == 'fill').mean():.3f}  "
                  f"filled={done['qty_filled'].sum():,.3f}", flush=True)
        print(f"{day} done in {time.time() - t0:.0f}s ({len(book):,} book rows, {len(trades):,} trades)", flush=True)


if __name__ == "__main__":
    main()
