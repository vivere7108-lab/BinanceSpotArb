#!/usr/bin/env python3
"""Binance USD-M futures market-data recorder.

Records, per symbol: bookTicker, depth20@100ms and trade (route /public) and
aggTrade (route /market). Each route is subscribed over N redundant feeds
(default 2, reconnects staggered so the 24 h connection limit never hits both
at once). Messages are de-duplicated by exchange id (first arrival wins) and
written to zstd Parquet, rotated every 15 minutes:

    <out>/<stream>/<SYMBOL>/<YYYY-MM-DD>/<first-row UTC time>.parquet
    <out>/_events/<YYYY-MM-DD>.jsonl    connects, disconnects, closed files, stats

Files are written as *.parquet.tmp and renamed on close, so every *.parquet is
complete. recv_ns is local receive time (ns since epoch, UTC); event_ms (E) and
trans_ms / trade_ms (T) are the exchange's own timestamps.

RPI (retail price improvement) orders are hidden from bookTicker and depth;
aggTrade.qty_nonrpi (Binance field "nq") excludes fills against them, so it is
the quantity that depletes the visible queue. Raw trade rows with type "NA"
are masked fills (price = qty = 0, never in aggTrade), consistent with RPI
fills: keep type == "MARKET" for volume.

Usage:
    .venv/bin/python recorder/record.py --symbols BTCUSDT,BTCUSDC --out data
"""

import argparse
import asyncio
import logging
import os
import signal
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("recorder")

BASE_URL = "wss://fstream.binance.com"

I8, I16, I64, F64 = pa.int8(), pa.int16(), pa.int64(), pa.float64()
LIST_F64 = pa.list_(F64)

COMMON_COLUMNS = [("recv_ns", I64), ("feed", I8), ("symbol", pa.string())]


def _opt_float(x):
    return None if x is None else float(x)


def _depth_row(d):
    return (d["E"], d["T"], d["U"], d["u"], d["pu"],
            [float(p) for p, _ in d["b"]], [float(q) for _, q in d["b"]],
            [float(p) for p, _ in d["a"]], [float(q) for _, q in d["a"]],
            d.get("st"))


# kind -> (dedup id field, kind-specific columns, payload -> row tuple)
KINDS = {
    "bookTicker": (
        "u",
        [("event_ms", I64), ("trans_ms", I64), ("update_id", I64), ("bid_px", F64),
         ("bid_qty", F64), ("ask_px", F64), ("ask_qty", F64), ("st", I16)],
        lambda d: (d["E"], d["T"], d["u"], float(d["b"]), float(d["B"]),
                   float(d["a"]), float(d["A"]), d.get("st")),
    ),
    "depth": (
        "u",
        [("event_ms", I64), ("trans_ms", I64), ("first_update_id", I64), ("update_id", I64),
         ("prev_update_id", I64), ("bid_px", LIST_F64), ("bid_qty", LIST_F64),
         ("ask_px", LIST_F64), ("ask_qty", LIST_F64), ("st", I16)],
        _depth_row,
    ),
    "trade": (
        "t",
        [("event_ms", I64), ("trade_ms", I64), ("trade_id", I64), ("price", F64), ("qty", F64),
         ("buyer_maker", pa.bool_()), ("type", pa.string()), ("st", I16)],
        lambda d: (d["E"], d["T"], d["t"], float(d["p"]), float(d["q"]), d["m"],
                   d.get("X"), d.get("st")),
    ),
    "aggTrade": (
        "a",
        [("event_ms", I64), ("trade_ms", I64), ("agg_id", I64), ("price", F64), ("qty", F64),
         ("qty_nonrpi", F64), ("first_id", I64), ("last_id", I64),
         ("buyer_maker", pa.bool_()), ("st", I16)],
        lambda d: (d["E"], d["T"], d["a"], float(d["p"]), float(d["q"]), _opt_float(d.get("nq")),
                   d["f"], d["l"], d["m"], d.get("st")),
    ),
}


def kind_of(stream):
    """'depth20@100ms' -> 'depth'; 'bookTicker' -> 'bookTicker'."""
    k = stream.split("@")[0]
    return "depth" if k.startswith("depth") else k


def route_of(stream):
    # Binance only pushes a stream on its own route; unrouted URLs no longer carry /market streams.
    return "public" if kind_of(stream) in ("bookTicker", "depth", "trade") else "market"


