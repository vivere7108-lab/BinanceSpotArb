"""Unit tests for arb_core. Run: python -m pytest research/ (or python research/test_arb_core.py)."""
import math
from collections import Counter

from arb_core import (Book, Cycle, best_path_rate, best_route, enumerate_cycles, enumerate_paths, fill_path, hurdle,
                      log_edge, make_leg, size_cycle)


def tri():
    """USDT -> BTC -> ETH -> USDT."""
    return Cycle((make_leg("USDT", "BTC"), make_leg("BTC", "ETH"), make_leg("ETH", "USDT")))


def books(ethbtc_asks=((0.05, 10.0),)):
    # Top of book: (1/100) * (1/0.05) * 5.1 = 1.02, i.e. a 2% gross edge.
    return {
        "BTCUSDT": Book(bids=[(99.0, 5.0)], asks=[(100.0, 1.0), (101.0, 1.0)]),
        "ETHBTC": Book(bids=[(0.049, 10.0)], asks=list(ethbtc_asks)),
        "ETHUSDT": Book(bids=[(5.1, 100.0)], asks=[(5.2, 100.0)]),
    }


def test_cycle_count_on_k5():
    cycles = enumerate_cycles()
    assert Counter(len(c.legs) for c in cycles) == {3: 20, 4: 30, 5: 24}
    # Each directed cycle appears once: no two share the same set of directed edges.
    keys = {frozenset((leg.src, leg.dst) for leg in c.legs) for c in cycles}
    assert len(keys) == 74


def test_leg_direction():
    assert make_leg("USDT", "BTC").side == "BUY"
    assert make_leg("BTC", "USDT").side == "SELL"
    assert make_leg("BNB", "ETH").symbol == "BNBETH"


def test_edge_is_rotation_invariant():
    tob = {"BTCUSDT": (99.0, 100.0), "ETHBTC": (0.049, 0.05), "ETHUSDT": (5.1, 5.2)}
    c = tri()
    assert math.isclose(log_edge(c, tob), math.log(1.02))
    assert math.isclose(log_edge(c.rotated_to("ETH"), tob), log_edge(c, tob))
    assert log_edge(c, tob, fee=0.01) < 0  # 2% gross does not survive 3 x 1% fees


def test_size_stops_at_bottleneck_level():
    s = size_cycle(tri(), books(), fee=0.0)
    # ETHBTC's only level holds 10 ETH = 0.5 BTC = 50 USDT: the bottleneck.
    assert math.isclose(s.start_in, 50.0)
    assert math.isclose(s.start_out, 51.0)
    assert [round(x, 9) for x in s.leg_base_qty] == [0.5, 10.0, 10.0]
    assert s.limit_px == [100.0, 0.05, 5.1]


def test_size_stops_when_marginal_rate_drops_below_one():
    # A second ETHBTC level at 0.052 makes the marginal rate 0.98: do not take it.
    s = size_cycle(tri(), books(ethbtc_asks=((0.05, 10.0), (0.052, 10.0))), fee=0.0)
    assert math.isclose(s.start_in, 50.0)


def test_packet_cap_and_fees():
    s = size_cycle(tri(), books(), fee=0.0, max_in=20.0)
    assert math.isclose(s.start_in, 20.0) and math.isclose(s.profit, 0.4)
    assert size_cycle(tri(), books(), fee=0.01).start_in == 0.0


def test_path_mode_walks_levels_and_routes():
    assert len(enumerate_paths("USDT", "BNB")) == 16
    # 150 USDT -> BTC: 1 BTC at 100, then 50 USDT at 101.
    r = fill_path((make_leg("USDT", "BTC"),), books(), fee=0.0, amount=150.0)
    assert math.isclose(r.start_out, 1.0 + 50.0 / 101.0) and r.limit_px == [101.0]
    assert fill_path((make_leg("USDT", "BTC"),), books(), fee=0.0, amount=500.0) is None  # deeper than the book
    # USDT -> ETH: direct costs 5.2 per ETH; via BTC it costs 100 * 0.05 = 5.0 per ETH, so route via BTC.
    legs, r = best_route("USDT", "ETH", 50.0, books(), fee=0.0, max_legs=2)
    assert [leg.symbol for leg in legs] == ["BTCUSDT", "ETHBTC"]
    assert math.isclose(r.start_out, 10.0)


