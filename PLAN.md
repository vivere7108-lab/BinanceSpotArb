# Binance spot cycle arbitrage: plan

Draft v1, 2026-10-04. Assets: USDT, USDC, BTC, ETH, BNB on Binance spot.

## 0. Recommendations

1. **Score every cycle, not paths between home nodes.** All 10 pairs between the five assets exist, so the graph is complete. It has exactly 74 directed cycles of 3–5 legs. Enumerate them once at startup. When a book updates, re-score only the 30 cycles that use that symbol. That costs about 40 µs in plain Python, so narrowing the search saves nothing.
2. **A "home" is an accounting choice, not a path constraint.** A cycle's return is the same whichever asset you start from. The start asset only decides where the surplus lands and, for sequential execution, which asset you must hold first. With parallel execution you hold all five anyway.
3. **Use one numeraire (USDT) and one fee float (BNB), not two homes.** Book all P&L in USDT. BNB is not a stable asset: hold it as a fee float with a target band. When the float runs low, land cycle surpluses in BNB instead of buying it separately. That is the useful version of "BNB as home".
4. **Open paths between nodes are already covered by cycles.** Take any route from A to B, say USDT⇝BNB. If it beats the direct A→B conversion, then, to within that pair's spread, it is a profitable cycle closed by the direct leg back. Judging an open path against a mid price instead is a directional bet on B. Picking the cheapest route for a purchase you need anyway, such as topping up BNB, is routing, and it belongs in the treasury (§2.3).
5. **Parallel execution means trading from inventory.** Hold working balances in every asset. Fire all legs at once as LIMIT IOC orders at the worst price the simulator touched. A rebalancer cleans up leg mismatches. This takes one round trip instead of one per leg. Send the contested leg (the quote most likely to vanish) first. If its misses prove expensive, fall back to sending the rest only after it fills (§5.1).
6. **Trade size = min(depth-optimal size, packet cap, inventory per leg), then apply exchange filters.** "Depth-optimal" means walking all legs' books together until the marginal cycle rate falls to 1 plus a buffer. That is the right generalisation of "bottleneck volume" (§4.2).
7. **Fees decide viability, so measure before building execution.** At VIP 0 paying fees in BNB, a 3-leg cycle needs more than 22.5 bps gross. In the live sample in §12, the best gross edge was about 10 bps and nothing cleared even 5 bps per leg. At 2 bps per leg, roughly top-VIP fees, there were five windows in 30 minutes, each 5 ms or less and worth about $1. Phase 0 (§11) is a go/no-go measurement from Tokyo at your real commission rates.
8. **Your constraints, in the order they bind:**
   * **Fees** bind first, by a wide margin.
   * **Latency** is next: the opportunities that do appear last milliseconds.
   * **The order-rate limit** binds only through misses, since filled orders don't count against it.
   * **Streaming rate and simulation time** are engineering work at the µs level, not limits (§6, §8).

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

Score all 74 cycles; it's cheap. Execute only 3-leg cycles in v1. Each extra leg adds a full taker fee and multiplies in another fill probability (P(all fill) = ∏ pᵢ). Enable 4- and 5-leg cycles once live fill statistics justify the extra risk. Longer cycles sometimes reach slightly higher gross edges by chaining several small mispricings. But each extra leg pays another fee: at 2 bps per leg, every net-positive episode in the sample was a 3-leg cycle.

## 3. Fees

* **Fetch rates per symbol; don't hardcode.** Call `GET /api/v3/account/commission?symbol=…` (IP weight 20) for each of the 10 symbols at startup and hourly. The total rate is standard (multiplied by `discount` when paying in BNB) + tax + special.
* **Current schedule.** VIP 0 is 0.10% maker and taker. Paying in BNB multiplies the standard commission by `discount`, currently 0.75 (25% off), giving **0.075%**. The published VIP 9 rate is 0.011% maker / 0.023% taker before the BNB discount. VIP level depends on 30-day volume and BNB balance.
* **Paying fees in BNB.** Each leg delivers its gross amount and the fee is a separate BNB debit: notional × rate × discount ÷ BNB price. The simulator multiplies each leg by (1 − f), which is correct to first order and fine for screening. The ledger should book the BNB debit exactly. If the BNB float runs out, Binance takes the fee from the received asset at the undiscounted rate, so alarm well before that.

Hurdle a cycle must clear at the touch, before any latency buffer:

| Per-leg taker fee | 3 legs | 4 legs | 5 legs |
|---|---:|---:|---:|
| VIP 0, no BNB: 10 bps | 30 bps | 40 bps | 50 bps |
| **VIP 0, BNB: 7.5 bps** | **22.5 bps** | **30 bps** | **37.5 bps** |
| VIP 9, BNB: ≈ 1.7 bps | ≈ 5.2 bps | ≈ 6.9 bps | ≈ 8.6 bps |

Ways to lower the hurdle: volume tier, BNB holdings, pair-specific promotions (they show up in the per-symbol commission call), market-maker programs, and maker legs (§5.5).

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

Rounding: one step is worth $0.27–$1 (table above), which is up to 1% of a leg on a $100 packet. In the inventory model this residue isn't lost; it stays in inventory as small drift that the rebalancer absorbs. Still, keep packets ≥ ~$100 so rounding doesn't dominate the edge.

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

