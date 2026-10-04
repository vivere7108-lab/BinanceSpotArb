"""Fair value for long-tail spot books: the coin's own perpetual, and its beta to BTC.

    python anchors.py --day 2026-10-02 --coins PEPE,BONK,IOTA,WLFI,BANK,GUN,PARTI,ILV

The graph's cross pairs are priced off the USDT books (PLAN.md §13.1). A long-tail spot coin
has no such web on spot, but most have a USDⓈ-M perpetual (61 of the 81 candidates in §16 do),
and the perp trades far more. This tests the perp as the anchor on one day of Binance's public
archive (data.binance.vision): spot and perp aggTrades with exchange timestamps, and BTCUSDT
1 s klines.

* Who leads: on a 1 s grid, the share of the gap between the spot mid and the perp-implied
  price that each side closes over the next 10 s.
* The factor model: the coin's beta to BTC on 1-minute returns and its R², and how well the
  perp gap, BTC's last 10 s, or both predict the spot's next 10 s (R²).
* Maker markouts: for every spot trade, the passive side's realized spread 60 s later, for all
  fills and for those at least N bps better than the perp-implied price 20 ms before the
  trade: the fills a maker quoting off the perp would keep.

Trades only, so mids come from trades: the last buyer-initiated price is the ask, the last
seller-initiated price the bid. A quiet book's quotes can move without trades, so its mid
lags a little. The perp-implied price is the perp mid times the median spot/perp ratio over
the previous 5 minutes (the basis, and the 1000x of perps like 1000PEPEUSDT). Perp data is
public: an account that can't trade perps can still price off them.
"""
import argparse
import bisect
import csv
import datetime as dt
import io
import math
import statistics
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

ARCHIVE = "https://data.binance.vision/data"
PRE_MS = 20
LEAD_MS = 10_000
HORIZON_MS = 60_000
FRESH_MS = 30_000
BASIS_MINUTES = 5
THRESHOLDS_BPS = (0, 5, 10)
DAY_MS = 86_400_000


