# BinanceSpotArb

Cycle arbitrage on Binance spot across USDT, USDC, BTC, ETH and BNB.

* [PLAN.md](PLAN.md): the design plan. Covers the home-node question, fees, depth-aware sizing, parallel execution, market data, rate limits, latency from Tokyo, risk controls and a phased build with a go/no-go gate.
* [`research/`](research/): a reference simulator (74-cycle enumeration, top-of-book screen, depth-aware sizer, routing) and scripts to:
  * record and replay live order books;
  * rank pairs for market making, and compare fair-value estimators (USDT as source of truth won);
  * measure perp funding and spot–perp basis from Binance's public archive;
  * measure latency to Binance per AWS zone, and probe DEX (PancakeSwap) vs Binance prices;
  * scan every spot pair for spreads a small maker could earn, and measure what makers actually earned there (realized spread against the pair's own mid or a USDT anchor).
* [`ops/`](ops/): Phase 0 on AWS Tokyo. A step-by-step runbook plus scripts that record the streams around the clock and ship each finished day to S3.
