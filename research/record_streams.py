"""Record Binance spot market streams for the 10 graph symbols to JSONL.

Each line is {"t": local receive time in ns, "m": raw combined-stream message}.

    python record_streams.py --stream bookTicker --seconds 3600 --out bt.jsonl.gz
    python record_streams.py --stream depth20@100ms --seconds 3600 --out d20.jsonl.gz

Run it from the box you will trade from (AWS Tokyo): receive-time gaps between
symbols are part of what you are measuring. Use a clock synced to the Amazon Time
Sync Service (chrony) so local timestamps can be compared with exchange ones.
"""
import argparse
import asyncio
import gzip
import time

import websockets

from arb_core import SYMBOLS


async def record(base_url: str, stream: str, seconds: float, out_path: str) -> int:
    url = f"{base_url}/stream?streams=" + "/".join(f"{s.lower()}@{stream}" for s in SYMBOLS)
    n, t_end = 0, time.time() + seconds
    opener = gzip.open if out_path.endswith(".gz") else open
    with opener(out_path, "wt") as f:
        async with websockets.connect(url, max_size=2**22, ping_interval=None) as ws:
            # The server pings every 20 s and the client library answers; no client pings needed.
            while time.time() < t_end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                f.write(f'{{"t":{time.time_ns()},"m":{msg}}}\n')
                n += 1
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stream", default="bookTicker", help="bookTicker | depth20@100ms | depth@100ms | trade")
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-url", default="wss://data-stream.binance.vision",
                    help="market-data-only endpoint; wss://stream.binance.com:9443 also works")
    args = ap.parse_args()
    count = asyncio.run(record(args.base_url, args.stream, args.seconds, args.out))
    print(f"recorded {count} messages in {args.seconds:.0f}s -> {args.out}")
