# BinanceSpotArb

> **Status 2026-10-06: the crypto lab.** Registered in
> [Wealth-Optimiser](https://github.com/vivere7108-lab/Wealth-Optimiser)'s `MAP.md`, which is the single
> source of truth for every line's state. Open here: long-tail spot making priced off each coin's perp
> (phase 0 recording, `PLAN.md` §16–17). Closed at the owner's fees: taker cycles, DEX–CEX, intraday
> basis. Blocked by regulation (Australian retail, spot only): cash-and-carry, perp hedges, perp making;
> perp making was also measured negative in BinanceMarketMaking, which merges into this repo as
> `perps/`. The default branch is `claude/great-dijkstra-n3wu1n` and the Tokyo runbook clones it by
> name: do not delete it until `main` exists and the box is confirmed on it (`MAP.md` §10). Whether the
> Tokyo instance exists is not yet written anywhere; a `STATUS.md` here should say so in its first line.

Cycle arbitrage on Binance spot across USDT, USDC, BTC, ETH and BNB.

* [PLAN.md](PLAN.md): the design plan. Covers the home-node question, fees, depth-aware sizing, parallel execution, market data, rate limits, latency from Tokyo, risk controls and a phased build with a go/no-go gate.
* [`research/`](research/): a reference simulator (74-cycle enumeration, top-of-book screen, depth-aware sizer, routing) and scripts to:
  * record and replay live order books;
  * rank pairs for market making, and compare fair-value estimators (USDT as source of truth won);
  * measure perp funding and spot–perp basis from Binance's public archive;
  * measure latency to Binance per AWS zone, and probe DEX (PancakeSwap) vs Binance prices;
  * scan every spot pair for spreads a small maker could earn, and measure what makers actually earned there (realized spread against the pair's own mid or a USDT anchor);
  * test which fair value a long-tail spot book follows: its coin's perp (it leads), BTC beta, or its own queues.
* [`ops/`](ops/): Phase 0 on AWS Tokyo. A step-by-step runbook plus scripts that record the streams around the clock and ship each finished day to S3.
