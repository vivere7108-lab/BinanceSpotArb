#!/usr/bin/env python3
"""Download Binance USD-M futures archive data (data.binance.vision) and convert it to Parquet.

    archive/bookTicker/<SYMBOL>/<YYYY-MM-DD>.parquet   sorted by (trans_ms, update_id)
    archive/aggTrade/<SYMBOL>/<YYYY-MM-DD>.parquet     sorted by agg_id

Column names match recorder/record.py, so the simulator reads both sources the same way.
Zips are verified against Binance's SHA-256 CHECKSUM files and deleted after conversion.
The bookTicker archive ends 2024-03-30 (and its rows are not time-ordered); aggTrades are current.

Usage:
    .venv/bin/python sim/fetch_archive.py --symbol BTCUSDT --start 2024-02-12 --end 2024-02-18
"""

import argparse
import hashlib
import shutil
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

BASE_URL = "https://data.binance.vision/data/futures/um/daily"

# archive kind -> (output stream dir, {csv column: output column}, sort keys)
KINDS = {
    "bookTicker": (
        "bookTicker",
        {"event_time": "event_ms", "transaction_time": "trans_ms", "update_id": "update_id",
         "best_bid_price": "bid_px", "best_bid_qty": "bid_qty",
         "best_ask_price": "ask_px", "best_ask_qty": "ask_qty"},
        ["trans_ms", "update_id"],
    ),
    "aggTrades": (
        "aggTrade",
        {"transact_time": "trade_ms", "agg_trade_id": "agg_id", "price": "price", "quantity": "qty",
         "first_trade_id": "first_id", "last_trade_id": "last_id", "is_buyer_maker": "buyer_maker"},
        ["agg_id"],
    ),
}


def parquet_opts(schema):
    """Same encodings as the recorder: delta for int64 times/ids, dictionary for the rest."""
    ints = [f.name for f in schema if f.type == pa.int64()]
    return dict(compression="zstd", compression_level=9,
                use_dictionary=[f.name for f in schema if f.name not in ints],
                column_encoding={c: "DELTA_BINARY_PACKED" for c in ints})


def fetch(kind, symbol, day, out_root):
    stream, columns, sort_keys = KINDS[kind]
    out = out_root / stream / symbol / f"{day}.parquet"
    if out.exists():
        return f"exists  {out}"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_root / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    name = f"{symbol}-{kind}-{day}"
    url = f"{BASE_URL}/{kind}/{symbol}/{name}.zip"
    zpath, cpath = tmp_dir / f"{name}.zip", tmp_dir / f"{name}.csv"
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(zpath, "wb") as f:
            shutil.copyfileobj(r, f, length=1 << 22)
        expected = urllib.request.urlopen(url + ".CHECKSUM", timeout=60).read().split()[0].decode()
        h = hashlib.sha256()
        with open(zpath, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        if h.hexdigest() != expected:
            raise RuntimeError(f"checksum mismatch for {name}.zip")
        with zipfile.ZipFile(zpath) as z, z.open(z.namelist()[0]) as src, open(cpath, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1 << 22)
        zpath.unlink()
        tbl = pacsv.read_csv(cpath)
        cpath.unlink()
        tbl = (tbl.select(list(columns)).rename_columns(list(columns.values()))
               .sort_by([(k, "ascending") for k in sort_keys]))
        tmp_out = out.with_suffix(".parquet.tmp")
        pq.write_table(tbl, tmp_out, **parquet_opts(tbl.schema))
        tmp_out.rename(out)
        return f"wrote   {out} ({tbl.num_rows:,} rows, {out.stat().st_size / 1e6:.0f} MB)"
    finally:
        zpath.unlink(missing_ok=True)
        cpath.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--start", required=True, help="first UTC date, YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="last UTC date (inclusive)")
    ap.add_argument("--kinds", default="bookTicker,aggTrades")
    ap.add_argument("--out", default="archive")
    ap.add_argument("--workers", type=int, default=2, help="parallel days (each needs ~4 GB RAM for bookTicker)")
    args = ap.parse_args()

    d0, d1 = date.fromisoformat(args.start), date.fromisoformat(args.end)
    days = [str(d0 + timedelta(n)) for n in range((d1 - d0).days + 1)]
    jobs = [(k, args.symbol.upper(), d) for d in days for k in args.kinds.split(",")]
    with ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(fetch, *job, Path(args.out)): job for job in jobs}
        for fut in as_completed(futures):
            try:
                print(fut.result(), flush=True)
            except Exception as e:
                print(f"FAILED  {futures[fut]}: {e}", flush=True)


if __name__ == "__main__":
    main()
