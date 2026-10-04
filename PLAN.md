# Binance spot cycle arbitrage: plan

Draft v7, 2026-10-04: Phase 0 set-up for AWS Tokyo ([`ops/README.md`](ops/README.md)), an Australian spot-only account (§13.5), DEX–CEX (§15), making the long tail of spot pairs (§16) and pricing it off each coin's perp (§17). Assets: USDT, USDC, BTC, ETH, BNB on Binance spot.

## 0. Recommendations

1. **Score every cycle, not paths between home nodes.** All 10 pairs between the five assets exist, so the graph is complete. It has exactly 74 directed cycles of 3–5 legs. Enumerate them once at startup. When a book updates, re-score only the 30 cycles that use that symbol. That costs about 40 µs in plain Python, so narrowing the search saves nothing.
2. **A "home" is an accounting choice, not a path constraint.** A cycle's return is the same whichever asset you start from. The start asset only decides where the surplus lands and, for sequential execution, which asset you must hold first. With parallel execution you hold all five anyway.
3. **Use one numeraire (USDT) and one fee float (BNB), not two homes.** Book all P&L in USDT. BNB is not a stable asset: hold it as a fee float with a target band. When the float runs low, land cycle surpluses in BNB instead of buying it separately. That is the useful version of "BNB as home".
4. **Open paths between nodes are already covered by cycles.** Take any route from A to B, say USDT⇝BNB. If it beats the direct A→B conversion, then, to within that pair's spread, it is a profitable cycle closed by the direct leg back. Judging an open path against a mid price instead is a directional bet on B. Picking the cheapest route for a purchase you need anyway, such as topping up BNB, is routing, and it belongs in the treasury (§2.3).
5. **Parallel execution means trading from inventory.** Hold working balances in every asset. Fire all legs at once as LIMIT IOC orders at the worst price the simulator touched. A rebalancer cleans up leg mismatches. This takes one round trip instead of one per leg. Send the contested leg (the quote most likely to vanish) first. If its misses prove expensive, fall back to sending the rest only after it fills (§5.1).
6. **Trade size = min(depth-optimal size, packet cap, inventory per leg), then apply exchange filters.** "Depth-optimal" means walking all legs' books together until the marginal cycle rate falls to 1 plus a buffer. That is the right generalisation of "bottleneck volume" (§4.2).
7. **Fees decide viability, so measure before building anything else.** At your VIP 2 fees the cheapest cycles are the six USDT→coin→USDC→USDT triangles. Their USDCUSDT leg is free, so they need **14.6 bps** gross; every other cycle needs 21.8 bps or more (§3). In the 30-minute live sample (§12), no cycle was ever net-positive at your fees. The closest, USDT→USDC→BNB→USDT, stayed at least 5.3 bps short. Phase 0 (§11) is a cheap go/no-go measurement from Tokyo at your fees, done before any C++ is written.
8. **Your constraints, in the order they bind:**
   * **Fees** bind first, by a wide margin.
   * **Latency** is next: the opportunities that do appear last milliseconds.
   * **The order-rate limit** binds only through misses, since filled orders don't count against it.
   * **Streaming rate and simulation time** are engineering work at the µs level, not limits (§6, §8).
9. **Expect cents per trade at $100 packets.** 5 bps net on $100 is $0.05. Profit must come from frequency, so Phase 0 should count opportunities per day above your hurdle, not just whether any exist. Grow inventory only if it finds size worth taking (§5.2).
10. **Market making: quote the slow cross pairs against fair value read from the USDT books, and get the maker fee to zero first.** Hedging each fill straight away through the graph never paid: the 7.1–7.5 bps taker hedge outweighs any spread here. Unhedged quotes at the touch need a maker fee of zero or less, and real fills on the cross pairs were often picked off by arbitrageurs. But fills where the quote was already ≥ 1 bps better than USDT-based fair value earned +1.9 to +2.6 bps on ETHBTC, BNBBTC and BNBETH. A maker that re-quotes off the USDT books keeps those and dodges the stale-quote losses. Reading fair value from USDT beat the median of routes in a direct comparison (§13.1). Build the quoting engine and run it in shadow now; go live once you're inside Binance's maker programme.
11. **Perps: hedge first, carry second.** Over the last year a short perp against held coins earned funding rather than cost it. Cash-and-carry earned ~2.5–3.6% a year gross on BTC and ETH: slow and modest at current funding. Intraday basis trading doesn't pay at these fees, because the basis moves under 1 bps an hour (§13.2–§13.3).
12. **DEX–CEX arbitrage: not on these assets at your fees.** PancakeSwap's deepest pools tracked Binance within 1–3 bps. In 10 minutes the best moment was still 4.3 bps short of covering your taker fee and the pool fee (§15).
13. **Small capital's natural edge is the long tail of spot pairs, and it isn't proven yet.**
    * 86 Binance pairs have spreads wider than two maker fees and $0.2–20M a day of volume.
    * If your fills were 1% of their volume, each at the full half-spread, they would pay ~$320 a day: the "few hundred a day" scale.
    * In 36 minutes of live trades, though, the spread mostly paid for adverse selection. The average fill was worth about −1 bps after 60 s before fees, and −9 bps after a 7.5 bps maker fee.
    * The pairs that earned were mostly ones whose price never moved, which makes this short volatility.
    * Phase 0 now records all of them for a week to settle it (§16).
14. **Price the long tail off each coin's own perpetual.** 61 of the 81 candidate coins have one, and it leads their spot book on every coin and day tested. Quoting only when the perp says the touch is ≥ 10 bps good lifted the realized spread from about 0 to 6–9 bps before fees, which is about break-even after a 7.5 bps maker fee; CYBER and BONK cleared it on all three days. BTC beta adds little once you have the perp. The book's queue sizes are the second signal (§17).

## 1. The graph

All 10 symbols were `TRADING` in `exchangeInfo` on 2026-10-04. Tick and step values below are converted to bps and USD at that day's prices (BTC ≈ 85.2k, ETH ≈ 2.70k, BNB ≈ 795 USDT).

| Symbol | Tick | Tick in bps | Step | Step ≈ USD | Min notional | Median spread (bps) |
|---|---|---:|---|---:|---|---:|
| BTCUSDT | 0.01 | 0.001 | 0.00001 BTC | 0.85 | 5 USDT | 0.00 |
| ETHUSDT | 0.01 | 0.04 | 0.0001 ETH | 0.27 | 5 USDT | 0.04 |
| BNBUSDT | 0.01 | 0.13 | 0.001 BNB | 0.80 | 5 USDT | 0.13 |
| USDCUSDT | 0.00001 | 0.10 | 1 USDC | 1.00 | 5 USDT | 0.10 |
| BTCUSDC | 0.01 | 0.001 | 0.00001 BTC | 0.85 | 5 USDC | 0.00 |
| ETHUSDC | 0.01 | 0.04 | 0.0001 ETH | 0.27 | 5 USDC | 0.04 |
| BNBUSDC | 0.01 | 0.13 | 0.001 BNB | 0.80 | 5 USDC | 0.13 |
| ETHBTC | 0.00001 | 3.15 | 0.0001 ETH | 0.27 | 0.0001 BTC (≈ 8.5 USD) | 3.15 |
| BNBBTC | 0.000001 | 1.07 | 0.001 BNB | 0.80 | 0.0001 BTC (≈ 8.5 USD) | 1.08 |
| BNBETH | 0.0001 | 3.40 | 0.001 BNB | 0.80 | 0.001 ETH (≈ 2.7 USD) | 3.41 |

**Cycle counts.** There are 20 three-leg, 30 four-leg and 24 five-leg directed cycles, 74 in total. Every symbol appears in 30 of them. Two-leg round trips on one symbol always lose the spread, so they are excluded.

**Where gross edge comes from.** Two sources showed up in the sample (§12):

1. **Tick-constrained cross pairs.** ETHBTC, BNBBTC and BNBETH have spreads pinned at one tick, worth 1–3.4 bps. They update 30–55× less often than BTCUSDT. Their mid can therefore sit about half a tick away from the cross price implied by the USDT legs. Example from 09:24 UTC: BNBETH's mid was 3.3 bps below the implied BNBUSDT/ETHUSDT cross, so USDT→ETH→BNB→USDT showed +1.5 bps gross at the touch.
2. **A USDT/USDC basis on BNB.** For long stretches BNB was cheaper against USDC than against USDT. USDT→USDC→BNB→USDT reached +9.3 bps gross and was gross-positive 40% of the time.

Both sit far inside everyone's fee hurdle, which is why they persist. The tradable version is narrower. It is the few milliseconds when a better quote appears on one leg, or when a slow book lags after a liquid leg moves. That is a latency race (§8).

## 2. Home nodes: the recommendation in detail

### 2.1 Why the start node doesn't matter for detection

Write the leg rates as r₁…r_k (units of `dst` per unit of `src`, net of fees). The cycle multiple R = ∏ rᵢ is the same for every rotation. Starting with x of asset A returns xR of A, a surplus of x(R − 1) in A. Rotating the cycle changes only which asset holds the surplus. The depth limits are physical (the same order-book levels), so the bottleneck is the same whichever asset you count it in. `Cycle.rotated_to()` in `research/arb_core.py` does the rotation.

### 2.2 Parallel execution makes "home" purely accounting

When all legs trade from inventory at the same moment, size them so every intermediate asset nets to zero (leg i+1 spends exactly what leg i receives). The surplus then appears in the rotation's start asset, the **sink**:

* Default sink: **USDT**, the P&L numeraire.
* When the BNB float is below its band, the sink is **BNB**. Profits top up the fee float with no separate purchase or extra fee.

Whether a cycle passes through USDT or BNB is irrelevant to execution. Only 14 of the 74 cycles avoid USDT, and only 2 avoid both USDT and BNB (USDC↔BTC↔ETH in either direction). Keep them all.

### 2.3 Split the system into two jobs

| | Arbitrage engine | Treasury / rebalancer |
|---|---|---|
| Question | "Is there a cycle with R > 1 + hurdle right now?" | "Are inventories inside their bands? Is the BNB float healthy?" |
| Objects | 74 cycles | Open paths (A ⇝ B) for rebalancing and BNB top-ups |
| Speed | Hot path, µs | Off the hot path, seconds |
| P&L | Realised in USDT (or BNB when topping up) | Cost centre: minimise slippage and fees |

