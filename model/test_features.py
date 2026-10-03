"""Hand-checked cases for the reversal features. Run: .venv/bin/python -m pytest model -q"""

import numpy as np
import polars as pl

from features import book_features, leader_features, trade_features

TRADES = pl.DataFrame({"t": [1000, 1500, 1900, 2500], "price": [100.0, 101.0, 99.0, 100.0],
                       "qty": [1.0, 2.0, 3.0, 1.0], "buyer_maker": [False, True, False, True]})


def test_window_features_and_direction_mapping():
    t0, mid = np.array([2000, 2000]), np.array([100.0, 100.0])
    f = trade_features(TRADES, t0, np.array([1, -1]), mid)
    # 1 s window w0 = (1000, 2000]: the sell at 1500 (2 @ 101) and the buy at 1900 (3 @ 99)
    assert list(f["buy_count_1s_w0"]) == [1, 1] and list(f["sell_count_1s_w0"]) == [1, 1]
    assert list(f["total_buy_1s_w0"]) == [3, 2] and list(f["total_sell_1s_w0"]) == [2, 3]
    assert list(f["max_size_1s_w0"]) == [3, 3] and list(f["avg_size_1s_w0"]) == [2.5, 2.5]
    assert np.allclose(f["amplitude_1s_w0"], 200.0)
    vwap = (101 * 2 + 99 * 3) / 5
    assert np.allclose(f["ret_vwap_1s_w0"], [np.log(vwap / 100) * 1e4, -np.log(vwap / 100) * 1e4])
    r1, r2 = np.log(101 / 100), np.log(99 / 101)
    assert np.allclose(f["ret_sum_1s_w0"], [(r1 + r2) * 1e4, -(r1 + r2) * 1e4])
    assert np.allclose(f["ret_autocov_1s_w0"], (r2 * r1 - ((r1 + r2) / 2) ** 2) * 1e8)
    assert list(f["trade_intensity_1s_w0"]) == [400, 400]
    # w1 = (0, 1000]: a single trade -> no autocov, intensity defaults to the window length
    assert list(f["buy_count_1s_w1"]) == [1, 0] and list(f["trade_intensity_1s_w1"]) == [1000, 1000]
    assert list(f["ret_autocov_1s_w1"]) == [0, 0] and list(f["amplitude_1s_w1"]) == [0, 0]


def test_book_features_and_factor1_label():
    book = pl.DataFrame({"t": [0, 500, 1200, 3000], "bid_px": [100.0, 100.0, 101.0, 100.0],
                         "bid_qty": [5.0, 6.0, 1.0, 2.0], "ask_px": [101.0, 101.0, 102.0, 101.0],
                         "ask_qty": [3.0, 3.0, 4.0, 7.0]})
    t0 = np.array([2000, 2000])
    f, labels, mid, _ = book_features(book, np.zeros(0, np.int64), t0, t0, np.array([1, -1]))
    assert list(f["ob_bid_liq"]) == [1.0, 4.0] and list(f["ob_ask_liq"]) == [4.0, 1.0]
    assert list(f["age"]) == [0.8, 0.8]              # last top-of-book price change at 1200
    assert list(f["totb_mean"]) == [300.0, 300.0]    # 2 price states in the last 10 min
    assert list(mid) == [101.5, 101.5]
    # next change at 3000 is down on both sides: bad for the buy, good for the sell
    assert list(labels["fav_first"]) == [0.0, 1.0]


def test_leader_features():
    lbook = pl.DataFrame({"t": [0, 1500], "bid_px": [100.0, 102.0], "bid_qty": [3.0, 1.0],
                          "ask_px": [101.0, 103.0], "ask_qty": [1.0, 1.0]})
    fbook = pl.DataFrame({"t": [0, 1000], "bid_px": [100.0, 99.0], "bid_qty": [1.0, 1.0],
                          "ask_px": [101.0, 100.0], "ask_qty": [1.0, 1.0]})
    ltrades = pl.DataFrame({"t": [1900, 1950], "price": [103.0, 102.0], "qty": [2.0, 0.5],
                            "buyer_maker": [False, True]})
    side = np.array([1, -1])
    f = leader_features(lbook, ltrades, fbook, np.array([2000, 2000]), side)
    up = np.log(102.5 / 100.5) * 1e4          # leader mid 100.5 -> 102.5 since the follower last moved (t=1000)
    assert np.allclose(f["lead_imb"], 0) and np.allclose(f["lead_micro"], 0)
    assert np.allclose(f["lead_ret_1s"], up * side) and np.allclose(f["lead_gap"], up * side)
    assert np.allclose(f["lead_flow_100ms"], -0.5 * side) and np.allclose(f["lead_flow_1s"], 1.5 * side)
    assert np.allclose(f["lead_age"], 0.5)
    basis_now = np.log(102.5 / 99.5) * 1e4
    basis_mean = (0 + np.log(100.5 / 99.5) * 1e4) / 2   # 1 s grid points at t=0 and t=1000
    assert np.allclose(f["lead_basis_dev"], (basis_now - basis_mean) * side)