def fetch(url, cache):
    """Rows of the CSV in an archive zip, without a header; None if there is no such file."""
    path = cache / url.split("/data/", 1)[1].replace("/", "_")
    if not path.exists():
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                path.write_bytes(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
    with zipfile.ZipFile(path) as z:
        rows = list(csv.reader(io.StringIO(z.read(z.namelist()[0]).decode())))
    return rows[1:] if rows and not rows[0][0][:1].isdigit() else rows


def trades(rows):
    """[(time ms, price, qty, buyer is maker)] from spot (µs since 2025) or perp (ms) aggTrades."""
    out = []
    for r in rows:
        t = int(r[5])
        out.append((t // 1000 if t > 10**14 else t, float(r[1]), float(r[2]), r[6].lower() == "true"))
    return out


def mids(tr):
    """(times, mids): after each trade, midway between the last seller- and buyer-initiated prices."""
    times, values, bid, ask = [], [], (0.0, 0), (0.0, 0)
    for t, p, _, buyer_maker in tr:
        if buyer_maker:
            bid = (p, t)  # a seller hit the bid
        else:
            ask = (p, t)
        if bid[0] and ask[0] and t - min(bid[1], ask[1]) <= FRESH_MS:
            times.append(t)
            values.append((bid[0] + ask[0]) / 2)
    return times, values


def at(series, t):
    """Last value of a (times, values) series at or before t, or None."""
    times, values = series
    i = bisect.bisect_right(times, t) - 1
    return values[i] if i >= 0 else None


def ols(xs, ys):
    """(slope, R²) of y on x, with an intercept."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / sxx, sxy * sxy / (sxx * syy)


def r2_two(x1, x2, ys):
    """R² of y on x1 and x2, with an intercept."""
    n = len(ys)
    m1, m2, my = sum(x1) / n, sum(x2) / n, sum(ys) / n
    a = [x - m1 for x in x1]
    b = [x - m2 for x in x2]
    c = [y - my for y in ys]
    s11, s22, s12 = sum(x * x for x in a), sum(x * x for x in b), sum(x * y for x, y in zip(a, b))
    s1y, s2y, syy = sum(x * y for x, y in zip(a, c)), sum(x * y for x, y in zip(b, c)), sum(y * y for y in c)
    det = s11 * s22 - s12 * s12
    b1, b2 = (s22 * s1y - s12 * s2y) / det, (s11 * s2y - s12 * s1y) / det
    return (b1 * s1y + b2 * s2y) / syy


def perp_for(coin, day, cache):
    for sym in (coin + "USDT", "1000" + coin + "USDT", "1000000" + coin + "USDT"):
        rows = fetch(f"{ARCHIVE}/futures/um/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip", cache)
        if rows:
            return sym, rows
    return None, None


def analyse(coin, day, day0, cache, btc):
    sym = coin + "USDT"
    spot_rows = fetch(f"{ARCHIVE}/spot/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip", cache)
    perp_sym, perp_rows = perp_for(coin, day, cache)
    if not spot_rows or not perp_rows:
        print(f"skipping {coin}: no spot or perp trades in the archive for {day}")
        return None
    spot, perp = trades(spot_rows), trades(perp_rows)
    smid, pmid = mids(spot), mids(perp)

    # Basis: median of log(spot mid / perp mid) at each spot mid update, per minute; the
    # basis for minute m is the median of the previous BASIS_MINUTES minutes.
    per_minute = defaultdict(list)
    for t, v in zip(*smid):
        p = at(pmid, t)
        if p:
            per_minute[(t - day0) // 60_000].append(math.log(v / p))
    med = {m: statistics.median(v) for m, v in per_minute.items()}
    basis = {}
    for m in range(1440):
        prev = [med[k] for k in range(m - BASIS_MINUTES, m) if k in med]
        if prev:
            basis[m] = statistics.median(prev)

    def fair(t):
        p, b = at(pmid, t), basis.get((t - day0) // 60_000)
        return p * math.exp(b) if p and b is not None else None

    # Who leads, and what predicts the spot's next 10 s, on a 1 s grid.
    gaps, spot_moves, perp_moves, btc_last = [], [], [], []
    for s in range(day0 + (BASIS_MINUTES + 1) * 60_000, day0 + DAY_MS - LEAD_MS, 1000):
        f, o0, o1 = fair(s), at(smid, s), at(smid, s + LEAD_MS)
        p0, p1 = at(pmid, s), at(pmid, s + LEAD_MS)
        b0, b1 = btc.get((s - LEAD_MS - day0) // 1000), btc.get((s - day0) // 1000)
        if f and o0 and o1 and p0 and p1 and b0 and b1:
            gaps.append(math.log(f / o0))
            spot_moves.append(math.log(o1 / o0))
            perp_moves.append(-math.log(p1 / p0))  # towards the spot when the gap is positive
            btc_last.append(math.log(b1 / b0))
    if len(gaps) < 600:
        print(f"skipping {coin}: too few seconds with both a spot and a perp price")
        return None
    spot_closes = ols(gaps, spot_moves)[0]
    perp_closes = ols(gaps, perp_moves)[0]
    r2_gap = ols(gaps, spot_moves)[1]
    r2_btc = ols(btc_last, spot_moves)[1]
    r2_both = r2_two(gaps, btc_last, spot_moves)

    # Beta to BTC on 1-minute returns of the perp mid (the coin's busiest price).
    xs, ys = [], []
    for m in range(1, 1440):
        t0, t1 = day0 + (m - 1) * 60_000, day0 + m * 60_000
        c0, c1 = at(pmid, t0), at(pmid, t1)
        b0, b1 = btc.get((t0 - day0) // 1000), btc.get((t1 - day0) // 1000)
        if c0 and c1 and b0 and b1:
            xs.append(math.log(b1 / b0))
            ys.append(math.log(c1 / c0))
    beta, r2_minute = ols(xs, ys) if len(xs) > 30 else (math.nan, math.nan)

    # Spot maker markouts against the perp-implied price.
    mk = defaultdict(float)
    for t, p, q, buyer_maker in spot:
        if t + HORIZON_MS > day0 + DAY_MS:
            break
        f0, f1 = fair(t - PRE_MS), fair(t + HORIZON_MS)
        o0, o1 = at(smid, t - PRE_MS), at(smid, t + HORIZON_MS)
        if not (f0 and f1 and o0 and o1):
            continue
        side = 1.0 if buyer_maker else -1.0  # the passive side bought when the buyer is the maker
        w = p * q
        edge = side * (f0 / p - 1) * 1e4
        anchored = side * (f1 / p - 1) * 1e4 * w
        mk["n"] += 1
        mk["w"] += w
        mk["fill"] += side * (o0 / p - 1) * 1e4 * w
        mk["own"] += side * (o1 / p - 1) * 1e4 * w
        mk["anchor"] += anchored
        for n in THRESHOLDS_BPS:
            if edge >= n:
                mk["w", n] += w
                mk["anchor", n] += anchored

    if not mk["w"]:
        print(f"skipping {coin}: no spot fills with prices on both sides 60 s later")
        return None
    spot_usd = sum(p * q for _, p, q, _ in spot)
    perp_usd = sum(p * q for _, p, q, _ in perp)
    return {"coin": coin, "perp": perp_sym, "spot_n": len(spot), "spot_usd": spot_usd, "perp_usd": perp_usd,
            "spot_closes": spot_closes, "perp_closes": perp_closes, "beta": beta, "r2_minute": r2_minute,
            "r2_gap": r2_gap, "r2_btc": r2_btc, "r2_both": r2_both, "mk": mk}


def main(day, coins, fee_bps, cache):
    cache.mkdir(parents=True, exist_ok=True)
    day0 = int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    rows = fetch(f"{ARCHIVE}/spot/daily/klines/BTCUSDT/1s/BTCUSDT-1s-{day}.zip", cache)
    if not rows:
        raise SystemExit(f"no BTCUSDT 1 s klines in the archive for {day} (yet?)")
    btc = {}
    for r in rows:
        t = int(r[0])
        btc[((t // 1000 if t > 10**14 else t) - day0) // 1000] = float(r[4])
    res = [r for r in (analyse(c, day, day0, cache, btc) for c in coins) if r]

    print(f"## Who leads: perp vs spot, {day} (Binance archive, exchange timestamps)\n")
    print("| coin | perp | spot trades | spot volume | perp volume | gap closed in 10 s: by spot / by perp |")
    print("|---|---|---:|---:|---:|---|")
    for r in res:
        print(f"| {r['coin']} | {r['perp']} | {r['spot_n']:,} | ${r['spot_usd'] / 1e6:.2f}M | "
              f"${r['perp_usd'] / 1e6:.1f}M ({r['perp_usd'] / r['spot_usd']:.0f}×) | "
              f"{r['spot_closes']:.0%} / {r['perp_closes']:.0%} |")

    print("\n## The factor model: beta to BTC, and what predicts the spot's next 10 s\n")
    print("| coin | beta to BTC (1-min returns) | R², 1 min | R² of spot's next 10 s on: perp gap / BTC's last 10 s / both |")
    print("|---|---:|---:|---|")
    for r in res:
        print(f"| {r['coin']} | {r['beta']:.2f} | {r['r2_minute']:.0%} | "
              f"{r['r2_gap']:.1%} / {r['r2_btc']:.1%} / {r['r2_both']:.1%} |")

    print(f"\n## Spot maker markouts against the perp-implied price (notional-weighted, bps, before fees)\n")
    print("Filtered columns: share of notional kept / realized spread at +60 s against the perp-implied price.\n")
    print("| coin | fills | at fill | all fills at +60 s: vs spot mid / vs perp | "
          + " | ".join(f"≥ {n} bps better than perp" for n in THRESHOLDS_BPS) + " |")
    print("|---|---:|---:|---|" + "---|" * len(THRESHOLDS_BPS))
    total = defaultdict(float)

    def cells(mk, fee):
        out = [f"{mk['own'] / mk['w'] - fee:+.1f} / {mk['anchor'] / mk['w'] - fee:+.1f}"]
        for n in THRESHOLDS_BPS:
            kept = mk["w", n]
            out.append(f"{kept / mk['w']:.0%} / {mk['anchor', n] / kept - fee:+.1f}" if kept else "–")
        return " | ".join(out)

    for r in res:
        mk = r["mk"]
        for k, v in mk.items():
            total[k] += v
        print(f"| {r['coin']} | {mk['n']:,.0f} | {mk['fill'] / mk['w']:+.1f} | {cells(mk, 0.0)} |")
    if total["w"]:
        print(f"| **all** | {total['n']:,.0f} | {total['fill'] / total['w']:+.1f} | {cells(total, 0.0)} |")
        print(f"| **all, after a {fee_bps:g} bps maker fee** | | | {cells(total, fee_bps)} |")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", required=True, help="UTC day in the archive, e.g. 2026-10-02")
    ap.add_argument("--coins", default="PEPE,BONK,IOTA,WLFI,BANK,GUN,PARTI,ILV",
                    help="spot coins quoted in USDT; the perp is found as COINUSDT or 1000COINUSDT")
    ap.add_argument("--fee-bps", type=float, default=7.5, help="maker fee for the after-fee row")
    ap.add_argument("--cache", default="archive_cache", help="where downloaded archive files are kept")
    args = ap.parse_args()
    main(args.day, [c.strip().upper() for c in args.coins.split(",")], args.fee_bps, Path(args.cache))