Both jobs use the same simulator. The treasury calls it on an open path instead of a cycle (`fill_path` and `best_route` in `arb_core.py`): of the 16 simple routes between any two assets, it picks the one that delivers the most.

### 2.4 Leg count

Score all 74 cycles; it's cheap. Execute only 3-leg cycles in v1, starting with the six cross-quote triangles (§3.2). Each extra leg adds a full taker fee and multiplies in another fill probability (P(all fill) = ∏ pᵢ). Enable 4- and 5-leg cycles once live fill statistics justify the extra risk. Longer cycles sometimes reach slightly higher gross edges by chaining several small mispricings. But each extra leg pays another fee: at 2 bps per leg, every net-positive episode in the sample was a 3-leg cycle.

## 3. Fees

### 3.1 Your schedule (VIP 2, fees paid in BNB)

VIP 2 needs ≥ $5M of 30-day spot volume and ≥ 25 BNB; see §13.5. At VIP 0 the maker rate is 0.075% instead of 0.06% and everything else below is unchanged.

| Pairs | Maker | Taker |
|---|---:|---:|
| USDT pairs and the cross pairs (ETHBTC, BNBBTC, BNBETH) | 0.060% | 0.075% |
| USDC pairs (BTCUSDC, ETHUSDC, BNBUSDC) | 0.060% | 0.07125% |
| USDCUSDT | 0 | 0 |
| USDⓈ-M futures (only relevant for hedging, §5.2) | ≈ 0.020% | ≈ 0.050% |

### 3.2 What it means for cycles

Taker hurdle each cycle must clear at the touch, before any latency buffer (`hurdle()` in `arb_core.py`):

| Hurdle | Cycles | Shape | Example |
|---:|---:|---|---|
| **14.63 bps** | **6** | **coin bought on one quote, sold on the other, closed through the free USDCUSDT leg** | **USDT→BNB→USDC→USDT** |
| 21.76 bps | 6 | USDC triangle through a cross pair | USDC→BTC→ETH→USDC |
| 22.13 bps | 12 | 4 legs, one of them USDCUSDT | USDT→USDC→BTC→ETH→USDT |
| 22.51 bps | 8 | USDT triangle through a cross pair, or the three cross pairs alone | USDT→BTC→ETH→USDT |
| 29.26–36.76 bps | 42 | all remaining 4- and 5-leg cycles | USDT→BTC→USDC→ETH→USDT |

* **VIP 2 doesn't help a taker-only design much.** Its taker rate (0.075%) equals VIP 0's with the BNB discount; only the maker rate and the USDC-pair taker rate improve.
* **The fee-free USDCUSDT is what changes the picture.** It makes the six cross-quote triangles about 7 bps cheaper than any other cycle. **v1 should concentrate on them.** This is the classic same-coin, two-quote-books trade: buy BTC, ETH or BNB on whichever of its USDT and USDC books is cheaper, sell it on the other, and convert USDC↔USDT for free.
* **Rebalancing between USDT and USDC is almost free** for the same reason. `exchangeInfo` lists no smart-order-routing groups today, so nothing on Binance's side automatically arbitrages the USDT and USDC books against each other.
* **Maker legs help only a little at VIP 2.** A maker leg saves 1.1–1.5 bps (0.06% against 0.07125–0.075%), bringing a cross-quote triangle from 14.6 to about 13.1–13.5 bps. See §5.5.

### 3.3 Mechanics

* **Fetch rates per symbol; don't hardcode.** Call `GET /api/v3/account/commission?symbol=…` (IP weight 20) for each of the 10 symbols at startup and hourly. The total rate is standard (multiplied by `discount` when paying in BNB) + tax + special. Pass the result to the simulator as a per-symbol schedule.
* **Paying fees in BNB.** Each leg delivers its gross amount and the fee is a separate BNB debit: notional × rate × discount ÷ BNB price. The simulator multiplies each leg by (1 − f), which is correct to first order and fine for screening. The ledger should book the BNB debit exactly. If the BNB float runs out, Binance takes the fee from the received asset at the undiscounted rate, so alarm well before that.
* **For reference:** VIP 0 is 0.10% maker and taker before the BNB discount (`discount` = 0.75, i.e. 25% off). The published VIP 9 rate is 0.011% maker / 0.023% taker. VIP level depends on 30-day volume and BNB balance.

## 4. Simulation and sizing

### 4.1 Two-stage evaluation

1. **Screen** on every top-of-book update for symbol s. For each of the 30 cycles using s, recompute `edge_c = Σ log rᵢ + Σ log(1 − fᵢ)` (`log_edge`). Measured cost: ~40 µs per update in plain Python. A compiled version would be well under 1 µs.
2. **Size** only the cycles with `edge_c > hurdle` (`size_cycle`), using the depth walk below. Worst case in Python with 20-level books: 44 µs for 3 legs, 78 µs for 5.

### 4.2 Depth-aware sizing: "bottleneck volume", generalised

Each leg is a concave piecewise-linear function (output vs input) with one kink per price level. The cycle is the composition of its legs, so profit keeps rising while the **marginal** cycle rate is above 1. The walk:

```
mult_i  = product of the current-level rates of legs before i   # start-asset units -> leg i input units
m       = product of all legs' current-level rates               # marginal cycle rate
while m > 1 + min_edge and size < packet_cap:
    step = min over legs of (remaining capacity at leg i's current level / mult_i)
    size += step;  out += step * m
    advance whichever legs' levels are now used up; recompute mult, m
```

The walk returns the start amount, the base quantity for each leg's order, and the worst level price touched on each leg, which becomes the IOC limit price. The bottleneck can move between legs as the walk goes deeper, and the walk handles that. `min_edge` is a marginal hurdle: the walk stops taking deeper levels once the next unit earns less than the buffer you want against latency.

### 4.3 Caps, in order

1. **Depth-optimal size** x* from the walk.
2. **Packet cap.** x ≤ P, defined in USDT and converted into the start asset. This is the transit-risk limit.
3. **Inventory.** Each leg's input must be ≤ the available balance of its `src` asset minus a reserve.
4. **Rate-limit budget.** Fire only if every leg fits in the order budget (§7).
5. **Exchange filters.** Round quantities down to `stepSize` and prices to `tickSize`. Every leg must be ≥ `minNotional` and ≤ `maxQty`, and the limit must sit inside `PERCENT_PRICE_BY_SIDE`.
6. **Re-score after rounding.** Drop the cycle if it no longer clears the hurdle.

Rounding: one step is worth $0.27–$1 (table above), which is up to 1% of a leg on a $100 packet. In the inventory model this residue isn't lost; it stays in inventory as small drift that the rebalancer absorbs. Drift between USDT and USDC costs nothing to tidy, since USDCUSDT is fee-free. At $100 packets, though, rounding makes per-trade P&L noisy: mark any leftovers at mid when you measure results.

### 4.4 Several opportunities at once

Cycles that share a symbol compete for the same levels.

* **v1:** one cycle in flight per symbol (symbol locks), and pick the highest expected profit.
* **v2:** greedy allocation. Take the best cycle, subtract its consumed depth from a scratch copy of the books, re-score, and repeat. Net out opposite legs on the same symbol before sending.

### 4.5 Expected value, not touch profit

`EV = P(all legs fill) × profit − E[repair cost | partial fill]`

Start with a flat per-leg buffer (`min_edge`). Replace it with a fitted model once shadow and live data show how P(fill) depends on opportunity age, leg count and which book was stale.

## 5. Execution: parallel legs

### 5.1 Sequential vs parallel

| | Sequential | Parallel (inventory) | Hybrid: contested leg first |
|---|---|---|---|
| Round trips to complete | k (one per leg) | 1 | 2 |
| Capital | the start asset only | working balance in every asset of the cycle | same as parallel |
| Price risk | only while legs are in flight | standing inventory in BTC/ETH/BNB | same as parallel |
| Typical failure | a later leg misses, leaving you holding an intermediate asset | some legs fill and others don't, so inventory drifts | contested leg misses, so nothing else is sent (cheap) |
| Sensitivity to the edge decaying | high: legs 2..k see books that have moved | low | low: the deep legs rarely move much in one round trip |

Recommendation: **parallel**, with the hybrid as a measured fallback.

* Net-positive episodes in the sample lasted a median of a few milliseconds (§12). Sequential execution would reach legs 2–3 after most of them had closed.
* The edge usually hinges on one *contested* quote, the one most likely to vanish. In the sample that was almost always a BNB quote, in one of two forms. Either a better quote on BNBUSDC or BNBUSDT appeared and vanished within ~6 ms, or a slow BNB cross quote (BNBETH, BNBBTC) was left behind after a liquid leg moved. The engine knows which update created the edge, so it knows which leg that is.
* So send the contested leg first. If its miss rate turns out high, switch to the hybrid: wait for that leg's fill before sending the rest. A parallel miss leaves drift that costs about two more fees and spreads to repair. A hybrid miss costs nothing but order budget.
* Measure both in Phases 1–2.

### 5.2 Inventory

* **Starting point: ~$100 per coin.** That allows one ~$100 packet in flight, or two of ~$50, before a leg runs short. Set a band around each target; the rebalancer acts only outside it.
* **Profit per trade.** 5 bps net on $100 is $0.05, so daily profit ≈ opportunities per day × capture rate × a few cents. In the sample, the depth-optimal size of a hypothetical lower-fee edge was typically ~$1k and sometimes over $10k (§12). Grow inventory only if Phase 0 finds sizes like that above your hurdle.
* **Directional exposure.** About $300 sits in BTC, ETH and BNB. A 3% daily move swings that by about ±$9, far more than cent-level arbitrage profits while you are testing. A short perp against each coin makes the inventory delta-neutral, and over the last year that hedge would have earned funding rather than cost it (§13.3). At ~$300 the cost is mostly operational: margin and a second order gateway. Add it once the engine tracks positions.
* **Sink.** Surpluses land in USDT by default, and in BNB when the float is low (§2.2).

