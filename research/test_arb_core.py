"""Unit tests for arb_core. Run: python -m pytest research/ (or python research/test_arb_core.py)."""
import math
from collections import Counter

from arb_core import (Book, Cycle, best_route, enumerate_cycles, enumerate_paths, fill_path, log_edge,
                      make_leg, size_cycle)


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
