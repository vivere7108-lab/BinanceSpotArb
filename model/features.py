#!/usr/bin/env python3
"""The paper's reversal features (Albers et al., Sec. 7.2) at each simulated order's decision time.

Rows are the simulator's orders in the paper's set D (estimated fill probability > 0.8 on the binned
(Q_near, Q_opp) fill surface). Features are direction-normalised for an order of side s (+1 buy, -1 sell),
named as in the paper's Table 5 (which describes a buy order):
    buy_* / sell_*            taker trades in the order's direction / against it (buy order: taker buys / sells)
    ob_bid_liq / ob_ask_liq   top-of-book qty on the order's side / the opposite side
    ret_sum, ret_vwap         multiplied by s, so positive = price moved up for a buy
Windows per scale (100ms, 1s, 5s, 30s, 300s): w0 = (t-s, t], w1 = (t-2s, t-s], w2 = (t-3s, t-2s].
ob_half / ob_other_half need depth beyond L1 and are omitted (171 of the paper's 173 features).

Labels kept per row:
    phi        the paper's reversal label from the simulator: filled, then the next near-side tick is favourable
    fav_first  factor 1 (market data only): the next near-side top price change after t is favourable
    filled, mo_mid_5s   simulated fill and its 5 s post-fill markout (bp), for economic evaluation

Output: results/model/<SYMBOL>/<sim config>/features.parquet

Usage:
    .venv/bin/python model/features.py --symbol BTCUSDT --config prop-L10ms --start 2024-02-12 --end 2024-02-18
"""

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
from numba import njit, prange

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sim"))
from queue_sim import load_day  # noqa: E402
from validate_paper import surface  # noqa: E402

SCALES = {"100ms": 100, "1s": 1_000, "5s": 5_000, "30s": 30_000, "300s": 300_000}
LAGS = (0, 1, 2)
MAX_LOOKBACK_MS = 3 * max(SCALES.values())


@njit(parallel=True, cache=True)
def window_stats(t, p, q, is_buy, r, ia, ib):
    """Raw stats of trades[ia[i]:ib[i]] for each query i.

    Columns: n_buy, n_sell, q_buy, q_sell, q_max, sum(p*q), p_max, p_min, sum(r), sum(r_j*r_{j-1}), span_ms
    """
    out = np.zeros((ia.shape[0], 11))
    for i in prange(ia.shape[0]):
        a, b = ia[i], ib[i]
        if b <= a:
            continue
        nb = ns = qb = qs = qmax = pq = sr = srr = 0.0
        pmax, pmin = p[a], p[a]
        for j in range(a, b):
            if is_buy[j]:
                nb += 1.0
                qb += q[j]
            else:
                ns += 1.0
                qs += q[j]
            qmax = max(qmax, q[j])
            pq += p[j] * q[j]
            pmax = max(pmax, p[j])
            pmin = min(pmin, p[j])
            sr += r[j]
            if j > a:
                srr += r[j] * r[j - 1]
        out[i, 0], out[i, 1], out[i, 2], out[i, 3], out[i, 4] = nb, ns, qb, qs, qmax
        out[i, 5], out[i, 6], out[i, 7], out[i, 8], out[i, 9] = pq, pmax, pmin, sr, srr
        out[i, 10] = t[b - 1] - t[a]
    return out


def trade_features(trades, t0, side, mid):
    """Window features for decision times t0 (sorted) with order sides and mids at t0."""
    t = trades["t"].to_numpy()
    p = trades["price"].to_numpy()
    q = trades["qty"].to_numpy()
    is_buy = ~trades["buyer_maker"].to_numpy()  # buyer_maker=False: the taker bought
    r = np.zeros(len(p))
    r[1:] = np.log(p[1:] / p[:-1])
    feats = {}
    for name, s in SCALES.items():
        for lag in LAGS:
            ia = np.searchsorted(t, t0 - (lag + 1) * s, side="right")
            ib = np.searchsorted(t, t0 - lag * s, side="right")
            w = window_stats(t, p, q, is_buy, r, ia, ib)
            nb, ns, qb, qs, qmax, pq, pmax, pmin, sr, srr, span = w.T
            n, qsum = nb + ns, qb + qs
            has, two = n > 0, n > 1
            safe_n, safe_q = np.maximum(n, 1), np.where(qsum > 0, qsum, 1.0)
            k = f"_{name}_w{lag}"
            feats["amplitude" + k] = np.where(has, (pmax - pmin) / mid * 1e4, 0.0)
            vwap = np.where(has, pq / safe_q, mid)  # empty window -> 0 return
            feats["ret_vwap" + k] = np.log(vwap / mid) * 1e4 * side
            feats["max_size" + k] = qmax
            feats["avg_size" + k] = np.where(has, qsum / safe_n, 0.0)
            feats["buy_count" + k] = np.where(side == 1, nb, ns)
            feats["sell_count" + k] = np.where(side == 1, ns, nb)
            feats["total_buy" + k] = np.where(side == 1, qb, qs)
            feats["total_sell" + k] = np.where(side == 1, qs, qb)
            feats["ret_autocov" + k] = np.where(
                two, (srr / np.maximum(n - 1, 1) - (sr / safe_n) ** 2) * 1e8, 0.0)
            feats["ret_sum" + k] = sr * 1e4 * side
            feats["trade_intensity" + k] = np.where(two, span / np.maximum(n - 1, 1), float(s))
    return feats