### 5.3 Order mechanics

* **Transport.** Use the WebSocket API (`wss://ws-api.binance.com:443/ws-api/v3`) with an **Ed25519** key. One `session.logon`, and later requests need no per-request signature. Connections last at most 24 h, so rotate proactively with an overlapping second session. Benchmark FIX order entry (`fix-oe.binance.com:9000`, also Ed25519-only) against it in Phase 2.
* **Order parameters.** `order.place` with `type=LIMIT`, `timeInForce=IOC`, `price` = worst level from the walk, `quantity` in base units, `newOrderRespType=ACK` and `newClientOrderId=<cycleId>-<leg>`. Rely on the user data stream for fills.
* **Why LIMIT IOC.** It caps each leg at the price you simulated; MARKET orders can walk further. FOK makes each leg all-or-nothing but doesn't remove cross-leg risk. IOC plus repair keeps whatever was really there.
* **Dispatch.** Send all k requests back-to-back without waiting for acks, contested leg first. Measure one connection against one connection per leg; the docs don't promise processing order within a connection.
* **No atomic multi-leg orders.** Order lists (OCO/OTO/OTOCO/OPO) are single-symbol only, so legging risk is inherent and handled by repair (§5.4).
* **Price Range Execution Rule (rolled out March–April 2026).** Taker fills outside a dynamic band around a reference price now expire. Treat the expired remainder as a partial fill; the response includes an `expiryReason`.

### 5.4 Fills and repair

* **Source of truth: the user data stream on the WebSocket API.** Call `userDataStream.subscribe` after `session.logon`. The listenKey endpoints were removed on 2026-02-20. Use `executionReport` for fills, including `commission` and `commissionAsset`. Use `outboundAccountPosition` for balances.
* **Per-cycle outcome:**
  * all legs filled: done;
  * nothing filled: costs only order-count budget;
  * partial: compute each asset's delta against the plan and hand it to the rebalancer.
* **Rebalancer v1.** If any asset drifts outside its band, flatten it via the cheapest route (`best_route`) with IOC and a slippage cap. Otherwise leave it.
* **Reconcile.** Check balances with `account.status` periodically, and reconcile daily against `myTrades`.

### 5.5 Later: maker legs

Rest a maker order on one leg at a price where a fill makes hedging the other legs as taker profitable, then fire the hedge legs IOC on the fill.

* **Fee saving.** At VIP 2 this saves 1.1–1.5 bps of fee per maker leg (§3.2).
* **Spread capture.** On the tight USDT/USDC books it captures almost nothing; on the slow cross pairs it captures 1–3 bps.
* **The bigger lever.** A resting order can sit at a price that is profitable if filled, instead of waiting for the touch to line up.
* **The catch.** It fills mostly when the market moves through it (adverse selection), so the hedge legs may have moved too.
* **Practicalities.** It needs an STP mode on the resting order and fast amend or cancel ("order amend keep priority" is available on all symbols).

Estimate fill rates and post-fill markouts from recorded `trade` streams in Phase 0 before building it. §13.1 covers market making in general.

## 6. Market data

### 6.1 Streams

| | Top of book | Depth | Notes |
|---|---|---|---|
| JSON, `stream.binance.com:9443` | `<s>@bookTicker`, real-time, no event-time field | `<s>@depth20@100ms` snapshots, or `<s>@depth@100ms` diffs on top of a REST snapshot | No key needed. This session's sample used the market-data-only mirror, `data-stream.binance.vision`. |
| **SBE**, `stream-sbe.binance.com:9443` | `<s>@bestBidAsk`, real-time, µs event time; under load it drops stale events instead of queueing them | `<s>@depth20` every 50 ms; `<s>@depth` diffs every 20 ms | Needs an Ed25519 API key (no permissions required). Binary, so cheaper to decode. |

Recommendation: let SBE `bestBidAsk` drive the screen, and keep local books from SBE diff depth for the depth walk (`depth20` is fine for v1). When `bestBidAsk` moves through levels that depth still shows, drop the crossed levels.

### 6.2 Consistency and staleness

* There is no cross-symbol snapshot. Track (last update id, exchange event time, local receive time) for each symbol.
* Time since a book's last update is a poor staleness signal on its own. ETHBTC updated about once a second in the sample, so a quiet book is legitimately old. Judge trust from connection health instead: heartbeats, gaps in diff update ids, and event-time lag against your clock.
* Clock: run chrony against the Amazon Time Sync Service. Exchange event time minus local receive time then gives one-way lag.

### 6.3 Throughput and limits

* The sample (§12) was a quiet Sunday, and BTCUSDT still burst to ~1,000 top-of-book updates in a single second. Volatile markets will be much higher. Size the decode→screen path for bursts, watch queue depth, and conflate to the latest top of book when behind.
* Stream limits: 1,024 streams per connection, 300 connection attempts per 5 min per IP, 5 incoming control messages per second per connection, and 24 h connection lifetime. The server pings every 20 s.
* Redundancy: run 2–3 identical connections, take whichever message arrives first, and dedupe by update id. This cuts tail latency.

## 7. Rate limits

Live values from `exchangeInfo`, 2026-10-04:

| Limit | Value | Counted per |
|---|---|---|
| REQUEST_WEIGHT | 6,000 / min | IP |
| ORDERS | 100 / 10 s | account |
| ORDERS | 200,000 / day | account |
| RAW_REQUESTS | 300,000 / 5 min | IP |

* **Request weight.** Since 2026-04-02, successful order place, cancel and amend requests cost **0 request weight**; failed ones are still charged. Request weight now mostly matters for REST polling (avoid it) and account or commission queries.
* **Unfilled orders.** The ORDERS limit counts unfilled orders, and fills decrement the count. IOC legs that expire unfilled are what you pay for. A 3-leg cycle that misses completely uses 3 of the 100 per 10 s, so you get at most ~33 complete misses per 10 s.
* **Daily cap.** 200,000 per day averages ~2.3 orders/s. It's a real budget if you fire often and miss often.
* **Budgeting.** Keep client-side token buckets that mirror both ORDERS windows, resynced from the `rateLimits` array in every WebSocket API response. Reserve ~30% for repair orders, and fire a cycle only if all its legs fit.
* **Errors.** On HTTP 429, back off; ignoring repeated 429s escalates to an IP ban (418). Put a hard circuit breaker on order placement so a bug can't loop.

## 8. Latency: running from Tokyo

### 8.1 Placement

Binance's spot gateways and matching are widely reported to run in AWS Tokyo (ap-northeast-1). AZ names (1a/1c/1d) map to different physical zones in each AWS account, so compare by **AZ ID** (apne1-az1/az2/az4). Launch a small network-optimised instance in each AZ ID and measure:

* `order.test` round-trip on the WebSocket API;
* SBE `bestBidAsk` event time → local receive time;
* p50, p99 and p99.9 of both.

Pick the best AZ. Don't trust published latency figures; measure.

### 8.2 Tick-to-trade budget

| Stage | Plain Python (measured in this session) | Compiled target |
|---|---:|---:|
| Decode one bookTicker / depth20 JSON message | 1.5 µs / 5 µs (`json.loads` only) | SBE, ≪ 1 µs |
| Re-score the 30 affected cycles | ~40 µs | < 1 µs |
| Depth walk for a survivor, 20 levels | 44–78 µs worst case | < 2 µs |
| Build and send k orders (no signing after `session.logon`) | measure | measure |
| Network + gateway + matching | measure | measure |

The simulation itself is small compared with the network. In a race, though, every 100 µs counts, which is why the live engine should be C++ (§8.3).

### 8.3 C++ engine

* **Keep the Python core as the oracle.** Port `arb_core` first, then run both implementations over the same recordings in CI. Cycle sets, edges and sizes must match (to ~1e-9). The live engine should also write its captures in the recorder's JSONL format, so `analyze_recording.py` can replay them.
* **Hot path.**
  * One thread pinned to an isolated core, busy-polling non-blocking sockets.
  * No allocation or locks after startup.
  * Books as fixed arrays of levels.
  * Prices and quantities as integer ticks and steps (`int64`), so order parameters never pick up float rounding. Use `double` only for edge math.
* **Market data.**
  * SBE from `stream-sbe.binance.com`. Generate decoders from Binance's published schema with the SBE tool (`real-logic/simple-binary-encoding`); Binance's C++ SBE sample app shows the usage.
  * The user data stream and API responses can be SBE too.
  * JSON (`simdjson`) only where SBE isn't available.
* **Order entry.**
  * WebSocket API with `session.logon` (Ed25519 via libsodium or OpenSSL 3), so there's no per-request signing.
  * Boost.Beast + OpenSSL is the well-trodden starting point; benchmark it against FIX before optimising further.
* **Off the hot path.** Logging and recording through a lock-free single-producer/single-consumer ring buffer to a writer thread, plus the rebalancer, reconciliation and metrics.
* **Clocks.**
  * `CLOCK_MONOTONIC` for internal latency.
  * `CLOCK_REALTIME`, disciplined by chrony with Amazon Time Sync, for exchange-lag measurements.
* **Build.** CMake, `-O3 -march=native` on the target instance type, sanitizers in CI, and GoogleTest or Catch2.

## 9. Risk controls

* **Pre-trade:** packet cap, per-asset inventory bands, max cycles in flight, max legs (3 to start), hurdle plus buffer, rate-limit budget, data-health gate.
* **Post-trade:** drift limits, a daily loss limit marked to market in USDT, breakers on consecutive partial fills and on error rate, and a BNB float alarm.
* **Kill switch:** stop firing and cancel any resting orders. Optionally flatten to target inventory.
* **Keys:** trade-only, withdrawals disabled, IP-restricted, Ed25519. Secrets stay out of the repo.
* **Exchange changes:** re-fetch `exchangeInfo` periodically and after any filter error. Handle status changes and maintenance windows.
* **Perps (§13):** a perp leg can be liquidated on a sharp move even when the spot leg offsets it. Use low leverage, alarms on margin ratio, and a cap on total perp notional. Watch for funding flips.
* **Making (§13.1):** per-pair inventory limits, quote-skew rules, and a breaker that pulls quotes when recent realized spread turns negative or fair-value inputs go stale.

