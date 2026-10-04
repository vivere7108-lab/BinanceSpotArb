"""How far do PancakeSwap v3 prices on BNB Chain stray from Binance spot, and is the gap tradable?

    python dex_probe.py --minutes 10 --cex-fee-bps 7.5

Every ~1.5 s it reads the current price (slot0) of the deepest PancakeSwap v3 pools for
the graph's assets, and Binance's best bid/ask for the same pairs. For each sample it
reports the DEX-vs-CEX gap and the edge of the better direction after the pool fee, the
Binance taker fee and gas:

    buy on DEX, sell on Binance:  bid * (1 - cex fee) * (1 - pool fee) / dex price - 1
    buy on Binance, sell on DEX:  dex price * (1 - pool fee) * (1 - cex fee) / ask - 1

This is a probe, not a backtest. Prices are marginal (no price impact). The two sources
are read a few hundred ms apart, so small gaps include timing noise. On-chain execution
also risks being front-run. Run it from Tokyo, next to Binance, for cleaner numbers.
"""
import argparse
import json
import statistics
import time
import urllib.parse
import urllib.request

RPC = "https://bsc-dataseed.binance.org/"
CEX = "https://data-api.binance.vision/api/v3/ticker/bookTicker"
TOKENS = {"WBNB": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "USDT": "0x55d398326f99059fF775485246999027B3197955",
          "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", "ETH": "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
          "BTCB": "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c"}  # all 18 decimals on BNB Chain
# (base, quote, PancakeSwap v3 pool, pool fee, Binance symbol): the deepest pool per pair.
POOLS = [
    ("WBNB", "USDT", "0x172fcd41e0913e95784454622d1c3724f546f849", 0.0001, "BNBUSDT"),
    ("WBNB", "USDT", "0x36696169c63e42cd08ce11f5deebbcebae652050", 0.0005, "BNBUSDT"),
    ("BTCB", "USDT", "0x46cf1cf8c69595804ba91dfdd8d6b960c9b0a7c4", 0.0005, "BTCUSDT"),
    ("ETH", "USDT", "0xbe141893e4c6ad9272e8c04bab7e6a10604501a5", 0.0005, "ETHUSDT"),
    ("WBNB", "USDC", "0xf2688fb5b81049dfb7703ada5e770543770612c4", 0.0001, "BNBUSDC"),
    ("USDC", "USDT", "0x92b7807bf19b7dddf89b706143896d05228f3121", 0.0001, "USDCUSDT"),
]
SWAP_GAS = 150_000  # a v3 swap on BNB Chain, roughly


def post(url, payload):
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def dex_prices():
    """Price of base in quote for every pool, plus gas price (wei), in one batched RPC call."""
    batch = [{"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": p[2], "data": "0x3850c7bd"}, "latest"]}
             for i, p in enumerate(POOLS)]  # slot0()
    batch.append({"jsonrpc": "2.0", "id": len(POOLS), "method": "eth_gasPrice", "params": []})
    res = {r["id"]: r["result"] for r in post(RPC, batch)}
    prices = []
    for i, (base, quote, *_rest) in enumerate(POOLS):
        sqrt_price = int(res[i][2:66], 16) / 2**96
        p = sqrt_price ** 2  # token1 per token0
        base_is_token0 = int(TOKENS[base], 16) < int(TOKENS[quote], 16)
        prices.append(p if base_is_token0 else 1 / p)
    return prices, int(res[len(POOLS)], 16)


def cex_quotes():
    syms = sorted({p[4] for p in POOLS})
    url = CEX + "?symbols=" + urllib.parse.quote(json.dumps(syms, separators=(",", ":")))
    with urllib.request.urlopen(url, timeout=10) as r:
        return {d["symbol"]: (float(d["bidPrice"]), float(d["askPrice"])) for d in json.loads(r.read())}


def main(minutes, cex_fee_bps, size_usd):
    gaps = {i: [] for i in range(len(POOLS))}
    edges = {i: [] for i in range(len(POOLS))}
    gas_bps = []
    t_end = time.time() + minutes * 60
    while time.time() < t_end:
        t0 = time.time()
        try:
            prices, gas_wei = dex_prices()
            quotes = cex_quotes()
        except Exception as e:  # public endpoints hiccup; skip the sample
            print("skip:", e)
            time.sleep(1.5)
            continue
        bnb = sum(quotes["BNBUSDT"]) / 2
        gas = SWAP_GAS * gas_wei / 1e18 * bnb / size_usd * 1e4  # bps of the trade size, one swap
        gas_bps.append(gas)
        for i, (base, quote, _, pool_fee, sym) in enumerate(POOLS):
            bid, ask = quotes[sym]
            p = prices[i]
            cex_fee = 0.0 if sym == "USDCUSDT" else (0.95 if quote == "USDC" else 1.0) * cex_fee_bps / 1e4
            gaps[i].append((p / ((bid + ask) / 2) - 1) * 1e4)
            sell_cex = bid * (1 - cex_fee) * (1 - pool_fee) / p - 1
            buy_cex = p * (1 - pool_fee) * (1 - cex_fee) / ask - 1
            edges[i].append(max(sell_cex, buy_cex) * 1e4 - gas)
        time.sleep(max(0.0, 1.5 - (time.time() - t0)))

    n = len(gas_bps)
    print(f"## DEX vs Binance, {n} samples over {minutes:g} min (CEX taker {cex_fee_bps:g} bps, "
          f"gas on a ${size_usd:,.0f} swap ≈ {statistics.median(gas_bps):.2f} bps)\n")
    print("| pool | pool fee | gap vs Binance mid: median / p95 / max abs (bps) | best edge after costs: median / max (bps) "
          "| % of samples with edge > 0 |")
    print("|---|---:|---|---|---:|")
    for i, (base, quote, _, pool_fee, sym) in enumerate(POOLS):
        g = sorted(abs(x) for x in gaps[i])
        e = sorted(edges[i])
        print(f"| {base}/{quote} | {pool_fee * 100:.2f}% | {statistics.median(g):.2f} / {g[len(g) * 19 // 20]:.2f} / {g[-1]:.2f} "
              f"| {statistics.median(e):.2f} / {e[-1]:.2f} | {sum(x > 0 for x in e) / len(e) * 100:.1f} |")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--cex-fee-bps", type=float, default=7.5, help="Binance taker fee on USDT pairs (USDC pairs get 0.95x)")
    ap.add_argument("--size-usd", type=float, default=1000, help="trade size used to express gas in bps")
    args = ap.parse_args()
    main(args.minutes, args.cex_fee_bps, args.size_usd)
