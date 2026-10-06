"""Scenario tests for the queue simulator's fill rules. Run: .venv/bin/python -m pytest sim -q"""

import numpy as np
import polars as pl

from queue_sim import MODELS, REASONS, STATUSES, FILL_KINDS, orders_frame, run_side, side_arrays, simulate_side


def arr(*xs):
    return np.array(xs, dtype=np.int64)


def run(book, trades, model="pess", lat_md=0, lat_ord=1, poll_ms=0, size=1):
    """book rows: (t, near_px, near_qty, opp_px, opp_qty); trades: (t, px, qty), buy frame."""
    b = np.array(book, dtype=np.int64).T
    t = np.array(trades, dtype=np.int64).T if trades else np.zeros((3, 0), np.int64)
    out = simulate_side(b[0], b[1], b[2], b[3], b[4], t[0], t[1], t[2], MODELS[model], lat_md, lat_ord,
                        poll_ms, size, 100)
    n = out[0]
    names = ["tdec", "tpost", "tend", "px", "reason", "status", "kind", "np_post", "nq_post", "oq_post",
             "la_post", "la_fill", "lb_fill", "nq_fill", "oq_fill", "qfill", "tfirst", "qcross"]
    rows = [dict(zip(names, (a[i] for a in out[1:]))) for i in range(n)]
    for r in rows:
        r["reason"], r["status"], r["kind"] = REASONS[r["reason"]], STATUSES[r["status"]], FILL_KINDS[r["kind"]]
    return rows


def test_trade_fill_tracks_queue_ahead_and_behind():
    book = [(0, 100, 5, 101, 5),
            (10, 100, 2, 101, 5),     # 3 traded from the front: 2 ahead of us
            (15, 100, 4, 101, 5),     # +2 join behind us
            (20, 100, 1, 101, 5),     # after the second 3-lot trade
            (30, 100, 1, 101, 5)]     # (more data, so the repost can arrive)
    trades = [(10, 100, 3), (20, 100, 3)]   # second trade: 3 > 2 ahead -> reaches us
    o = run(book, trades)
    assert o[0]["tpost"] == 1 and o[0]["la_post"] == 5
    assert o[0]["status"] == "fill" and o[0]["kind"] == "trade" and o[0]["tend"] == 20
    assert (o[0]["la_fill"], o[0]["lb_fill"]) == (2, 2)
    assert o[1]["reason"] == "after_fill" and o[1]["tpost"] == 21 and o[1]["la_post"] == 1
    assert o[1]["status"] == "end"


def test_stale_reject_and_cancel_are_reposted():
    book = [(0, 100, 5, 101, 5),
            (3, 101, 1, 102, 5),      # top moves up while our order is in flight
            (8, 100, 3, 101, 2),      # ask comes down to our intended 101 -> post-only reject
            (20, 101, 1, 102, 5),     # top improves while resting -> cancel
            (30, 101, 1, 102, 5)]
    o = run(book, [], lat_md=0, lat_ord=5)
    assert [r["status"] for r in o] == ["cancel", "reject", "cancel", "end"]
    assert [r["reason"] for r in o] == ["start", "after_stale", "after_reject", "after_cancel"]
    assert [r["px"] for r in o] == [100, 101, 100, 101]
    assert [r["tend"] for r in o[:3]] == [5, 10, 20]


def test_sweep_and_alone_fills():
    book = [(0, 100, 5, 101, 5),
            (10, 99, 3, 101, 5),      # level 100 swept
            (15, 98, 4, 101, 5)]      # level 99 vanishes by cancels -> our order alone at the top
    trades = [(10, 100, 5), (10, 99, 2), (20, 98, 1)]
    o = run(book, trades)
    assert (o[0]["status"], o[0]["kind"], o[0]["tend"]) == ("fill", "sweep", 10)
    assert (o[1]["px"], o[1]["la_post"]) == (99, 3)
    assert (o[1]["status"], o[1]["kind"], o[1]["tend"]) == ("fill", "alone", 20)


def test_cross_fill():
    o = run([(0, 100, 5, 101, 5), (5, 99, 5, 100, 1)], [])
    assert (o[0]["status"], o[0]["kind"], o[0]["tend"]) == ("fill", "cross", 5)