def test_per_symbol_fee_schedule():
    # Cross-quote triangle through a fee-free USDCUSDT: only two legs pay.
    c = Cycle((make_leg("USDT", "BTC"), make_leg("BTC", "USDC"), make_leg("USDC", "USDT")))
    fees = {"BTCUSDT": 0.00075, "BTCUSDC": 0.0007125, "USDCUSDT": 0.0}
    assert math.isclose(hurdle(c, fees) * 1e4, 14.63, abs_tol=0.01)
    tob = {"BTCUSDT": (99.99, 100.0), "BTCUSDC": (100.2, 100.21), "USDCUSDT": (1.0, 1.0001)}
    gross = log_edge(c, tob)
    assert math.isclose(log_edge(c, tob, fees), gross - hurdle(c, fees))
    assert gross > 0 and log_edge(c, tob, fees) > 0  # 20 bps gross clears the 14.6 bps hurdle


def test_best_path_rate_skips_the_quoted_pair():
    # A flat, consistent market (BTC 100, ETH 5, BNB 1, USDC 1 in USDT) with zero spreads,
    # except that BTCUSDC bids 101: selling BTC via USDC is the best way into USDT.
    px = {"BTCUSDT": 100.0, "ETHUSDT": 5.0, "BNBUSDT": 1.0, "USDCUSDT": 1.0, "BTCUSDC": 100.0,
          "ETHUSDC": 5.0, "BNBUSDC": 1.0, "ETHBTC": 0.05, "BNBBTC": 0.01, "BNBETH": 0.2}
    tob = {s: (p, p) for s, p in px.items()}
    tob["BTCUSDC"] = (101.0, 101.0)
    rate, legs = best_path_rate("BTC", "USDT", tob, 0.0, exclude="BTCUSDT")
    assert math.isclose(rate, 101.0) and [leg.symbol for leg in legs] == ["BTCUSDC", "USDCUSDT"]
    # With the direct pair allowed it still wins via USDC; excluding BTCUSDC falls back to 100.
    assert math.isclose(best_path_rate("BTC", "USDT", tob, 0.0, exclude="BTCUSDC")[0], 100.0)


def test_fair_values():
    from analyze_making import route_fair_values, usdt_fair_values

    px = {"BTCUSDT": 100.0, "ETHUSDT": 5.0, "BNBUSDT": 1.0, "USDCUSDT": 1.0, "BTCUSDC": 100.0,
          "ETHUSDC": 5.0, "BNBUSDC": 1.0, "ETHBTC": 0.05, "BNBBTC": 0.01, "BNBETH": 0.2}
    tob = {s: (p, p) for s, p in px.items()}
    for fair_values in (route_fair_values, usdt_fair_values):  # consistent market: fair = mid
        assert all(math.isclose(v, px[s]) for s, v in fair_values(tob).items())
    tob["BTCUSDC"] = (101.0, 101.0)  # one route for BTCUSDT is now off by 1%
    assert math.isclose(route_fair_values(tob)["BTCUSDT"], 100.0)  # the median of three routes ignores it
    # USDT as the source of truth prices BTCUSDC off BTCUSDT and USDCUSDT, so it shows BTCUSDC 1% rich.
    assert math.isclose(usdt_fair_values(tob)["BTCUSDC"], 100.0)


def test_archive_trades_and_trade_mids():
    from anchors import at, mids, trades

    # Spot archive rows carry microseconds and "True"; perp rows carry milliseconds and "false".
    spot = trades([["1", "10.0", "2", "1", "1", "1700000000000000", "True", "True"]])
    perp = trades([["1", "10.0", "2", "1", "1", "1700000000000", "false"]])
    assert spot == [(1700000000000, 10.0, 2.0, True)] and perp == [(1700000000000, 10.0, 2.0, False)]
    # A seller hits the bid at 9, a buyer lifts the ask at 11: mid 10. A later bid at 10 moves it to 10.5.
    series = mids([(0, 9.0, 1.0, True), (100, 11.0, 1.0, False), (200, 10.0, 1.0, True)])
    assert at(series, 50) is None and at(series, 150) == 10.0 and at(series, 250) == 10.5
    # A side last seen more than 30 s ago doesn't make a mid.
    assert mids([(0, 9.0, 1.0, True), (40_000, 11.0, 1.0, False)]) == ([], [])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
