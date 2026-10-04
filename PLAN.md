# Binance spot cycle arbitrage: plan

Draft v4, 2026-10-04: reads fair value from the USDT books (§13.1) and fits the plan to 15k AUD (§13.5). Assets: USDT, USDC, BTC, ETH, BNB on Binance spot.

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
| **0. Measure** | Run the Python recorder on a Tokyo instance for 1–2 weeks, including volatile days. JSON streams answer this question; no C++ is needed yet. Replay with `--fees "7.5,USDCUSDT=0,BTCUSDC=7.125,ETHUSDC=7.125,BNBUSDC=7.125" --packet-usdt 100`. Also record `aggTrade` streams for markouts (§13.1), and perp `bookTicker` / `markPrice` streams (§13.2). Run `basis_funding.py` monthly. Measure `order.test` round-trip per AZ. | Enough episodes per day above ~15–17 bps (the 14.6 bps hurdle plus a buffer), lasting longer than your measured round-trip, at sizes that make cents-per-trade worth running. **Otherwise stop, or revisit fees (higher tier, maker legs).** |
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

### 13.5 Fitting it to 15k AUD

Your answers settle the scale: 15k AUD in total, roughly US$10k at ~0.65 USD per AUD. Three things follow from it.

* **Perps may not be available to you at all.** If the account is Australian, retail clients generally can't trade Binance derivatives. ASIC cancelled Binance Australia Derivatives' licence in April 2023, and in 2026 a court penalised it A$10M for letting retail clients into derivatives. Wholesale clients still can, subject to the wholesale-client tests.
  * Without perps, the carry trade, the perp hedges and perp market making (§13.2–§13.3) drop out.
  * What remains is spot-only: taker cycles, spot making, and inventory kept small instead of hedged.
  * Confirm your classification before building anything that needs futures.
* **VIP 2 needs more than this account will have.** Binance's criteria require both ≥ $5M of 30-day spot volume and ≥ 25 BNB (about US$20k).
  * Unless Binance offers you a trial or another route, plan on VIP 0 with the BNB discount: 0.075% maker and taker, or 0.075% / 0.07125% on USDC pairs.
  * Only the maker rate differs from your VIP 2 numbers, and no conclusion here depends on it. Taker cycles never cleared even 5 bps per leg, and making needs a maker fee of zero either way.
* **Portfolio margin probably isn't available yet.** Binance's last published requirement for regular users was 100,000 USDT across the cross-margin and futures wallets (April 2024). Without it, a perp hedge needs its own margin in the futures wallet: hedging $3k of coins at 3× leverage ties up about $1k more.

What the capital is for, at this size:

* **The edges measured so far are worth cents to a few dollars a day here.** The capital's main job is to fund Phase 0 and a careful start, not to earn yet. Keep most of it in stablecoins until a strategy passes its gate (§13.4).
* **Taker cycles** need about US$500 of working inventory (~$100 per asset) plus a small BNB fee float.
* **Making** stays in shadow until the maker fee is zero. The programme's ~$20M a month is about 2,000 turns of this whole book every month, so it only becomes reachable once a strategy is profitable per trade at your fees.
* **Carry** is the one track that works at any size, but at last year's funding (~3% a year) it earns about A$25 a month per A$10k, if perps are available to you at all.

## 14. Decisions so far, and open questions

Decided on 2026-10-04:

* **Fees (as you expect them):** VIP 2, paid in BNB. 0.06% maker on all pairs. 0.075% taker on USDT and cross pairs, 0.07125% taker on USDC pairs. USDCUSDT free. Futures ≈ 0.02% / 0.05%. See §13.5 for whether VIP 2 is reachable.
* **Capital:** 15k AUD in total (§13.5). Start with ~$100 per coin of working inventory.
* **Live engine:** C++ (§8.3). Python stays as the research tooling and reference implementation.
* **Scope:** taker cycles, market making (working towards Binance's maker programme), and perps for hedging and spot–perp basis (§13), subject to futures access (§13.5).
* **Fair value for making:** read from the USDT books (§13.1).
* **Portfolio margin:** you plan to enable it; check eligibility (§13.5).
* **Account manager:** once the account is approved, ask them about the maker programme's requirements.

Open:

1. **Futures access:** is the account Australian, and if so, are you classified as a wholesale client? This decides whether the perps track exists at all.
2. **VIP 2:** how do you expect to qualify (a trial, a referral offer)? Otherwise the plan assumes VIP 0 maker fees.
3. **Phase 0:** when can the Tokyo recorder start? It's the next concrete step for every track.

## Appendix: research code

```
research/arb_core.py           graph, 74-cycle enumeration, top-of-book screen, depth-aware sizer, open-path routing (pure functions)
research/record_streams.py     record bookTicker / depth20 for the 10 symbols to JSONL(.gz)
research/analyze_recording.py  replay a recording: update rates, spreads, net-positive episodes per fee level or per-pair
                               fee schedule (--fees), closest cycles, optimal sizes (optionally capped, --packet-usdt)
research/analyze_making.py     which pairs to make: hedged-at-once edge, quote edge vs graph fair value, realized spread
                               of real fills (--trades, an aggTrade recording)
research/basis_funding.py      perp funding history and spot-perp basis from the public archive (data.binance.vision)
research/compare_fair_values.py  which fair-value estimator to make markets off (own mid, route median, USDT, volume-weighted)
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
```
