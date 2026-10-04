# BinanceSpotArb

Cycle arbitrage on Binance spot across USDT, USDC, BTC, ETH and BNB.

* [PLAN.md](PLAN.md): the design plan. Covers the home-node question, fees, depth-aware sizing, parallel execution, market data, rate limits, latency from Tokyo, risk controls and a phased build with a go/no-go gate.
* [`research/`](research/): a reference simulator (74-cycle enumeration, top-of-book screen, depth-aware sizer) plus scripts to record and replay live order books.
