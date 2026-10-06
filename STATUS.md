# Status board — the Binance lab

Update at the end of every session. A line's **state** lives in `Wealth-Optimiser/MAP.md` and the
portfolio board in `Wealth-Optimiser/STATUS.md`; this file holds the lab's detail and its measurements.
A number without its mean ± SE and n does not go in here.

**Line 1, to be filled by the owner:** whether the AWS Tokyo recorder (`ops/README.md`) exists, which
AZ, the bucket name, the date of the first day's upload, and whether `/opt/binance-arb` is on `main`.
Until this line is filled, every open line below is "designed; phase 0 not confirmed running".

**Now (2026-10-06):** BinanceMarketMaking is merged in as `perps/` with its history (its recorder,
the queue simulator calibrated to the paper's fill rate, the checksummed archive fetcher and
`leader_features()`); `perps/sim` and `perps/model` tests: 12 passed, run by the orchestrator in a
fresh Python 3.13 venv on 2026-10-06. `main` now exists on GitHub and the Tokyo runbook clones it; the
old default branch `claude/great-dijkstra-n3wu1n` is kept until the owner confirms the box is on `main`
or confirms the box does not exist. `PLAN.md` §0, §3 and §12 are still costed at VIP 2 while §13.5 and
§14 decided VIP 0; the taker numbers coincide, the maker numbers (6 vs 7.5 bps) do not, and the §13.1
making tables are the ones affected.

| line | state (per `MAP.md`) | what decides it | next |
|---|---|---|---|
| Long-tail spot making priced off each coin's perp | open: phase 0 | a week of markouts, paired by day with SEs, passing the §16 test per pair, including days the price jumped | fill line 1; run `markouts.py` and `anchors.py` on each recorded day |
| Cross-pair making off USDT fair value | blocked: fees | a maker fee of 0 | shadow only |
| Taker cycles; DEX–CEX; intraday basis | closed at current fees | §12, §15, §13.2 | phase 0 counts episodes; nothing else |
| Perp making (`perps/`) | blocked: regulation; negative in simulation | §13.5; `perps/README.md` findings | nothing |
| Cash-and-carry; perp hedges | blocked: regulation | §13.5 | wholesale status, the owner's call |

## Decisions taken

- 2026-10-06 — **This repo is the single Binance lab** (owner, via `Wealth-Optimiser/MAP.md` §10).
  BinanceMarketMaking merged as `perps/`; one recorder and one markout module are the next cleanup
  (`MAP.md` §8), before phase 0 data is read.
- 2026-10-06 — **Default branch moves to `main`.** The runbook's two clone lines now say `-b main`.
- 2026-10-04 — the decisions in `PLAN.md` §14 (Australian retail, spot only; VIP 0 with BNB for
  planning; 15k AUD; C++ live engine; phase 0 on Tokyo).

## Result log

_(newest first; one entry per measured result, with n, mean ± SE, t, and the test count)_

- 2026-10-06 — no paired result yet. Every number in `PLAN.md` §12–§17 is a single pooled window
  measured from a sandbox, which the plan itself calls "a sanity check, not as Phase 0". The first
  entry here will be a per-pair realised spread over the phase 0 week, paired by day.