## 10. Architecture sketch

```
 SBE streams ─► feed handler ─► book store ─► screener (30 cycles/update) ─► sizer ─► risk gate ─► order gateway (WS API) ─► Binance
                                                                                          ▲                │
 user data stream (WS API) ─────────────────────────────► positions / fills ◄─────────────┴────────────────┘
                                                                  │
                                                  treasury / rebalancer (bands, BNB float, open-path routing)
 recorder (raw messages + local timestamps) ─► replay / backtest
```

The hot loop runs on one thread with no locks. The treasury and recorder run off the hot path. Market making and perps (§13) add a quoting engine beside the screener, a perp order gateway, and a treasury that tracks delta per coin across spot and perps.

## 11. Phased plan

| Phase | What | Exit criterion |
|---|---|---|
| **0. Measure** | Run the Python recorder on a Tokyo instance for 1–2 weeks, including volatile days. JSON streams answer this question; no C++ is needed yet. Replay with `--fees "7.5,USDCUSDT=0,BTCUSDC=7.125,ETHUSDC=7.125,BNBUSDC=7.125" --packet-usdt 100`. Also record `aggTrade` streams for markouts (§13.1), and perp `bookTicker` / `markPrice` streams (§13.2). Run `basis_funding.py` monthly. Measure latency per AZ (`latency_probe.py`). Step-by-step set-up: [`ops/README.md`](ops/README.md). | Enough episodes per day above ~15–17 bps (the 14.6 bps hurdle plus a buffer), lasting longer than your measured round-trip, at sizes that make cents-per-trade worth running. **Otherwise stop, or revisit fees (higher tier, maker legs).** |
| **1. Shadow** | Build the C++ feed handler, books, screen and sizer (§8.3), golden-tested against the Python core. Run them live and log would-be orders without sending them. Score each against the books seen 1, 5 and 20 ms later (would it have filled?). | Shadow P(fill) and P&L agree with the Phase 0 replay. |
| **2. Small live** | Add the C++ order gateway. ~$100 packets, cross-quote triangles only, one cycle in flight, IOC. Benchmark the WebSocket API against FIX. | Fill rates, slippage and repair costs inside the Phase 1 model. |
| **3. Scale** | More inventory if Phase 0–2 found the size, concurrent cycles with depth allocation, other cycle families, the maker-leg variant, more assets. | Each step pays for its added risk. |

Use the Spot Testnet (`testnet.binance.vision`) only for functional tests of order flow. Its books aren't the real market.

## 12. Measured in this session (2026-10-04)

Measured with `research/record_streams.py` and `research/analyze_recording.py` on JSON streams. **Caveats:** a quiet Sunday morning, measured from this sandbox rather than Tokyo, and a short window. Treat it as a sanity check, not as Phase 0.

Window: 09:25–09:55 UTC (30 min), with 237,965 top-of-book updates and 102,259 depth20 snapshots across the 10 symbols. A separate 7-minute window just before (09:19–09:26) gave the same picture.

**Update rates and spreads**

| Symbol | Updates/s (mean) | Busiest second | Median spread (bps) |
|---|---:|---:|---:|
| BTCUSDT | 55.5 | 990 | 0.00 |
| ETHUSDT | 22.8 | 415 | 0.04 |
| BNBUSDT | 18.2 | 322 | 0.13 |
| USDCUSDT | 4.3 | 68 | 0.10 |
| BTCUSDC | 12.3 | 243 | 0.00 |
| ETHUSDC | 7.4 | 137 | 0.04 |
| BNBUSDC | 7.3 | 87 | 0.13 |
| ETHBTC | 1.6 | 71 | 3.15 |
| BNBBTC | 1.9 | 116 | 1.08 |
| BNBETH | 1.0 | 40 | 3.41 |

**How often any cycle was net-positive at the touch** (top-of-book replay over all 74 cycles)

| Fee per leg | 3-leg hurdle | Episodes | Per hour | % of time | Duration median / p90 / max (ms) | Median peak net edge (bps) | Median touch size (USDT) | Best cycle had 3 / 4 / 5 legs |
|---:|---:|---:|---:|---:|---|---:|---:|---|
| 1 bps | 3 bps | 81 | 162 | 2.4 | 7 / 214 / 13,795 | 0.39 | 434 | 68 / 13 / 0 |
| 2 bps | 6 bps | 5 | 10 | 0.001 | 4 / 5 / 5 | 0.93 | 644 | 5 / 0 / 0 |
| 5 bps | 15 bps | 0 | 0 | 0 | – | – | – | – |
| **7.5 bps (your USDT-pair taker)** | **22.5 bps** | **0** | 0 | 0 | – | – | – | – |
| 10 bps | 30 bps | 0 | 0 | 0 | – | – | – | – |

With zero fees, some cycle was gross-positive for the whole window. The touch is always slightly mispriced somewhere, by less than any taker's fee.

**Which update opened and closed each episode**

| Fee per leg | Opened by | Closed by | Episodes | Median duration (ms) |
|---|---|---|---:|---:|
| 1 bps | BNBUSDC | BNBUSDC | 46 | 6 |
| 1 bps | BNBUSDT | BNBBTC | 6 | 6 |
| 1 bps | BNBUSDC | BNBETH | 5 | 214 |
| 1 bps | BNBUSDC | BNBBTC | 4 | 5 |
| 1 bps | BNBBTC | BNBBTC | 3 | 5 |
| 2 bps | BNBUSDT | BNBUSDT | 3 | 0 |
| 2 bps | BNBUSDT | BNBBTC | 1 | 4 |
| 2 bps | BNBUSDC | BNBUSDC | 1 | 4 |

**Cycles with the largest gross edge (before fees)**

| Cycle | Legs | Max gross edge (bps) | % of time gross > 0 |
|---|---:|---:|---:|
| USDT→BTC→USDC→BNB→USDT | 4 | 9.96 | 74.5 |
| USDT→ETH→USDC→BNB→USDT | 4 | 9.34 | 48.7 |
| USDT→USDC→BNB→USDT | 3 | 9.31 | 39.7 |
| USDT→BTC→ETH→USDC→BNB→USDT | 5 | 9.30 | 25.7 |
| USDT→ETH→USDC→BTC→BNB→USDT | 5 | 9.21 | 26.0 |
| USDT→BTC→BNB→USDT | 3 | 9.07 | 41.6 |
| USDT→ETH→BNB→USDT | 3 | 7.96 | 10.8 |

**Depth-optimal size and profit** (depth20 replay, `size_cycle` with no packet cap)

| Fee per leg | Snapshots with a net-positive cycle | Optimal size median / p90 / max (USDT) | Profit median / p90 / max (USDT) |
|---:|---:|---|---|
| 1 bps | 2,709 (2.65%) | 944 / 28,035 / 48,992 | 0.03 / 0.68 / 6.74 |
| 2 bps | 17 (0.02%) | 1,082 / 11,778 / 12,519 | 0.06 / 1.03 / 1.20 |
| 5 bps and up | 0 | – | – |

**At your VIP 2 fees** (same 30 minutes, replayed with `--fees "7.5,USDCUSDT=0,BTCUSDC=7.125,ETHUSDC=7.125,BNBUSDC=7.125" --packet-usdt 100`)

No cycle was ever net-positive. The closest approaches:

| Cycle | Legs | Hurdle (bps) | Max gross (bps) | Max net (bps) |
|---|---:|---:|---:|---:|
| USDT→USDC→BNB→USDT | 3 | 14.63 | 9.31 | −5.32 |
| USDT→ETH→USDC→USDT | 3 | 14.63 | 2.89 | −11.74 |
| USDT→BTC→USDC→USDT | 3 | 14.63 | 2.61 | −12.03 |
| USDT→BNB→USDC→USDT | 3 | 14.63 | 2.56 | −12.07 |
| USDT→USDC→BTC→BNB→USDT | 4 | 22.13 | 9.07 | −13.07 |
| USDT→BTC→BNB→USDT | 3 | 22.51 | 9.07 | −13.44 |

* **BTC and ETH stayed aligned.** Their USDT and USDC books never drifted more than about 3 bps apart.
* **Only BNB showed a real basis,** and it peaked at 9.3 bps.
* **$100 packets earn cents even at lower fees.** With sizes capped at $100, a hypothetical 2 bps-per-leg schedule would have earned about $0.02 per trade.

**What it means**

* **At your fees nothing was net-positive.** The cheapest cycles need 14.6 bps. The best gross edge among them was 9.3 bps (BNB); BTC and ETH never exceeded about 3 bps.
* **At 2 bps per leg the best case was tiny.** There were five windows of 5 ms or less in 30 minutes, all in the first 13, each worth about $1 at full depth. Catching them takes both top-tier fees and sub-5 ms reaction.
* **Almost every episode involved BNB,** usually a better BNB quote that lived only a few milliseconds. The persistent USDT/USDC basis on BNB is the most interesting lead, and a natural subject for the maker-leg study (§5.5).
* **This was a quiet Sunday morning, measured outside Tokyo.** Volatile periods produce larger and more frequent dislocations, and also more competition. Phase 0 exists to measure exactly that.

## 13. Beyond taker cycles: market making, perps and basis

You've put three more things in scope: making markets (working towards a maker rebate), spot vs perpetual basis, and perps as hedges. They all run on the same graph and simulator. What differs is where the edge comes from and what it costs.

| Strategy | What the graph contributes | Edge comes from | Main costs | At your fees today |
|---|---|---|---|---|
| Taker cycles (§2–§5) | finds and sizes mispriced cycles | quotes that are briefly wrong | 2–3 taker fees per cycle | none net-positive in 30 min (§12) |
| Market making (§13.1) | a fair value for every pair, and the cheapest route to offload inventory | spread capture; rebates once in a maker programme | maker fees, adverse selection, inventory | needs a maker fee of 0 or less |
| Spot vs perp carry (§13.2) | perps as extra edges: exposure without conversion | funding, plus basis convergence | spot + perp fees, margin, funding flips | ~2.5–3.6% a year gross on BTC/ETH over the last year |
| Perps as hedges (§13.3) | delta per coin = spot + perp | – | perp fees, margin | short hedges have earned funding |

