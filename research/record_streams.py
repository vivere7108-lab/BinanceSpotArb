"""Record Binance market streams to JSONL, one file per UTC day if asked.

Each line is {"t": local receive time in ns, "m": raw combined-stream message}.

    python record_streams.py --stream bookTicker --seconds 3600 --out bt.jsonl.gz
    python record_streams.py --stream depth20@100ms --seconds 0 --out /data/live/depth20-{date}.jsonl.gz
    python record_streams.py --base-url wss://fstream.binance.com --symbols BTCUSDT,ETHUSDT,BNBUSDT \\
        --stream markPrice@1s --seconds 0 --out /data/live/perp-markprice-{date}.jsonl.gz

By default it records the 10 spot symbols of the graph. --seconds 0 runs until stopped.
A {date} in --out starts a new file at every UTC midnight, so finished days can be shipped
off the box (see ops/). A restart never appends to an existing file; it starts a numbered
one (name.1.jsonl.gz, ...), and SIGTERM closes the current file cleanly.

Run it from the box you will trade from (AWS Tokyo): receive-time gaps between symbols are
part of what you are measuring. Use a clock synced to the Amazon Time Sync Service (chrony)
so local timestamps can be compared with exchange ones. It reconnects on its own (Binance
closes every connection at 24 h); a reconnect leaves a gap of about a second in the data.
"""
import argparse
import asyncio
import datetime as dt
import gzip
import os
import signal
import sys
import time

import websockets

from arb_core import SYMBOLS


def unused(path: str) -> str:
    """``path``, or ``stem.N.ext`` with the first N that doesn't exist yet."""
    if not os.path.exists(path):
        return path
    stem, ext = path, ""
    for e in (".jsonl.gz", ".jsonl", ".gz"):
        if path.endswith(e):
            stem, ext = path[: -len(e)], e
            break
    k = 1
    while os.path.exists(f"{stem}.{k}{ext}"):
        k += 1
    return f"{stem}.{k}{ext}"


def open_for(out_path: str, now: float):
    """Open a new output file for the UTC day containing ``now``; return (file, time to rotate)."""
    day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    path = unused(out_path.replace("{date}", day.isoformat()))
    opener = gzip.open if path.endswith(".gz") else open
    midnight = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), dt.timezone.utc).timestamp()
    return opener(path, "wt"), (midnight if "{date}" in out_path else float("inf"))


async def record(base_url: str, stream: str, symbols: list[str], seconds: float, out_path: str) -> int:
    url = f"{base_url}/stream?streams=" + "/".join(f"{s.lower()}@{stream}" for s in symbols)
    n, t_end = 0, (time.time() + seconds) if seconds > 0 else float("inf")
    f, rotate_at = open_for(out_path, time.time())
    try:
        while time.time() < t_end:
            try:
                async with websockets.connect(url, max_size=2**22, ping_interval=None) as ws:
                    # The server pings every 20 s and the client library answers; no client pings needed.
                    while time.time() < t_end:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        now = time.time()
                        if now >= rotate_at:
                            f.close()
                            f, rotate_at = open_for(out_path, now)
                        f.write(f'{{"t":{time.time_ns()},"m":{msg}}}\n')
                        n += 1
            except (websockets.ConnectionClosed, websockets.InvalidHandshake, OSError) as e:
                # Binance closes every connection at 24 h (and on maintenance): reconnect and carry on.
                print(f"reconnecting after {e!r}", file=sys.stderr)
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass  # SIGTERM (systemctl stop): fall through and close the file cleanly
    finally:
        f.close()
    return n


async def main(args, symbols):
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    return await record(args.base_url, args.stream, symbols, args.seconds, args.out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stream", default="bookTicker",
                    help="bookTicker | depth20@100ms | depth@100ms | aggTrade | trade | markPrice@1s (perps)")
    ap.add_argument("--symbols", default=",".join(SYMBOLS), help="comma-separated (default: the 10 graph symbols)")
    ap.add_argument("--seconds", type=float, default=600, help="0 = run until stopped")
    ap.add_argument("--out", required=True, help="output path; {date} rotates the file at UTC midnight")
    ap.add_argument("--base-url", default="wss://data-stream.binance.vision",
                    help="spot: wss://stream.binance.com:9443 (or the data-stream mirror); perps: wss://fstream.binance.com")
    args = ap.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    count = asyncio.run(main(args, syms))
    print(f"recorded {count} messages -> {args.out}")
