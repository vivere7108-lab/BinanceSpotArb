#!/usr/bin/env python3
"""Integrity report for recorder output: coverage, gaps, duplicates, latency, disk use.

Usage:
    .venv/bin/python recorder/check_data.py                  # today (UTC)
    .venv/bin/python recorder/check_data.py --date 2026-10-03
    .venv/bin/python recorder/check_data.py --date all
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import orjson
import polars as pl

ID_COL = {"bookTicker": "update_id", "trade": "trade_id", "aggTrade": "agg_id"}  # depth*: update_id
CONTIGUOUS_IDS = {"trade", "aggTrade"}  # ids step by exactly 1, so missing rows are countable


def check(stream, files, gap_s):
    id_col = ID_COL.get(stream, "update_id")
    df = pl.read_parquet(files, columns=["recv_ns", "event_ms", id_col])
    ids = df[id_col].unique().sort()
    t = df["recv_ns"].sort()
    gaps = t.diff().drop_nulls() / 1e9
    lat = df["recv_ns"] // 1_000_000 - df["event_ms"]
    max_gap_at = (datetime.fromtimestamp(t[int(gaps.arg_max())] / 1e9, tz=timezone.utc).strftime("%m-%d %H:%M:%S")
                  if gaps.len() else "-")
    return dict(
        files=len(files),
        rows=df.height,
        hours=round((t[-1] - t[0]) / 3.6e12, 2),
        dup_rows=df.height - ids.len(),
        missing_ids=(int((ids.diff().drop_nulls() - 1).clip(lower_bound=0).sum())
                     if stream in CONTIGUOUS_IDS else None),
        # Trade streams go quiet in calm markets; missing_ids is their real integrity check.
        gaps=int((gaps > gap_s).sum()) if stream not in CONTIGUOUS_IDS else None,
        max_gap_s=round(float(gaps.max()), 2) if gaps.len() else 0.0,
        max_gap_at=max_gap_at,
        lat_p50_ms=lat.quantile(0.5),
        lat_p99_ms=lat.quantile(0.99),
        mb=round(sum(f.stat().st_size for f in files) / 1e6, 2),
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data")
    ap.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    help="UTC date YYYY-MM-DD, or 'all'")
    ap.add_argument("--gap-sec", type=float, default=10.0, help="report receive gaps longer than this")
    args = ap.parse_args()

    root, day = Path(args.data), ("*" if args.date == "all" else args.date)
    rows = []
    for stream_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        for sym_dir in sorted(p for p in stream_dir.iterdir() if p.is_dir()):
            files = sorted(sym_dir.glob(f"{day}/*.parquet"))
            if files:
                rows.append(dict(stream=stream_dir.name, symbol=sym_dir.name,
                                 **check(stream_dir.name, files, args.gap_sec)))
    if not rows:
        print(f"no closed .parquet files for {args.date} under {root}")
        return

    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=250, tbl_hide_dataframe_shape=True):
        print(pl.DataFrame(rows))

    hours = max(r["hours"] for r in rows)
    total_mb = sum(r["mb"] for r in rows)
    if hours > 0:
        print(f"disk: {total_mb:.1f} MB over {hours:.2f} h  ->  ~{total_mb / hours * 24 / 1e3:.2f} GB/day")
    tmp = sorted(root.glob(f"*/*/{day}/*.parquet.tmp"))
    print(f"open/unfinished .tmp files: {len(tmp)} (open now, or lost to a crash if the recorder is stopped)")

    reasons = Counter()
    for f in sorted((root / "_events").glob(f"{day}.jsonl")):
        for line in f.open("rb"):
            e = orjson.loads(line)
            if e["event"] == "disconnect":
                reasons[e["reason"][:80]] += 1
    print("disconnects:", dict(reasons) or "none")


if __name__ == "__main__":
    main()
