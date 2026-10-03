# BinanceMarketMaking

Research and tooling for maker strategies on Binance USDⓈ-M futures, starting from Albers, Cucuringu,
Howison & Shestopaloff, "The Market Maker's Dilemma" ([arXiv:2502.18625](https://arxiv.org/abs/2502.18625)).

| Path | What it does |
|---|---|
| `recorder/record.py` | 24/7 recorder: bookTicker, depth20@100ms, trade (route `/public`) and aggTrade (route `/market`) over redundant websocket feeds, written to zstd Parquet |
| `recorder/check_data.py` | Integrity report: gaps, duplicates, missing trade ids, latency, GB/day |
| `recorder/binance-recorder.service` | systemd user unit (paths assume `/home/bread/Documents/BinanceMarketMaking`) |
| `sim/fetch_archive.py` | Downloads and checksums data.binance.vision archives (bookTicker ends 2024-03-30) |
| `sim/queue_sim.py` | Numba simulator for maker orders at the touch: queue exponent k, latency, polling bot, order size |
| `sim/validate_paper.py` | Compares the simulator with the paper's published real-order statistics |
| `model/features.py` | The paper's 171 L1 features (plus optional leader-market features) at each order's decision time |
| `model/fit_reversal.py`, `model/leader_test.py`, `model/learning_curve.py` | Reversal models, leader-feature test, learning curves and model comparison |

## Setup

```bash
uv venv .venv --python 3.14
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest sim model -q
```

## Recording

```bash
.venv/bin/python recorder/record.py --symbols BTCUSDT,BTCUSDC --out data
.venv/bin/python recorder/check_data.py
```

To run it as a service, edit the paths in `recorder/binance-recorder.service`, then:

```bash
systemctl --user link "$PWD/recorder/binance-recorder.service"
systemctl --user enable --now binance-recorder
loginctl enable-linger "$USER"   # keep running after logout
```

## Research pipeline

```bash
.venv/bin/python sim/fetch_archive.py --symbol BTCUSDT --start 2024-02-12 --end 2024-02-18
.venv/bin/python sim/queue_sim.py --symbol BTCUSDT --start 2024-02-12 --end 2024-02-18 --models pess,prop,opt --latency-ms 10
.venv/bin/python sim/validate_paper.py --symbol BTCUSDT
.venv/bin/python model/features.py --config prop-L10ms --start 2024-02-12 --end 2024-02-18
.venv/bin/python model/fit_reversal.py --config prop-L10ms
```

Add `--source recorded` to the simulator and feature builder to run them on the recorder's own data.
`data/`, `archive/` and `results/` are not committed; the commands above regenerate them.

## Findings so far

Simulated fills on the paper's week (BTCUSDT and BTCUSDC, Feb 12-18 2024) unless noted.

- The pro-rata queue model reproduces the paper's real fill rate (55.2% vs 54.6%); the pessimistic model matches its markouts.
- The paper's reversal model reaches AUC 0.55-0.60. Its top 1% of orders improve the 5 s markout by 0.1-0.3 bp but stay negative, and the paper's Table 5 coefficients don't replicate.
- BTCUSDC fills are about 0.3 bp more adverse than BTCUSDT's. BTCUSDT's state carries the signal (11 features beat the paper's 171), but the selected fills still lose about 0.85-0.93 bp.
- More training data stops helping the linear models early; gradient-boosted trees raise AUC without improving the selected orders' markouts.
- Capacity on BTCUSDC (2026-10-03 sample): fill rate and markout degrade gently up to about 0.05-0.1 BTC per order.
