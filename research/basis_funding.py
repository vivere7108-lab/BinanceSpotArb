"""Spot vs perpetual: funding carry and basis, from Binance's public data archive.

    python basis_funding.py --months 12 --days 7

Downloads (and caches) from https://data.binance.vision:
* USDⓈ-M perpetual funding-rate history (monthly files), for the carry trade:
  long spot + short perp collects funding while it is positive.
* 1-minute klines for spot and perp (daily files), for the basis (perp close vs
  spot close) and how far it swings.
No API key needed, and unlike fapi.binance.com it is reachable from anywhere.
"""
import argparse
import csv
import datetime as dt
import io
import statistics
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ARCHIVE = "https://data.binance.vision/data"
COINS = ("BTC", "ETH", "BNB")
QUOTES = ("USDT", "USDC")


def fetch_csv(url: str, cache: Path) -> list[list[str]]:
    """Rows of the single CSV inside an archive zip; [] if the file doesn't exist."""
    path = cache / url.split("/data/", 1)[1].replace("/", "_")  # spot and perp files share names
    if not path.exists():
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                path.write_bytes(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise
    with zipfile.ZipFile(path) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    return rows[1:] if rows and not rows[0][0][:1].isdigit() else rows  # drop a header row if present


def months_back(n: int) -> list[str]:
    first = dt.date.today().replace(day=1)
    out = []
    for _ in range(n):
        first = (first - dt.timedelta(days=1)).replace(day=1)
        out.append(first.strftime("%Y-%m"))
    return out[::-1]


def funding(symbol: str, months: list[str], cache: Path):
    """[(time ms, rate, interval hours)] for one perp."""
    out = []
    for m in months:
        for row in fetch_csv(f"{ARCHIVE}/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{m}.zip", cache):
            out.append((int(row[0]), float(row[2]), float(row[1])))
    return sorted(out)


def closes(market: str, symbol: str, days: list[str], cache: Path) -> dict[int, float]:
    """1-minute close by minute (epoch ms). Spot files switched to µs timestamps in 2025."""
    base = f"{ARCHIVE}/spot/daily/klines" if market == "spot" else f"{ARCHIVE}/futures/um/daily/klines"
    out = {}
    for d in days:
        for row in fetch_csv(f"{base}/{symbol}/1m/{symbol}-1m-{d}.zip", cache):
            t = int(row[0])
            out[t // 1000 if t > 10**14 else t] = float(row[4])
    return out


def funding_report(months: list[str], cache: Path):
    print(f"## Funding, {months[0]} to {months[-1]} (positive = longs pay shorts)\n")
    print("| perp | payments | mean per day (bps) | annualised | % of payments positive "
          "| worst 7 days (bps) | best 7 days (bps) |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for coin in COINS:
        for q in QUOTES:
            sym = coin + q
            f = funding(sym, months, cache)
            if not f:
                print(f"| {sym} | 0 | - | - | - | - | - |")
                continue
            span_days = (f[-1][0] - f[0][0]) / 86_400_000 + f[-1][2] / 24
            total = sum(r for _, r, _ in f)
            per_day = total / span_days
            week = []  # rolling 7-day sums of funding
            j = 0
            for i, (t, _, _) in enumerate(f):
                while f[j][0] <= t - 7 * 86_400_000:
                    j += 1
                week.append(sum(r for _, r, _ in f[j:i + 1]))
            print(f"| {sym} | {len(f)} | {per_day * 1e4:.2f} | {per_day * 365 * 100:.1f}% "
                  f"| {sum(r > 0 for _, r, _ in f) / len(f) * 100:.0f}% | {min(week) * 1e4:.1f} | {max(week) * 1e4:.1f} |")


def basis_report(days: list[str], cache: Path):
    print(f"\n## Basis, perp close vs spot close, 1-minute, {days[0]} to {days[-1]}\n")
    print("| pair | minutes | mean (bps) | p5 / median / p95 (bps) | min / max (bps) "
          "| typical 1-hour swing (bps, median abs change) |")
    print("|---|---:|---:|---|---|---:|")
    for coin in COINS:
        for q in QUOTES:
            sym = coin + q
            spot, perp = closes("spot", sym, days, cache), closes("perp", sym, days, cache)
            ts = sorted(set(spot) & set(perp))
            if not ts:
                print(f"| {sym} | 0 | - | - | - | - |")
                continue
            b = {t: (perp[t] / spot[t] - 1) * 1e4 for t in ts}
            v = sorted(b.values())
            hourly = [abs(b[t] - b[t - 3_600_000]) for t in ts if t - 3_600_000 in b]
            print(f"| {sym} | {len(v)} | {statistics.fmean(v):.1f} "
                  f"| {v[len(v) // 20]:.1f} / {statistics.median(v):.1f} / {v[len(v) * 19 // 20]:.1f} "
                  f"| {v[0]:.1f} / {v[-1]:.1f} | {statistics.median(hourly):.1f} |")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", type=int, default=12, help="full months of funding history")
    ap.add_argument("--days", type=int, default=7, help="days of 1-minute klines (ending yesterday)")
    ap.add_argument("--cache", default="archive_cache")
    args = ap.parse_args()
    cache = Path(args.cache)
    cache.mkdir(exist_ok=True)
    today = dt.date.today()
    days = [(today - dt.timedelta(days=k)).isoformat() for k in range(args.days, 0, -1)]
    funding_report(months_back(args.months), cache)
    basis_report(days, cache)