* **Target balances.** Hold T_a ≈ (packets in flight + repair headroom) × packet size in each asset, e.g. 3–5 packets. Set a band around each target; the rebalancer acts only outside it.
* **Directional exposure.** The BTC/ETH/BNB part of that inventory is a directional position. Keep it small relative to expected profit, or hedge it with perpetual futures. Hedging adds funding cost and margin management, so decide once the inventory size is known.
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

The cross pairs are slow and tick-constrained. You could rest a maker order on one of them at a price where a fill makes hedging the other legs as taker profitable, then fire those hedge legs IOC on the fill. You earn the 1–3 bps spread instead of paying it. The costs: adverse selection, an STP mode on the resting order, and fast amend or cancel ("order amend keep priority" is now available on all symbols). At VIP 0 the maker fee equals the taker fee, so this helps the spread, not the fee. It deserves its own study once Phase 0 numbers are in.

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

The simulation itself is small compared with the network. In a race, though, every 100 µs counts. Prototype in Python. If Phase 0 says go, port the hot path (decode → book → screen → size → send) to Rust or C++ as a single-threaded loop on a pinned core.

## 9. Risk controls

* **Pre-trade:** packet cap, per-asset inventory bands, max cycles in flight, max legs (3 to start), hurdle plus buffer, rate-limit budget, data-health gate.
* **Post-trade:** drift limits, a daily loss limit marked to market in USDT, breakers on consecutive partial fills and on error rate, and a BNB float alarm.
* **Kill switch:** stop firing and cancel any resting orders. Optionally flatten to target inventory.
* **Keys:** trade-only, withdrawals disabled, IP-restricted, Ed25519. Secrets stay out of the repo.
* **Exchange changes:** re-fetch `exchangeInfo` periodically and after any filter error. Handle status changes and maintenance windows.

## 10. Architecture sketch

```
 SBE streams ─► feed handler ─► book store ─► screener (30 cycles/update) ─► sizer ─► risk gate ─► order gateway (WS API) ─► Binance
                                                                                          ▲                │
 user data stream (WS API) ─────────────────────────────► positions / fills ◄─────────────┴────────────────┘
                                                                  │
                                                  treasury / rebalancer (bands, BNB float, open-path routing)
 recorder (raw messages + local timestamps) ─► replay / backtest
```

The hot loop runs on one thread with no locks. The treasury and recorder run off the hot path.

## 11. Phased plan

| Phase | What | Exit criterion |
|---|---|---|
| **0. Measure** | Record SBE `bestBidAsk` and depth from Tokyo for 1–2 weeks, including volatile days. Replay at **your** commission rates with `research/analyze_recording.py`, extended to SBE input. Measure tick-to-trade per AZ. | Expected daily profit after fees and an estimated miss rate beats running costs, given your measured latency against opportunity durations. **Otherwise stop, or change the fee assumptions (tier, maker legs).** |
| **1. Shadow** | Run the live engine and log would-be orders without sending them. Score each against the books seen 1, 5 and 20 ms later (would it have filled?). | Shadow P(fill) and P&L agree with the Phase 0 replay. |
| **2. Small live** | Small packets (~$100: well above min notional, and big enough that step rounding doesn't swamp the edge), 3-leg only, one cycle in flight, IOC. Benchmark WebSocket API against FIX. | Fill rates, slippage and repair costs inside the Phase 1 model. |
| **3. Scale** | Larger packets, concurrent cycles with depth allocation, 4–5 legs, maker-leg variant, more assets. | Each step pays for its added risk. |

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
| **7.5 bps (VIP 0 + BNB)** | **22.5 bps** | **0** | 0 | 0 | – | – | – | – |
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

**What it means**

* **At your likely fee (7.5 bps per leg), nothing came close.** The best gross edge, ~10 bps, is less than half the 22.5 bps three-leg hurdle.
* **At 2 bps per leg the best case was tiny.** There were five windows of 5 ms or less in 30 minutes, all in the first 13, each worth about $1 at full depth. Catching them takes both top-tier fees and sub-5 ms reaction.
* **Almost every episode involved BNB,** usually a better BNB quote that lived only a few milliseconds. The persistent USDT/USDC basis on BNB is the most interesting lead, and a natural subject for the maker-leg study (§5.5).
* **This was a quiet Sunday morning, measured outside Tokyo.** Volatile periods produce larger and more frequent dislocations, and also more competition. Phase 0 exists to measure exactly that.

## 13. Open questions

1. What commission does your account pay on these 10 symbols (VIP tier, any promotions)? That one number decides viability.
2. How much BTC/ETH/BNB inventory are you willing to hold, and should it be hedged?
3. What language do you want for the live engine? The research code is Python.
4. Are maker-leg variants in scope?

## Appendix: research code

```
research/arb_core.py           graph, 74-cycle enumeration, top-of-book screen, depth-aware sizer, open-path routing (pure functions)
research/record_streams.py     record bookTicker / depth20 for the 10 symbols to JSONL(.gz)
research/analyze_recording.py  replay a recording: update rates, spreads, net-positive episodes per fee level, optimal sizes
research/test_arb_core.py      unit tests (python research/test_arb_core.py)
```

```
pip install websockets
cd research
python record_streams.py --stream bookTicker    --seconds 3600 --out bt.jsonl.gz &
python record_streams.py --stream depth20@100ms --seconds 3600 --out d20.jsonl.gz &
wait
python analyze_recording.py --bookticker bt.jsonl.gz --depth d20.jsonl.gz
```