class Sink:
    """Buffers one (stream, symbol) pair and writes it to rotating Parquet files.

    add() and flush() run on the event loop; _write() runs on the recorder's
    single writer thread, which owns the ParquetWriter.
    """

    def __init__(self, rec, stream, symbol):
        self.rec, self.stream, self.symbol = rec, stream, symbol
        self.kind = kind_of(stream)
        self.key, fields, self.row = KINDS[self.kind]
        self.schema = pa.schema(COMMON_COLUMNS + fields)
        # Delta-encoding the monotonic int64 columns (times, ids) and byte-stream-splitting depth
        # levels cut file size 20-43% vs plain zstd in testing; zstd level itself barely matters.
        ints = [f.name for f in self.schema if f.type == I64]
        lists = [f.name for f in self.schema if pa.types.is_list(f.type)]
        self.parquet_opts = dict(
            rec.parquet_opts,
            use_dictionary=[f.name for f in self.schema if f.name not in ints + lists],
            column_encoding={c: "DELTA_BINARY_PACKED" for c in ints}
            | {f"{c}.list.element": "BYTE_STREAM_SPLIT" for c in lists},
        )
        self.dir = rec.out / stream.split("@")[0] / symbol
        self.max_rows = 10_000 if self.kind == "depth" else 100_000
        self.recent = deque(maxlen=50_000)  # ids seen recently, for cross-feed de-duplication
        self.seen = set()
        self.accepted = self.dups = 0
        self.latency_ms = []
        self._reset()
        self.writer = self.path = None
        self.file_rows = 0

    def _reset(self):
        self.cols = [[] for _ in self.schema]
        self._append = [c.append for c in self.cols]
        self.n = 0

    def add(self, d, recv_ns, feed):
        k = d[self.key]
        if k in self.seen:
            self.dups += 1
            return
        if len(self.recent) == self.recent.maxlen:
            self.seen.discard(self.recent[0])
        self.recent.append(k)
        self.seen.add(k)
        for append, v in zip(self._append, (recv_ns, feed, self.symbol) + self.row(d)):
            append(v)
        self.n += 1
        self.accepted += 1
        self.latency_ms.append(recv_ns // 1_000_000 - d["E"])
        if self.n >= self.max_rows:
            self.flush()

    def flush(self, close=False):
        cols, n = (self.cols, self.n) if self.n else (None, 0)
        if n:
            self._reset()
        if n or close:
            self.rec.pool.submit(self._write, cols, n, close)

    def _write(self, cols, n, close):
        try:
            if n:
                if self.writer is None:
                    self._open(cols[0][0])
                arrays = [pa.array(c, type=f.type) for c, f in zip(cols, self.schema)]
                self.writer.write_table(pa.Table.from_arrays(arrays, schema=self.schema))
                self.file_rows += n
            if close and self.writer is not None:
                self.writer.close()
                self.writer = None
                final = self.path.with_suffix("")  # drop ".tmp"
                os.replace(self.path, final)
                self.rec.event("file", path=str(final.relative_to(self.rec.out)),
                               rows=self.file_rows, bytes=final.stat().st_size)
        except Exception:
            log.exception("write failed: %s %s (rows in this batch are lost)", self.stream, self.symbol)
            if self.writer is not None:
                try:
                    self.writer.close()
                except Exception:
                    pass
                self.writer = None

    def _open(self, first_recv_ns):
        t = datetime.fromtimestamp(first_recv_ns / 1e9, tz=timezone.utc)
        day_dir = self.dir / t.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        stem = t.strftime("%Y%m%dT%H%M%SZ")
        path, i = day_dir / f"{stem}.parquet.tmp", 1
        while path.exists() or path.with_suffix("").exists():
            path, i = day_dir / f"{stem}-{i}.parquet.tmp", i + 1
        self.path = path
        self.writer = pq.ParquetWriter(path, self.schema, **self.parquet_opts)
        self.file_rows = 0


class Recorder:
    def __init__(self, args):
        self.out = Path(args.out).resolve()
        self.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        self.streams = [s.strip() for s in args.streams.split(",") if s.strip()]
        self.nfeeds = args.feeds
        self.rotate_s = args.rotate_min * 60
        self.max_age_s = args.max_age_hours * 3600
        self.silence_s = args.silence_sec
        self.parquet_opts = dict(compression="zstd", compression_level=args.zstd_level)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parquet")
        self.sinks = {f"{sym.lower()}@{st}": Sink(self, st, sym)
                      for sym in self.symbols for st in self.streams}
        self.routes = {}
        for name in self.sinks:
            self.routes.setdefault(route_of(name.split("@", 1)[1]), []).append(name)
        self.bad_messages = 0
        self._events_lock = threading.Lock()
        (self.out / "_events").mkdir(parents=True, exist_ok=True)

    def event(self, kind, **fields):
        """Append one JSON line to today's events file (called from both threads)."""
        now = datetime.now(timezone.utc)
        line = orjson.dumps({"ts": now.isoformat(timespec="milliseconds"), "event": kind, **fields})
        with self._events_lock, open(self.out / "_events" / f"{now:%Y-%m-%d}.jsonl", "ab") as f:
            f.write(line + b"\n")

    def on_message(self, raw, recv_ns, feed):
        try:
            m = orjson.loads(raw)
            sink = self.sinks.get(m.get("stream"))
            if sink is not None:
                sink.add(m["data"], recv_ns, feed)
        except Exception as e:
            self.bad_messages += 1
            if self.bad_messages <= 5:
                log.warning("unparseable message (%s): %.300r", e, raw)

    async def feed(self, route, names, feed):
        url = f"{BASE_URL}/{route}/stream?streams=" + "/".join(names)
        loop = asyncio.get_running_loop()
        # Stagger the first proactive reconnect so the feeds never rotate together.
        max_age = self.max_age_s * (1 - feed / self.nfeeds)
        backoff = 1.0
        while True:
            started, opened, msgs, reason = loop.time(), False, 0, "?"
            try:
                # Binance doesn't answer close frames promptly; a short close_timeout keeps reconnects fast.
                async with connect(url, compression=None, open_timeout=10, ping_interval=20,
                                   ping_timeout=20, close_timeout=1, max_queue=4096) as ws:
                    opened = True
                    self.event("connect", route=route, feed=feed)
                    log.info("connected feed %d /%s (%d streams)", feed, route, len(names))
                    deadline = loop.time() + max_age
                    async with asyncio.timeout(self.silence_s) as watchdog:
                        while True:
                            raw = await ws.recv(decode=False)
                            self.on_message(raw, time.time_ns(), feed)
                            msgs += 1
                            now = loop.time()
                            watchdog.reschedule(now + self.silence_s)
                            if now >= deadline:
                                reason = "rotate"
                                break
            except TimeoutError:
                reason = f"no data for {self.silence_s}s" if opened else "open timeout"
            except ConnectionClosed as e:
                reason = f"closed: {e}"
            except asyncio.CancelledError:
                self.event("disconnect", route=route, feed=feed, reason="shutdown", msgs=msgs,
                           lived_s=round(loop.time() - started, 1))
                raise
            except Exception as e:
                reason = f"{type(e).__name__}: {e}"
            lived = loop.time() - started
            self.event("disconnect", route=route, feed=feed, reason=reason, msgs=msgs, lived_s=round(lived, 1))
            if reason != "rotate":
                log.warning("feed %d /%s disconnected after %.0fs: %s", feed, route, lived, reason)
            max_age = self.max_age_s
            if lived < 30:  # failing fast: back off; a long-lived connection reconnects at once
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            else:
                backoff = 1.0

    async def housekeeping(self):
        period = int(time.time() // self.rotate_s)
        next_report = time.monotonic() + 60
        while True:
            await asyncio.sleep(1)
            try:
                p = int(time.time() // self.rotate_s)
                if p != period:
                    period = p
                    for s in self.sinks.values():
                        s.flush(close=True)
                if time.monotonic() >= next_report:
                    next_report += 60
                    self.report()
            except Exception:
                log.exception("housekeeping failed")

    def report(self):
        stats, parts = {}, []
        for name, s in self.sinks.items():
            lat, s.latency_ms = sorted(s.latency_ms), []
            pct = (lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]) if lat else (lambda q: None)
            stats[name] = dict(rows=s.accepted, dups=s.dups, lat_p50_ms=pct(0.5),
                               lat_p99_ms=pct(0.99), lat_max_ms=lat[-1] if lat else None)
            parts.append(f"{name}={s.accepted}" + (f"({pct(0.5)}ms)" if s.kind == "bookTicker" else ""))
            s.accepted = s.dups = 0
        self.event("stats", bad_messages=self.bad_messages, **stats)
        log.info("rows/min %s", " ".join(parts))

    def close(self):
        for s in self.sinks.values():
            s.flush(close=True)
        self.pool.shutdown(wait=True)
        self.event("stop")


async def amain(args):
    rec = Recorder(args)
    rec.event("start", pid=os.getpid(), symbols=rec.symbols, streams=rec.streams, feeds=rec.nfeeds,
              rotate_min=args.rotate_min)
    log.info("recording %s x %s over %d feed(s) -> %s", ",".join(rec.symbols), ",".join(rec.streams),
             rec.nfeeds, rec.out)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(rec.feed(route, names, k))
             for k in range(rec.nfeeds) for route, names in rec.routes.items()]
    tasks.append(asyncio.create_task(rec.housekeeping()))
    await stop.wait()
    log.info("stopping: flushing buffers")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    rec.close()
    log.info("stopped cleanly")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="BTCUSDT,BTCUSDC")
    ap.add_argument("--streams", default="bookTicker,depth20@100ms,trade,aggTrade")
    ap.add_argument("--out", default="data")
    ap.add_argument("--feeds", type=int, default=2, help="redundant connections per route")
    ap.add_argument("--rotate-min", type=float, default=15, help="Parquet file rotation period")
    ap.add_argument("--max-age-hours", type=float, default=23,
                    help="proactive reconnect age (Binance drops connections at 24 h)")
    ap.add_argument("--silence-sec", type=float, default=60, help="reconnect if a connection is silent this long")
    ap.add_argument("--zstd-level", type=int, default=9)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
