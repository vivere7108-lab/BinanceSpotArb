"""Measure latency to Binance from this box: run it once in each Tokyo AZ, keep the fastest.

    python latency_probe.py --seconds 60

Three measurements, none needing an API key:
* REST round trip: GET /api/v3/time against each API host (api, api1-4, api-gcp).
* WebSocket API round trip: `ping` requests on one ws-api connection, the transport
  orders would use.
* Market-data lag: local receive time minus Binance's event time on the BTCUSDT trade
  stream (microsecond timestamps). This is only meaningful with a synced clock; the
  script prints chrony's offset. On EC2, chrony uses the Amazon Time Sync Service by default.
"""
import argparse
import asyncio
import json
import statistics
import subprocess
import time
import urllib.request

import websockets

REST_HOSTS = ("api.binance.com", "api1.binance.com", "api2.binance.com", "api3.binance.com",
              "api4.binance.com", "api-gcp.binance.com")
WS_API = "wss://ws-api.binance.com:443/ws-api/v3"
STREAM = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade&timeUnit=MICROSECOND"


def summary(ms):
    if not ms:
        return "no data"
    s = sorted(ms)
    q = lambda p: s[min(len(s) - 1, int(p * len(s)))]
    return f"n={len(s)}  min {s[0]:.2f}  p50 {q(.5):.2f}  p90 {q(.9):.2f}  p99 {q(.99):.2f}  max {s[-1]:.2f} ms"


def rest_rtts(host, n):
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(f"https://{host}/api/v3/time", timeout=5) as r:
                r.read()
        except Exception as e:
            return out, repr(e)
        out.append((time.perf_counter() - t0) * 1e3)
        time.sleep(0.2)
    return out, None


async def ws_api_rtts(n):
    out = []
    async with websockets.connect(WS_API) as ws:
        for i in range(n):
            t0 = time.perf_counter()
            await ws.send(json.dumps({"id": i, "method": "ping"}))
            while json.loads(await ws.recv()).get("id") != i:
                pass
            out.append((time.perf_counter() - t0) * 1e3)
            await asyncio.sleep(0.05)
    return out


async def stream_lags(seconds):
    out, t_end = [], time.time() + seconds
    async with websockets.connect(STREAM) as ws:
        while time.time() < t_end:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            now_us = time.time_ns() / 1e3
            e = msg["data"]["E"]
            e_us = e if e > 10**14 else e * 1e3  # microseconds if the server honoured timeUnit
            out.append((now_us - e_us) / 1e3)
    return out


def chrony_offset():
    try:
        out = subprocess.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=5).stdout
        return next((line.strip() for line in out.splitlines() if line.startswith("System time")), "chrony: no data")
    except Exception as e:
        return f"chrony not available ({e.__class__.__name__}): stream lag may include clock error"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=60, help="how long to sample the trade stream")
    ap.add_argument("--pings", type=int, default=200)
    ap.add_argument("--rest", type=int, default=30, help="requests per REST host")
    args = ap.parse_args()

    print(chrony_offset())
    for host in REST_HOSTS:
        ms, err = rest_rtts(host, args.rest)
        print(f"REST {host:22s} {summary(ms)}" + (f"  (stopped: {err})" if err else ""))
    try:
        print(f"WS API ping {' ' * 15} {summary(asyncio.run(ws_api_rtts(args.pings)))}")
    except Exception as e:
        print(f"WS API ping failed: {e!r}")
    try:
        lags = asyncio.run(stream_lags(args.seconds))
        print(f"Trade stream lag {' ' * 10} {summary(lags)}  (negative values = clock ahead of Binance)")
    except Exception as e:
        print(f"Trade stream failed: {e!r}")