### 13.1 Market making: which pairs the graph says to make

**Fair value: read it from the USDT books.** Price every asset off its USDT book and take fair = p(base) / p(quote). For example, ETHBTC's fair value is ETHUSDT / BTCUSDT and BTCUSDC's is BTCUSDT / USDCUSDT; a USDT pair's fair value is its own mid. The slow cross pairs and the USDC books show their fair value in the fast USDT books before their own quotes move, and that is what protects a maker from being picked off.

An earlier version of this plan used the median of each pair's three two-leg routes. You asked whether tilting towards USDT does better, so I tested five estimators on the same 20 minutes of trades (`compare_fair_values.py`). Each fill is marked by its value in USDT terms 10 s later, because the plan offloads inventory through the USDT books or perps priced off them. The table assumes a maker had been on every fill where its quote was at least N bps better than that estimator's fair value 20 ms before the trade, and sums the P&L of those fills over all ten pairs (USDT, before fees). It is for comparing estimators, not a P&L forecast.

| Fair value | N = 0 | N = 1 bps | N = 2 bps | Cross pairs only, N = 1 |
|---|---:|---:|---:|---:|
| The pair's own mid | −1,318 | −39 | +17 | +11 |
| Median of the three routes (earlier plan) | −763 | −408 | −34 | +28 |
| **USDT as the source of truth** | −1,113 | **−2** | **+33** | **+28** |
| Routes, weighted by each route's traded volume | **−406** | −66 | +19 | +28 |
| Routes plus the pair's own book, volume-weighted | −510 | −23 | +32 | +28 |

* **USDT as the source of truth wins at the thresholds a maker would actually quote at (N ≥ 1 bps).** The route median was fooled on the big books: it kept 28% of BTCUSDT's fills, at −1.28 bps. USDT-based fair value treats a USDT pair's own book as the truth and doesn't make that mistake. On BTCUSDC it kept 14% of fills at +0.86 bps, where the route median kept 25% at −0.23 bps.
* **On the cross pairs every graph estimator ties.** Their three routes mostly agree, and the USDT route dominates any volume weighting anyway.
* **The USDT books lead, most clearly for BNB.** Within 10 s, BNBUSDC's mid closed 47% of its gap to the USDT-based fair value and 60–73% of the gap to the volume-weighted ones, but only 9% of the gap to the route median. BNBUSDT moved slightly away from the other books' implied price: it leads them. For BTC and ETH the effect was weaker (BTCUSDC and ETHUSDC closed only 3–15% of any gap).
* **Volume-weighted routes are the better filter only at N = 0,** because they also filter the USDT pairs using the other books. A maker demanding no edge at all shouldn't be quoting, so this doesn't matter in practice. Keep the volume-weighted blend (routes plus own book) as the Phase 0 alternative to USDT.
* **Exit through the deep books.** Marked instead to each pair's own mid 10 s later (exit on the same book), the cross pairs' filtered fills earned about half as much. Offload cross-pair inventory through the USDT books or perps, not by round-tripping the thin book.

**Hedging every fill at once doesn't pay.** The test: quote at the touch and flatten each fill immediately through the best other path. That never had a positive edge in 30 minutes, even with a −0.8 bps rebate; the best case was −4.7 bps (BNBUSDC). The hedge leg's taker fee (7.1–7.5 bps) is bigger than the spreads and the usual USDT/USDC basis; the basis only beat it in brief spikes. So making has to be inventory-based: earn on many fills and offload only the net position, cheaply (perps at 2–5 bps, or passively).

**An unhedged quote at the touch, against fair value.** Same 30 minutes, before adverse selection. The last three columns show the share of time the edge beats each maker fee (range across bid and ask).

| Pair | Spread (bps) | Median edge per fill, bid / ask (bps) | Beats VIP 2 maker (6 bps) | Beats 0 (maker programme) | Beats −0.8 bps (top rebate) |
|---|---:|---|---:|---:|---:|
| BNBETH | 3.41 | 0.93 / 2.97 | 0.5–3% | 70–89% | 81–95% |
| ETHBTC | 3.15 | 1.72 / 1.44 | 0% | 77–88% | 89–96% |
| BNBBTC | 1.08 | 1.24 / 0.26 | 0% | 57–73% | 84–89% |
| BNBUSDC | 0.13 | 0.10 / 0.03 | 0% | 55–56% | 81–96% |
| BTCUSDC | 0.00 | −0.57 / 0.58 | 0% | 20–81% | 70–99% |

On the USDT pairs and USDCUSDT, fair value is the pair's own mid, so a quote at the touch is worth exactly half the spread (at most 0.06 bps).

**What makers actually earned: the realized spread of real fills.** A separate recording of trades and top-of-book, 10:32–10:52 UTC (20 minutes, ~18,700 aggregated trades). For each trade, this is the passive side's P&L in USDT terms 10 s later, notional-weighted, before maker fees (`analyze_making.py --trades`, USDT fair value):

| Pair | Fill notional (20 min) | All fills, +10 s (bps) | Only fills ≥ 1 bps better than fair: share kept / +10 s (bps) |
|---|---:|---:|---|
| ETHBTC | $186k | +0.97 | 36% / +2.39 |
| BNBBTC | $133k | −1.38 | 26% / +2.59 |
| BNBETH | $54k | −1.66 | 33% / +1.91 |
| BNBUSDC | $263k | −0.82 | 7% / +0.19 |
| BNBUSDT | $814k | −0.92 | 16% / +0.63 |
| BTCUSDC | $2.19M | −0.79 | 14% / +0.86 |
| BTCUSDT | $8.51M | −0.73 | 4% / −0.15 |
| ETHUSDT | $2.69M | −1.02 | 10% / −2.20 |
| ETHUSDC | $524k | −1.77 | 8% / −1.21 |
| USDCUSDT | $28.9M | −0.02 | 0%; at ≥ 0 bps: 95% / −0.02 |