def stdev_features(all_trades, t0):
    """Std (bp) of the last 100 / 500 ten-second returns of the last traded price."""
    bars = (all_trades.select(bar=pl.col("t") // 10_000, price="price")
            .group_by("bar").agg(pl.col("price").last()).sort("bar"))
    full = pl.DataFrame({"bar": np.arange(bars["bar"].min(), bars["bar"].max() + 1)})
    bars = (full.join(bars, on="bar", how="left").with_columns(pl.col("price").forward_fill())
            .with_columns(ret=pl.col("price").log().diff() * 1e4)
            .with_columns(sd100=pl.col("ret").rolling_std(100), sd500=pl.col("ret").rolling_std(500)))
    first = int(bars["bar"][0])
    idx = np.clip(t0 // 10_000 - 1 - first, 0, len(bars) - 1)  # last complete bar before t0
    return {"stdev_100": bars["sd100"].to_numpy()[idx], "stdev_500": bars["sd500"].to_numpy()[idx]}


def asof(times, t):
    """Index of the last row at or before each t."""
    return np.clip(np.searchsorted(times, t, side="right") - 1, 0, len(times) - 1)


def leader_features(lbook, ltrades, fbook, t0, side):
    """State of a price-leading market (e.g. BTCUSDT) at t0, for orders on the follower (e.g. BTCUSDC).

    Signed so that positive = the leader points in the order's favour (up for a buy).
    """
    lt = lbook["t"].to_numpy()
    lbid, lask = lbook["bid_px"].to_numpy(), lbook["ask_px"].to_numpy()
    lbq, laq = lbook["bid_qty"].to_numpy(), lbook["ask_qty"].to_numpy()
    lmid = (lbid + lask) / 2
    i = asof(lt, t0)
    m = lmid[i]
    micro = (lbq[i] * lask[i] + laq[i] * lbid[i]) / (lbq[i] + laq[i])
    f = {"lead_imb": (lbq[i] - laq[i]) / (lbq[i] + laq[i]) * side,
         "lead_micro": (micro / m - 1) * 1e4 * side}
    for name, h in (("100ms", 100), ("500ms", 500), ("1s", 1_000), ("5s", 5_000)):
        f[f"lead_ret_{name}"] = np.log(m / lmid[asof(lt, t0 - h)]) * 1e4 * side
    tt = ltrades["t"].to_numpy()
    flow = np.r_[0.0, np.cumsum(ltrades["qty"].to_numpy() * np.where(ltrades["buyer_maker"].to_numpy(), -1, 1))]
    for name, h in (("100ms", 100), ("1s", 1_000)):
        a, b = np.searchsorted(tt, t0 - h, side="right"), np.searchsorted(tt, t0, side="right")
        f[f"lead_flow_{name}"] = (flow[b] - flow[a]) * side
    lchg = lt[np.r_[True, np.diff(lmid) != 0]]
    f["lead_age"] = (t0 - lchg[np.maximum(np.searchsorted(lchg, t0, side="right") - 1, 0)]) / 1000.0
    # follower vs leader: leader move since the follower's mid last changed, and basis vs its 5-min mean
    ft = fbook["t"].to_numpy()
    fmid = ((fbook["bid_px"] + fbook["ask_px"]) / 2).to_numpy()
    fchg = ft[np.r_[True, np.diff(fmid) != 0]]
    f_last = fchg[np.maximum(np.searchsorted(fchg, t0, side="right") - 1, 0)]
    f["lead_gap"] = np.log(m / lmid[asof(lt, f_last)]) * 1e4 * side
    grid = np.arange(ft[0] - ft[0] % 1000, ft[-1] + 1000, 1000)
    basis = pl.Series(np.log(lmid[asof(lt, grid)] / fmid[asof(ft, grid)]) * 1e4).rolling_mean(300, min_samples=1)
    base_now = basis.to_numpy()[np.clip((t0 - grid[0]) // 1000 - 1, 0, len(grid) - 1)]
    f["lead_basis_dev"] = (np.log(m / fmid[asof(ft, t0)]) * 1e4 - base_now) * side
    return f


def book_features(book, prev_changes, t0, t_label, side):
    """Top-of-book state at t0 plus the factor-1 label at t_label. Returns (features, labels, mid, change times)."""
    bt = book["t"].to_numpy()
    bid, ask = book["bid_px"].to_numpy(), book["ask_px"].to_numpy()
    bq, aq = book["bid_qty"].to_numpy(), book["ask_qty"].to_numpy()
    i = asof(bt, t0)
    changed = np.r_[True, (np.diff(bid) != 0) | (np.diff(ask) != 0)]
    chg_t = np.concatenate([prev_changes, bt[changed]])
    last = np.searchsorted(chg_t, t0, side="right") - 1
    n_10min = last + 1 - np.searchsorted(chg_t, t0 - 600_000, side="right")
    feats = {
        "ob_bid_liq": np.where(side == 1, bq[i], aq[i]),
        "ob_ask_liq": np.where(side == 1, aq[i], bq[i]),
        "age": (t0 - chg_t[np.maximum(last, 0)]) / 1000.0,
        "totb_mean": 600.0 / np.maximum(n_10min, 1),
    }
    # factor 1: does the near-side best price next move in the order's favour after the decision?
    fav = np.full(len(t0), np.nan)
    il = asof(bt, t_label)
    for s, px in ((1, bid), (-1, ask)):
        cidx = np.flatnonzero(np.diff(px) != 0) + 1
        j = np.searchsorted(bt[cidx], t_label, side="right")
        ok = (side == s) & (j < len(cidx))
        new = px[cidx[np.minimum(j, len(cidx) - 1)]]
        fav = np.where(ok, (s * (new - px[il]) > 0).astype(float), fav)
    mid = (bid[i] + ask[i]) / 2
    return feats, {"fav_first": fav}, mid, chg_t[chg_t > bt[-1] - 600_000]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--config", default="prop-L10ms", help="simulator run under results/sim/<SYMBOL>/")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--source", default="archive", choices=["archive", "recorded"])
    ap.add_argument("--d-threshold", type=float, default=0.8, help="keep orders with surface fill prob above this")
    ap.add_argument("--leader", help="symbol of a price-leading market to add lead_* features from, e.g. BTCUSDT")
    ap.add_argument("--info-delay-ms", type=int, default=0,
                    help="compute features this long before the decision (the bot's market-data latency)")
    args = ap.parse_args()

    d0, d1 = date.fromisoformat(args.start), date.fromisoformat(args.end)
    days = [str(d0 + timedelta(n)) for n in range((d1 - d0).days + 1)]
    sim_dir = Path("results/sim") / args.symbol / args.config
    orders = pl.read_parquet([sim_dir / f"{d}.parquet" for d in days])
    orders = orders.filter(pl.col("status").is_in(["fill", "cancel"]))
    # set D: binned fill surface on the whole run, as in the paper's Sec. 7.1
    q, grid, *_ = surface(orders)
    q = q.join(grid.select("qn_bin", "qo_bin", p_fill_surface="fill"), on=["qn_bin", "qo_bin"], how="left")
    D = q.filter(pl.col("p_fill_surface") > args.d_threshold).sort("t_dec")
    print(f"D: {len(D):,} of {len(orders):,} orders ({len(D) / len(orders):.0%})", flush=True)

    all_trades = pl.concat([load_day(args.symbol, d, args.source)[1] for d in days])
    prev_changes, prev_trades, parts = np.zeros(0, np.int64), None, []
    for d in days:
        t_start = time.time()
        book, trades = load_day(args.symbol, d, args.source)
        lo = int(book["t"][0])
        rows = D.filter((pl.col("t_dec") >= lo) & (pl.col("t_dec") <= int(book["t"][-1])))
        t_dec, side = rows["t_dec"].to_numpy(), rows["side"].to_numpy().astype(np.int64)
        t0 = t_dec - args.info_delay_ms  # only data the bot could have seen when deciding
        if prev_trades is not None:  # carry the previous day's tail so early windows are complete
            trades = pl.concat([prev_trades.filter(pl.col("t") > lo - MAX_LOOKBACK_MS), trades])
        bfeat, labels, mid, prev_changes = book_features(book, prev_changes, t0, t_dec, side)
        feats = {**trade_features(trades, t0, side, mid), **stdev_features(all_trades, t0), **bfeat}
        if args.leader:
            lbook, ltrades = load_day(args.leader, d, args.source)
            feats.update(leader_features(lbook, ltrades, book, t0, side))
        part = rows.select("t_dec", "side", "price", "q_near_post", "q_opp_post", "p_fill_surface", "phi",
                           filled=pl.col("status") == "fill", mo_mid_5s="mo_mid_5s")
        part = part.with_columns(**{k: pl.Series(v) for k, v in labels.items()},
                                 **{k: pl.Series(v, dtype=pl.Float32) for k, v in feats.items()})
        parts.append(part)
        prev_trades = trades
        print(f"{d}: {len(part):,} rows, {len(feats)} features, {time.time() - t_start:.0f}s", flush=True)

    name = "features" + (f"_lead-{args.leader}" if args.leader else "") + \
        (f"_delay{args.info_delay_ms}ms" if args.info_delay_ms else "")
    out = Path("results/model") / args.symbol / args.config / f"{name}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.concat(parts).write_parquet(out, compression="zstd")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