def test_queue_models_split_cancels_differently():
    book = [(0, 100, 5, 101, 5),
            (5, 100, 10, 101, 5),     # +5 behind
            (6, 100, 6, 101, 5),      # -4 cancelled
            (10, 100, 4, 101, 5)]
    trades = [(10, 100, 2), (12, 100, 2)]
    ends = {m: run(book, trades, model=m)[0] for m in ("pess", "prop", "opt")}
    assert (ends["opt"]["status"], ends["opt"]["tend"]) == ("fill", 10)    # ahead: 5 -> 1, trade of 2 fills
    assert (ends["prop"]["status"], ends["prop"]["tend"]) == ("fill", 12)  # ahead: 5 -> 3 -> 1, then fills
    assert ends["pess"]["status"] == "end"                                 # ahead: 5 -> 3 -> 1, no fill


def test_polling_bot_keeps_place_through_flicker_and_waits_for_checks():
    book = [(0, 100, 5, 101, 5),
            (100, 101, 1, 102, 5),    # our level drops below the top: no cancel until the 1000 ms check
            (200, 100, 3, 101, 5),    # back on top before the check; 2 cancelled ahead of us -> 3 ahead
            (250, 100, 5, 101, 5),    # +2 behind
            (300, 100, 1, 101, 5),
            (1500, 101, 2, 102, 5),   # below the top again; the 2000 ms check cancels
            (2500, 101, 2, 102, 5)]
    trades = [(300, 100, 4)]          # 4 > 3 ahead -> filled; repost waits for the 1000 ms check
    o = run(book, trades, poll_ms=1000)
    assert [r["status"] for r in o] == ["fill", "cancel", "end"]
    assert [r["tend"] for r in o] == [300, 2000, 2500]
    assert [r["tpost"] for r in o] == [1, 1001, 2001]
    assert [r["reason"] for r in o] == ["start", "after_fill", "after_cancel"]
    assert (o[0]["la_fill"], o[0]["lb_fill"]) == (3, 2)


def test_larger_orders_fill_partially():
    book = [(0, 100, 8, 101, 5),
            (5, 100, 12, 101, 5),     # +4 behind us
            (10, 100, 3, 101, 5),     # after a 9-lot taker sell
            (20, 100, 1, 101, 5),
            (30, 101, 1, 102, 5)]
    trades = [(10, 100, 9),           # 9 > 8 ahead: 1 of our 3 lots fills, we're now at the front
              (20, 100, 2)]           # the last 2 lots fill
    o = run(book, trades, size=3)
    assert (o[0]["status"], o[0]["qfill"], o[0]["tfirst"], o[0]["tend"]) == ("fill", 3, 10, 20)
    assert (o[0]["la_fill"], o[0]["lb_fill"]) == (8, 4)          # queue position at the first fill
    assert (o[1]["status"], o[1]["qfill"], o[1]["tend"]) == ("cancel", 0, 30)


def test_larger_orders_sweep_alone_and_cross():
    book = [(0, 100, 2, 101, 5),
            (10, 99, 4, 101, 5),      # our level swept; we'd be alone at the top
            (20, 99, 4, 100, 3),      # a seller posts at our price: it matches us
            (30, 99, 4, 100, 3)]
    trades = [(10, 100, 2), (10, 99, 3),   # 2 ahead of us consumed, then 3 lots below our price hit us
              (15, 99, 1)]                 # a taker sell while we're alone at the top
    o = run(book, trades, size=5)
    assert (o[0]["status"], o[0]["qfill"], o[0]["kind"], o[0]["tfirst"], o[0]["tend"]) == ("fill", 5, "sweep", 10, 20)
    assert o[0]["qcross"] == 1                                    # the last lot came from the crossing seller
    assert o[1]["px"] == 99 and o[1]["la_post"] == 4


def test_sell_side_frame_and_markouts():
    book = pl.DataFrame({"t": [0, 10, 1010], "bid_px": [100.0, 100.0, 99.0], "bid_qty": [5.0, 5.0, 5.0],
                         "ask_px": [101.0, 101.0, 100.0], "ask_qty": [5.0, 0.0, 5.0]})
    trades = pl.DataFrame({"t": [10], "price": [101.0], "qty": [6.0], "buyer_maker": [False]})
    arrs = side_arrays(book, trades, -1, tick=1.0, lot=1.0)
    df = orders_frame(run_side(arrs, MODELS["pess"], 0, 1), -1, book, tick=1.0, lot=1.0)
    first = df.row(0, named=True)
    assert (first["side"], first["price"], first["status"], first["fill_kind"]) == (-1, 101.0, "fill", "trade")
    # 1 s after the fill the mid is 99.5: a sell at 101 gained (101/99.5 - 1) bp
    assert abs(first["mo_mid_1s"] - (-(99.5 / 101 - 1) * 1e4)) < 1e-9
    assert first["phi"]  # next ask change (to 100) is below our 101: favourable for a sell