* **The average maker at the touch lost money to informed flow.** On the coin pairs other than ETHBTC, a passive fill was worth −0.7 to −1.8 bps ten seconds later, before fees. On BNBBTC the loss was there at the moment of the fill: those trades are mostly arbitrageurs picking off stale quotes, the same flow the taker-cycle engine would be part of.
* **Quoting off USDT fair value keeps the good fills.** Fills where the quote was already at least 1 bps better than fair value earned +1.9 to +2.6 bps on the three cross pairs, on 26–36% of their flow. That is the case for making those pairs: re-quote off the USDT books and never be the stale quote.
* **On the big books the filter keeps little.** It kept 4–16% of their fills, earning −2.2 to +0.9 bps. There, informed flow is as fast as the USDT books themselves.
* **USDCUSDT has nothing to earn.** It's fee-free for you, but fills there were worth about zero (−0.02 bps), and the book is very deep at each price, so queue position would decide whether you'd get them at all.
* **None of this survives a 6 bps maker fee** (or VIP 0's 7.5). At 0 (the maker programme), making ETHBTC, BNBBTC and BNBETH off USDT fair value looks positive before queue effects. The cross pairs had only 37–255 trades in the window, so treat these as directions for Phase 0 to measure, not estimates.

**Ranking for v1 making**

1. **ETHBTC, BNBBTC and BNBETH.** One-tick spreads worth 1–3.4 bps, slow quotes, and a fair value visible in the USDT books. Filtered fills there earned +1.9 to +2.6 bps. They are small markets ($7–14M a day), so capacity is limited.
2. **The USDC books, against their USDT twins** (BTCUSDC, ETHUSDC, BNBUSDC). The USDT book gives a sharp fair value and USDCUSDT makes inventory moves free. But there is little spread to earn, and filtered fills were mixed (−1.2 to +0.9 bps), so this only works with a rebate.
3. **The big USDT books.** There is no spread to earn. This is a rebate and queue-position business for later, with low-latency infrastructure.
4. **USDCUSDT.** It is fee-free and the busiest book ($1.2B a day), but fills there were worth about zero and there is no rebate. Zero-fee volume has historically not counted towards VIP tiers. Use it for rebalancing, not as a making target.

**Perps may be the cheaper place to make.** Your futures maker fee (0.02%) is a third of spot's. Perps also trade 4–12× spot's volume (7-day average: BTCUSDT $11.4B a day against $1.4B on spot; ETHUSDT $8.0B against $0.7B). And the futures liquidity-provider programme has paid rebates (up to 0.3 bps in its last published terms). The graph still supplies fair value: spot, plus the basis. The same markout method applies, but futures data is blocked from this sandbox, so it's a Phase 0 measurement from Tokyo.

**Getting the maker fee to zero or below**

* **Binance's spot maker programme:** 0 maker fee, with rebates up to about 0.8 bps at the top tiers. You qualify by weekly maker-volume share, or by application (historically from about $20M a month of spot volume).
* **Altcoin LiquidityBoost:** 0.5 or 1 bps rebate at 0.5% or 1% of weekly maker volume across 40 altcoin pairs. The absolute bar is lower, but the pairs are outside this graph.
* **Scale check.** $20M a month is about $670k a day, or ~6,700 fills of $100. At VIP 2's 6 bps maker fee that costs about $400 a day in fees, so the volume has to pay for itself first or come from a larger book. Build the making engine now and run it in shadow, or at tiny size. Then its P&L at zero fee is known by the time the programme is within reach.

**Mechanics**

* **Orders.** Use post-only (`LIMIT_MAKER`) quotes around the USDT-based fair value plus a margin, skewed by inventory. Cancel and replace when fair value moves; amend-keep-priority only reduces size. Set an STP mode.
* **Inventory.** Offload the net position through perps or the cheapest route once it leaves its band.
* **Order budget.** The unfilled-order limits (§7) bind much harder here than for taker cycles, because every quote that doesn't fill counts. 100 per 10 s and 200,000 a day average out to about 2.3 new orders a second across all your quotes.
  * In the quiet 30-minute sample, each cross pair's USDT-based fair value moved 0.5 bps 7–18 times a minute, and 1 bps 3–8 times (`analyze_making.py`, re-quote load).
  * Re-quoting both sides of all three cross pairs on every 0.5 bps move would take ~84 orders a minute (1.4/s); on 1 bps moves, ~37 a minute.
  * Volatile days will be several times busier, so make the re-quote threshold adapt to the budget left. Quote few pairs.
  * The maker programme can bring higher limits; ask your account manager.

### 13.2 Spot vs perp: carry and basis

Measured from Binance's public archive with `research/basis_funding.py`.

**Funding over the last 12 months** (Oct 2025 – Sep 2026, paid 8-hourly, positive = longs pay shorts)

| Perp | Mean per day | Annualised | Payments positive | Worst 7 days | Best 7 days |
|---|---:|---:|---:|---:|---:|
| BTCUSDT | 0.92 bps | 3.4% | 76% | −12.1 bps | +19.6 bps |
| BTCUSDC | 0.99 bps | 3.6% | 78% | −10.8 bps | +18.5 bps |
| ETHUSDT | 0.69 bps | 2.5% | 74% | −13.5 bps | +20.3 bps |
| ETHUSDC | 0.74 bps | 2.7% | 71% | −11.6 bps | +18.1 bps |
| BNBUSDT | 0.54 bps | 2.0% | 40% | −19.4 bps | +20.8 bps |
| BNBUSDC | 0.95 bps | 3.4% | 59% | −7.0 bps | +18.5 bps |

**Basis, perp vs spot** (1-minute closes, 27 Sep – 3 Oct 2026)

| Pair | Median | p5 / p95 | Typical 1-hour change |
|---|---:|---|---:|
| BTCUSDT | −4.7 bps | −5.8 / −3.4 | 0.7 bps |
| ETHUSDT | −4.6 bps | −5.8 / −3.3 | 0.6 bps |
| BNBUSDT | +4.9 bps | +2.1 / +7.3 | 0.7 bps |
| BNBUSDC | +6.1 bps | +4.2 / +8.4 | 1.0 bps |

BTCUSDC and ETHUSDC were within 0.2 bps of their USDT twins.

What it means:

* **Cash-and-carry (long spot, short perp) is slow and modest right now.** It earned about 0.7–1 bps a day on BTC and ETH.
  * Getting in and out costs 16 bps with maker orders on both legs (2 × (0.06% + 0.02%)), or 25 bps with takers. That is 2½–4 weeks of funding just to break even, and a bad week gives back 10–19 bps.
  * Run it as a regime trade: switch it on when funding runs well above this level (bull markets have paid far more) and off when it fades. Compare against what idle USDT and USDC would earn elsewhere.
* **Low funding and the perp discount are the same fact.** Binance's funding formula adds a fixed 0.01% interest term per 8 h unless the premium is far from it. A premium near −0.047% clamps the adjustment and leaves about 0.3 bps per payment, about the 0.9 bps a day measured.
* **Intraday basis trading isn't viable at these fees.** The basis moves under 1 bps an hour and spans a few bps all week, against 16–25 bps of round-trip costs.
* **The entry basis matters.** BTC and ETH perps sat about 4.7 bps below spot, so a carry trade opened then pays that discount back if the gap closes. BNB is the other way round.

### 13.3 Perps as hedges, and in the engine

* **Inventory hedge.** Short perps against the BTC/ETH/BNB working inventory make it delta-neutral. Over the last year that hedge *earned* the funding above, because shorts receive when funding is positive. Prefer BNBUSDC to BNBUSDT for the BNB hedge (positive 59% of the time against 40%).
* **Not a conversion.** A perp is not an edge that converts one asset into another; it adds exposure without moving any asset. The engine tracks delta per coin = spot balance + perp position. The treasury keeps each coin's delta inside a band using whichever is cheaper: a spot route through the graph, or a perp order.
* **Margin and liquidation.** The perp leg can be liquidated on a sharp move even when the spot leg offsets it. Keep leverage low, hold margin in USDT/USDC, and alarm on margin ratio. Portfolio margin, if your account has it, nets spot against perps.
* **Market data.** The futures API (`fapi` / `fstream`) is blocked from this sandbox, but not from a Tokyo instance. Add `<s>@bookTicker`, `<s>@markPrice@1s` (mark price and funding) and `<s>@aggTrade` for the six perps to Phase 0.

### 13.4 Order of work

| Track | Phase 0 (now, Python) | Next | Go / no-go |
|---|---|---|---|
| Taker cycles | Record spot books from Tokyo; replay at your fees | C++ engine (§8.3, §11) | Episodes above ~15–17 bps that outlast your round-trip |
| Market making | Record spot books and `aggTrade`; realized spread per pair at 0 maker fee | Quoting engine in C++, in shadow on ETHBTC and BNBETH | Positive realized spread at a maker fee you can reach; a path into the maker programme |
| Hedge and carry | Run `basis_funding.py` monthly; record perp streams from Tokyo | Hedge the working inventory with perps; carry when funding clears your threshold | Funding regime, and margin set-up |
| Long-tail making (§16–§17) | Record the 87 candidates and their 61 perps from Tokyo; `markouts.py` per day, `anchors.py` on the archive | Quoting off the perp and the queues; queue-aware simulation, then a small live test | Positive realized spread after a 7.5 bps maker fee over a week, jumps included |

### 13.5 Fitting it to 15k AUD

Your answers settle the scale: 15k AUD in total, roughly US$10k at ~0.65 USD per AUD. Three things follow from it.

* **Perps: not available on an Australian retail account.** You've confirmed the account is Australian, and retail clients there generally can't trade Binance derivatives. ASIC cancelled Binance Australia Derivatives' licence in April 2023, and in 2026 a court penalised it A$10M for letting retail clients into derivatives. Only wholesale clients still can.
  * Unless you qualify as wholesale, plan spot-only: taker cycles, spot making, and inventory kept small instead of hedged. The carry trade, the perp hedges and perp market making (§13.2–§13.3) are off the table.
  * Keep recording public perp data anyway (the Phase 0 kit does). It costs nothing, perps carry most of the volume, and they may be a better source of fair value for spot than the USDT books. That's a Phase 0 test.
* **VIP 2 through borrowed BNB doesn't add up.**
  * BNB borrowed on margin doesn't count: Binance counts a margin account's *net* BNB, i.e. holdings minus borrowed BNB and interest.
  * BNB borrowed through Crypto Loans and held in spot does appear to count, since the FAQ nets only margin. But the loan needs collateral worth more than the ~US$20k of BNB borrowed, which is more than the whole 15k AUD, plus interest.
  * The VIP Borrower Program only raises borrowing limits; it doesn't grant trading-fee tiers.
  * VIP 2 also needs ≥ $5M of 30-day spot volume. At 0.075% that volume would cost about US$3,750 a month in fees unless it is already profitable.
  * So plan on VIP 0 with the BNB discount: 0.075% maker and taker, or 0.075% / 0.07125% on USDC pairs. Only the maker rate differs from the VIP 2 numbers, and no conclusion here depends on it. Taker cycles never cleared even 5 bps per leg, and making needs a maker fee of zero either way.
* **Portfolio margin probably isn't available yet.** Binance's last published requirement for regular users was 100,000 USDT across the cross-margin and futures wallets (April 2024). Without it, a perp hedge needs its own margin in the futures wallet: hedging $3k of coins at 3× leverage ties up about $1k more.

What the capital is for, at this size:

* **The edges measured so far are worth cents to a few dollars a day here.** The capital's main job is to fund Phase 0 and a careful start, not to earn yet. Keep most of it in stablecoins until a strategy passes its gate (§13.4).
* **Taker cycles** need about US$500 of working inventory (~$100 per asset) plus a small BNB fee float.
* **Making** stays in shadow until the maker fee is zero. The programme's ~$20M a month is about 2,000 turns of this whole book every month, so it only becomes reachable once a strategy is profitable per trade at your fees.
* **Carry** would be the one track that works at any size, but it needs perps (see above). Even then, last year's funding (~3% a year) earns about A$25 a month per A$10k.

## 14. Decisions so far, and open questions

Decided on 2026-10-04:

* **Account:** Australian, retail. Spot only unless you qualify as a wholesale client (§13.5). Perp data is still recorded for research.
* **Fees for planning:** VIP 0 with BNB. 0.075% maker and taker; USDC pairs 0.075% / 0.07125%; USDCUSDT free. VIP 2 isn't reachable at this size (§13.5).
* **Capital:** 15k AUD in total (§13.5). Start with ~$100 per coin of working inventory.
* **Live engine:** C++ (§8.3). Python stays as the research tooling and reference implementation.
* **Scope:** taker cycles and spot market making, working towards Binance's maker programme (§13).
* **Fair value for making:** read from the USDT books (§13.1); for long-tail books, from each coin's perp (§17).
* **Phase 0:** recorder on AWS Tokyo, set up with [`ops/README.md`](ops/README.md).
* **Account manager:** once the account is approved, ask them about the maker programme's requirements.

Open:

1. **Wholesale status:** only matters if you want the perps track back. It's your call whether to pursue it.
2. **API key:** once the account is approved, create an Ed25519 API key for later phases. Make it trade-only, with withdrawals disabled and access restricted to the trading instance's Elastic IP. Phase 0 doesn't need one.
3. **DEX–CEX:** park it, or record it alongside Phase 0 (§15)?
4. **The long tail:** Phase 0 now records the 87 wide-spread candidates. Decide after a week of markouts (§16).

## 15. DEX–CEX arbitrage

**The idea.** Buy on a DEX and sell on Binance, or the reverse, when their prices diverge. The two legs can't be atomic, and moving money between a chain and the exchange takes minutes and fees, so it needs inventory on both sides. That's the parallel model of §5 with a wallet as an extra venue. For this graph's assets the natural place to look is PancakeSwap on BNB Chain.

**Measured** with `research/dex_probe.py`: 10 minutes on 2026-10-04, from this sandbox. It compares the deepest PancakeSwap v3 pools with Binance's top of book, after the pool fee, a 0.075% Binance taker fee, and gas on a $1,000 swap.

| Pool (fee) | Gap vs Binance mid: median / p95 / max (bps) | Best edge after costs (bps) |
|---|---|---:|
| WBNB/USDT (0.01%) | 0.7 / 1.6 / 3.2 | −5.4 |
| WBNB/USDT (0.05%) | 3.7 / 5.3 / 6.3 | −6.3 |
| BTCB/USDT (0.05%) | 3.7 / 5.2 / 8.2 | −4.3 |
| ETH/USDT (0.05%) | 2.7 / 4.7 / 8.1 | −4.5 |
| WBNB/USDC (0.01%) | 0.8 / 2.3 / 3.5 | −4.8 |
| USDC/USDT (0.01%) | 0.7 / 0.7 / 0.7 | −0.4 (the Binance leg is fee-free) |

None of the 388 samples was profitable. Gas is negligible on BNB Chain, about 0.06 bps of a $1,000 swap. The obstacles are fees and competition:

* **The pools are already arbitraged against Binance every block.** The 0.01% pools sat within 1–3 bps of Binance; the 0.05% pools stayed inside their fee band. Searchers on top fee tiers pay a fraction of your exchange fee and bid block builders for priority, so a gap you can see has usually closed before your transaction lands.
* **Your 0.075% taker fee is most of the hurdle.** The best moment in 10 minutes was still 4.3 bps short.
* **It adds new risks and work.**
  * The two legs can't be atomic.
  * A hot wallet and token approvals have to be secured.
  * Your own swaps can be front-run.
  * Rebalancing means deposits and withdrawals.
  * For an Australian taxpayer, every swap is a capital-gains event to record.

**Verdict: not on the five majors at your fees.** The on-chain edges that do exist sit in riskier places:

* long-tail tokens and new pools: less competition, but thin liquidity, rugs and honeypots;
* volatility spikes, when pools lag for more than a block;
* providing liquidity rather than taking it, which is a different business with its own risks.

To keep the door open at no cost, run `dex_probe.py` from the Tokyo box for an hour or two during busy trading. Look again only if the gaps clear ~10 bps.

## 16. The long tail: where small capital might have the edge

**The thesis, and why nothing so far fitted it.** A strategy that can't net more than a few hundred dollars a day is worthless to a large firm and very attractive at 15k AUD. Edges like that live in markets too small to be worth a big firm's time. Everything tested in §12–§15 was the other kind. On the majors, the prize goes to whoever is fastest and pays the least in fees, whatever their capital, which is why none of it cleared VIP 0 fees. On Binance, the small-market version is making the long tail of spot pairs.

**The scan** (`research/scan_spreads.py`, 2026-10-04 15:03 UTC, six snapshots of every book 10 s apart). Of 1,372 trading spot pairs, 86 had a median spread above 15 bps (two VIP 0 maker fees) and $0.2–20M of daily volume.

* **5 can be priced off a USDT book.** They are books quoted in USDC, FDUSD or BTC, whose coin also trades on a USDT book with at least 10× the volume and at most a third of the spread: BEAMXUSDC, MUBARAKUSDC, TRXBTC, SUIFDUSD and 牛来USDC. That USDT book can price them, as it prices the cross pairs (§13.1).
  * The list moves with the snapshots. A scan a few minutes earlier also had ZECBTC: 30 bps wide, $2.4M a day, priced off ZECUSDT at 0.1 bps and $105M a day.
* **81 have only their own mid.** Many of their spreads are a single tick: PEPE (23.3 bps), BONK (26.1), AI (49.4), METIS (29.8). On those books the price can't improve, so makers compete on queue position, and a newcomer joins the back of the queue.
* **6 are sports fan tokens** (ACM, JUV, PSG, CITY, ATM, ALPINE). Their flow may come mostly from fans rather than traders, which is the kind a maker wants.

**What they could be worth, at most.** Suppose a maker's fills were 1% of each pair's volume, and each fill earned half the quoted spread less a 7.5 bps maker fee. The 86 pairs together would then pay about **$320 a day**. That's the "few hundred a day" scale, but only as a ceiling: it assumes no adverse selection.

| Pair | Median spread (bps) | 24 h volume | Fair value from | Ceiling at 1% of volume ($/day) |
|---|---:|---:|---|---:|
| PEPEUSDT | 23.3 | $11.6M | own mid | 48 |
| IOTAUSDT | 24.6 | $5.5M | own mid | 26 |
| GLMRUSDT | 17.9 | $12.1M | own mid | 18 |
| AIUSDT | 49.6 | $0.9M | own mid | 16 |
| QIUSDT | 26.9 | $2.6M | own mid | 15 |
| BONKUSDT | 26.1 | $2.3M | own mid | 13 |
| GUNUSDT | 29.5 | $1.7M | own mid | 12 |
| ALPINEUSDT | 27.5 | $1.8M | own mid | 11 |
| PARTIUSDT | 36.3 | $1.0M | own mid | 11 |
| BANKUSDT | 35.0 | $1.0M | own mid | 10 |
| BEAMXUSDC | 45.2 | $0.4M | BEAMXUSDT (3.8 bps, $16.6M) | 6 |
| MUBARAKUSDC | 27.1 | $0.6M | MUBARAKUSDT (4.6 bps, $13.0M) | 3 |
| TRXBTC | 25.4 | $0.5M | TRXUSDT (3.0 bps, $13.9M) | 3 |

**What makers actually earned** (`research/markouts.py`). For every trade, the passive side is credited the move from the trade price to the pair's mid 1 s, 10 s and 60 s later, notional-weighted. That is what a maker filled in that trade made before fees, if it could unwind at the mid. It flatters a newcomer, who sits at the back of the queue and tends to be filled only when a price level is cleared. Two live recordings on Sunday 2026-10-04, from this sandbox:

| Recording | Pairs | Trades | Fills (USDT) | At fill | +1 s | +60 s | +60 s after a 7.5 bps maker fee |
|---|---:|---:|---:|---:|---:|---:|---:|
| 14:34–14:49 UTC | 14 | 606 | $349k | +11.6 | −5.0 | −4.9 | −12.4 |
| 14:58–15:19 UTC | 25 | 2,361 | $732k | +12.1 | −0.4 | +0.7 | −6.8 |

The second recording, pair by pair (bps; the last column is how far the mid moved over the 21 minutes):

| Pair | Trades | Fills (USDT) | At fill | +60 s | After fee | Price move |
|---|---:|---:|---:|---:|---:|---:|
| PEPEUSDT | 326 | 310k | +10.8 | −2.1 | −9.6 | +23 |
| IOTAUSDT | 366 | 167k | +9.6 | +4.4 | −3.1 | −163 |
| GLMRUSDT | 538 | 50k | +20.6 | −1.8 | −9.3 | −53 |
| BONKUSDT | 273 | 40k | +12.8 | −0.4 | −7.9 | +52 |
| WLFIUSDT | 41 | 38k | +9.0 | −3.0 | −10.5 | 0 |
| BANKUSDT | 98 | 23k | +16.9 | −2.8 | −10.3 | +35 |
| MUBARAKUSDC | 72 | 16k | +10.6 | +26.4 | +18.9 | +119 |
| ILVUSDT | 75 | 14k | +16.7 | +6.6 | −0.9 | −48 |
| STXBTC | 24 | 6.6k | +9.6 | −17.4 | −24.9 | +177 |
| AIUSDT | 29 | 5.4k | +47.5 | +46.5 | +39.0 | −25 (half a tick) |
| ZECBTC | 34 | 4.8k | +4.3 | −7.4 | −14.9 | +39 |
| ACMUSDT | 35 | 1.4k | +35.4 | +35.4 | +27.9 | 0 |
| PSGUSDT | 60 | 1.1k | +21.6 | +21.7 | +14.2 | 0 |
| ADXUSDT | 37 | 1.1k | +15.1 | +15.1 | +7.6 | 0 |

The books not quoted in USDT, marked to their USDT anchor instead of their own mid (second recording; TRXBTC traded once and is left out):

| Pair | Trades | Fills (USDT) | All fills, +60 s | Fills ≥ 5 bps better than the anchor: share / +60 s |
|---|---:|---:|---:|---|
| MUBARAKUSDC | 72 | 15.7k | +27.3 | 73% / +42.7 |
| PEPEUSDC | 64 | 12.4k | −5.9 | 79% / −7.5 |
| IOTAUSDC | 34 | 8.0k | +1.1 | 92% / +1.7 |
| STXBTC | 24 | 6.6k | −17.0 | 72% / −12.6 |
| ZECBTC | 34 | 4.8k | −11.3 | none; only 6% were even ≥ 0 bps better, at −15.6 |
| BEAMXUSDC | 15 | 1.9k | +31.6 | 94% / +33.0 |
| ESPUSDC | 14 | 0.8k | −1.6 | 30% / −0.0 |
| **All, before fees** | 258 | 50.2k | +5.1 | 71% / +12.0 |
| **All but MUBARAKUSDC, before fees** | 186 | 34.5k | −4.9 | 70% / −2.6 |

**What it means**

* **The spread mostly pays for adverse selection.** At the moment of the fill, the passive side was up about half the spread (+12 bps). Within a second it was gone. Across both recordings the average fill was worth about −1 bps after 60 s before fees, and about −9 bps after a 7.5 bps maker fee. On the busiest pairs (PEPE, GLMR, BONK, BANK, WLFI) fills lost money even before fees, or made at most 4 bps (IOTA), so every one of them lost after fees.
* **The pairs that earned mostly had prices that never moved.** ACM, PSG and ADX ended the window at the mid they started at, so both sides of the book earned the full half-spread: +15 to +35 bps before fees. (AI moved half a tick. JUV and WLFI also ended where they started but lost after fees, so stillness isn't enough.)
  * That is short volatility: a maker collects the half-spread while nothing happens and is left holding inventory when the price jumps. A fan token reprices on a match result. Twenty quiet minutes show the premium, not the jumps.
  * It is also small: ACM, PSG and ADX traded $70–100k a day each at this window's pace.
* **The USDT anchor helped only where it knows more than the book does.**
  * ZECUSDT is a sharp anchor (0.1 bps wide, $105M a day). But 94% of ZECBTC's trading (by notional) was at prices already stale against it: arbitrageurs picking off old quotes, as on BNBBTC (§13.1). A maker re-quoting off ZECUSDT would have dodged them and been left with almost nothing to fill.
  * PEPEUSDC's anchor, PEPEUSDT, is an equally coarse one-tick book, so it adds nothing. The filter kept 79% of fills at every threshold, and they lost 7.5 bps.
  * MUBARAKUSDC made money on every measure, during a 120 bps rally on its anchor. Without it, the anchored books lost 3–5 bps per fill before fees, filtered or not. One pair in one window isn't an edge, but it is the first pair to watch.
* **Scale.** The ceiling was ~$320 a day across all 86 pairs. In practice, a VIP 0 maker would have lost money on the average fill. If an edge shows up here, it will be a handful of quiet pairs worth tens of dollars a day each, with jump risk attached.

**Next**

* **Fair value: each coin's perp (§17).** Quoting off it lifted the filtered fills to +6 to +9 bps before fees, about break-even after them.
* **Record a week.** Phase 0 now records all 87 candidates (86 plus ZECBTC: `ops/recorders/spot-alts-*.env`). Run `markouts.py` on each day ([`ops/README.md`](ops/README.md) §11).
* **The test for a pair:** a positive +60 s realized spread after a 7.5 bps maker fee across the week, over at least a few hundred trades and including the days its price jumped. For books with a USDT anchor, judge the filtered version.
* **Then simulate the queue.** Your quote joins the back of its price level and fills only after the orders ahead of it have traded. On one-tick books that is most of the problem. After that, a small live test.
* **The fee matters here too.** The busiest pairs earned −3 to +4 bps before fees, so a maker fee of zero or below would bring them to roughly break-even. Ask the account manager about Altcoin LiquidityBoost (§13.1), which pays rebates to makers on altcoin pairs.
* **It adds to the short-volatility book.** Long-tail making earns while prices are still and loses when they jump, like the rest of the making in this plan. Size it with the rest of your short-volatility risk in mind, not as a diversifier.

## 17. A fair-value web for the long tail

You asked whether the long tail has a web to price against, like the triangles on the majors, or a factor model as in equities. There are three candidates, and they stack: the coin's own perpetual, its beta to BTC, and the book's own queues.

Fundamentals (APT-style factors, cash flows, token unlocks) move over days to months. They can set a view or an inventory skew, not a quote. Equity market makers don't quote off DCF either: they quote off the index future or ETF, through each stock's beta, plus the order book. Here the coin's perp plays the index future's role.

**1. The coin's own perpetual: the anchor.** 61 of the 81 candidate coins in §16 have a USDⓈ-M perp, trading 2–9× their spot volume.
* You can't trade perps from an Australian retail account, but their market data is public, so a spot quote can still be priced off them.
* `research/anchors.py` tests this on Binance's public archive: spot and perp trades with exchange timestamps, for 15 long-tail coins over 1–3 October 2026.

| Day | Spot fills | Gap to the perp closed in 10 s: by spot / by perp | All fills, +60 s | Fills ≥ 10 bps better than the perp: share / +60 s | Same, after a 7.5 bps maker fee |
|---|---:|---|---:|---|---:|
| Thu 1 Oct | 131,692 | 3–38% / −2 to +1% | 0.0 | 29% / +7.8 | +0.3 |
| Fri 2 Oct | 202,127 | 4–43% / −2 to +1% | +0.7 | 35% / +6.3 | −1.2 |
| Sat 3 Oct | 65,861 | 2–14% / −1 to +1% | +2.4 | 38% / +9.3 | +1.8 |

Realized spreads are in bps, notional-weighted, marked to the perp-implied price (the perp mid times the spot/perp ratio over the previous 5 minutes). Marking to the spot's own mid gives the same daily averages within 0.3 bps.

* **The perp leads on every coin, every day.** Spot moves towards it; it doesn't move towards spot. The busiest coins follow fastest (PEPE, JASMY, BONK, NMR).
* **Quoting only when the perp says the touch is good** keeps about a third of the fills and lifts their realized spread from 0–2 bps to 6–9 bps. It's the same effect as the USDT anchor on the cross pairs (§13.1), but on books with ten times the spread.
* **After a 7.5 bps maker fee it's roughly break-even overall.** Two coins cleared the fee on all three days: CYBER (+5.4 to +9.0 bps after it) and BONK (+2.5 to +3.1). PEPE cleared it on two days.

**2. The factor model: beta to BTC.** On 1-minute returns these coins have betas of 0.4–2.0 to BTC; the memecoins PEPE and BONK are about 1.9. BTC explains up to half of a busy coin's minute-to-minute variance.
* But for the next 10 s of a spot book, the perp gap explains 1–19% of the variance, and BTC's last 10 s adds at most 1.5 points. The perp has already priced BTC in.
* A beta model is the fallback for the 20 candidate coins with no perp (the fan tokens, AI, GLMR): BTC, ETH and a basket of same-sector perps as factors.

**3. The book itself: who is queueing on which side.** The live recordings in §16 carry the size at the best bid and ask. Here the fills are bucketed by how much of the touch's size was on the maker's side 20 ms before the fill:

| Maker's side of the touch | Fills | Share of notional | +60 s vs own mid (bps) |
|---|---:|---:|---:|
| 0–25% | 1,227 | 43% | −3.6 |
| 25–50% | 753 | 31% | −4.8 |
| 50–75% | 591 | 13% | +8.4 |
| 75–100% | 396 | 13% | +7.1 |

* Fills on the thin side of the touch (the side about to be cleared) lost money; fills on the thick side earned 7–8 bps. On one-tick books (PEPE, BONK, AI) this is the sub-tick fair value, the "microprice".
* The catch is queue position. A quote that joins the back of a thick queue fills only once the queue ahead of it has traded, by which time its side is the thin one. Getting the good fills means joining a new price level as soon as it forms.

**What it means**

* **The web for the long tail is each coin's perp, with the book's queues on top.** BTC beta adds little where a perp exists.
* **The perp moves the long tail from a loss to about break-even.** Fills lost 5–9 bps after fees (§16 and the all-fills column above); with the perp as fair value they come to about zero, and two coins are clearly positive.
* **The fee decides the rest.** At a 5 bps maker fee the filtered fills are positive on all three days (+1.3 to +4.3 bps); at zero they make +6 to +9 bps on a third of the flow.
* **Scale at your fees.** At a 5% share of the flow that passes the filter, CYBER and BONK are each worth roughly $5–45 a day after fees. At a zero maker fee, a 1% share of the filtered flow across these 15 coins would have earned about $190 on 2 October.
* **Speed.** The perp's lead plays out over seconds, not microseconds: spot closes 2–43% of the gap in 10 s. A Tokyo box with millisecond latency can use it. Queue position on one-tick books is the part that rewards speed.

**Next**

* Phase 0 now also records the top of book of the 61 alt perps (`ops/recorders/perp-alts-bookticker.env`). That measures the lead on quotes, from Tokyo, alongside the spot books.
* Run `anchors.py` on more days. It reads the public archive, so it needs no recorder. Keep CYBER and BONK as the first test pairs.
* The quoting engine needs four inputs:
  * the perp-implied price (perp mid × a 5-minute basis);
  * the queue sizes on both sides of the touch;
  * its own place in the queue;
  * an order budget: re-quotes on perp moves count against 100 orders per 10 s.

## Appendix: research code

```
research/arb_core.py           graph, 74-cycle enumeration, top-of-book screen, depth-aware sizer, open-path routing (pure functions)
research/record_streams.py     record any spot or perp stream to JSONL(.gz), optionally one file per UTC day
research/analyze_recording.py  replay a recording: update rates, spreads, net-positive episodes per fee level or per-pair
                               fee schedule (--fees), closest cycles, optimal sizes (optionally capped, --packet-usdt)
research/analyze_making.py     which pairs to make: hedged-at-once edge, quote edge vs graph fair value, realized spread
                               of real fills (--trades, an aggTrade recording)
research/basis_funding.py      perp funding history and spot-perp basis from the public archive (data.binance.vision)
research/compare_fair_values.py  which fair-value estimator to make markets off (own mid, route median, USDT, volume-weighted)
research/latency_probe.py      REST, WebSocket API and market-data latency from the box it runs on (pick the Tokyo AZ)
research/dex_probe.py          PancakeSwap v3 (BNB Chain) prices vs Binance top of book, after pool fee, taker fee and gas
research/scan_spreads.py       every Binance spot pair: spreads wide enough for a retail maker, in markets too small for big
                               firms; prints the symbols to record for them
research/markouts.py           realized spread of passive fills on any pairs, against their own mid and, where one exists,
                               a USDT anchor
research/anchors.py            does a long-tail spot book follow its coin's perp or BTC? lead, R², and maker markouts
                               filtered by the perp-implied price (one day of the public archive)
ops/                           Phase 0 on AWS Tokyo: runbook, bootstrap, systemd recorders, hourly S3 upload, IAM policies
research/test_arb_core.py      unit tests (python research/test_arb_core.py)
```

```
pip install websockets
cd research
python record_streams.py --stream bookTicker    --seconds 3600 --out bt.jsonl.gz &
python record_streams.py --stream depth20@100ms --seconds 3600 --out d20.jsonl.gz &
wait
python analyze_recording.py --bookticker bt.jsonl.gz --depth d20.jsonl.gz \
    --fees "7.5,USDCUSDT=0,BTCUSDC=7.125,ETHUSDC=7.125,BNBUSDC=7.125" --packet-usdt 100
python record_streams.py --stream aggTrade --seconds 3600 --out tr.jsonl.gz   # alongside a bookTicker recording
python analyze_making.py --bookticker bt.jsonl.gz --trades tr.jsonl.gz \
    --fees "7.5,USDCUSDT=0,BTCUSDC=7.125,ETHUSDC=7.125,BNBUSDC=7.125"
python compare_fair_values.py --bookticker bt.jsonl.gz --trades tr.jsonl.gz
python basis_funding.py --months 12 --days 7
python scan_spreads.py --top 100        # long-tail candidates and the symbols to record for them
python markouts.py --bookticker alts_bt.jsonl.gz --trades alts_tr.jsonl.gz --maker-bps 7.5
python anchors.py --day 2026-10-02 --coins PEPE,BONK,CYBER,JASMY,NMR
```
