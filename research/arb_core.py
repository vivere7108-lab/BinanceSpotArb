"""Core math for cycle arbitrage on the 5-asset Binance spot graph.

Pure functions, no I/O: graph, cycle enumeration, top-of-book screening,
depth-aware sizing, and open-path routing for the rebalancer. The research
scripts use it, and the live engine should be tested against it.

Conventions
-----------
* A leg converts its ``src`` asset into its ``dst`` asset on one symbol.
  SELL = give base, receive quote (hit the bids). BUY = give quote, receive
  base (lift the asks).
* Rates are "units of dst per unit of src". A cycle is profitable when the
  product of its leg rates, after fees, is above 1.
* ``fee`` is the taker fee as a fraction (7.5 bps = 0.00075): one rate for
  every leg, or a mapping symbol -> rate when pairs differ (e.g. a fee-free
  USDCUSDT). It is modelled as taken from the proceeds. With BNB fee payment
  the fee is charged in BNB instead. To first order the cost is the same; the
  live engine books it separately.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import combinations, permutations

ASSETS = ("USDT", "USDC", "BTC", "ETH", "BNB")

# symbol -> (base, quote). Every pair of the five assets has exactly one market,
# so the graph is complete (K5): 10 symbols, 20 directed edges.
SYMBOLS = {
    "BTCUSDT": ("BTC", "USDT"),
    "ETHUSDT": ("ETH", "USDT"),
    "BNBUSDT": ("BNB", "USDT"),
    "USDCUSDT": ("USDC", "USDT"),
    "BTCUSDC": ("BTC", "USDC"),
    "ETHUSDC": ("ETH", "USDC"),
    "BNBUSDC": ("BNB", "USDC"),
    "ETHBTC": ("ETH", "BTC"),
    "BNBBTC": ("BNB", "BTC"),
    "BNBETH": ("BNB", "ETH"),
}


Fee = float | Mapping[str, float]  # one rate for every leg, or a rate per symbol


@dataclass(frozen=True)
class Leg:
    symbol: str
    side: str  # "SELL" or "BUY", from the point of view of the base asset
    src: str
    dst: str


@dataclass(frozen=True)
class Cycle:
    legs: tuple[Leg, ...]

    @property
    def path(self) -> str:
        return "->".join([self.legs[0].src] + [leg.dst for leg in self.legs])

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(leg.symbol for leg in self.legs)

    def rotated_to(self, asset: str) -> "Cycle":
        """Same cycle, started at ``asset``. Gross edge is rotation-invariant;
        only the asset the surplus is counted in changes."""
        i = next(i for i, leg in enumerate(self.legs) if leg.src == asset)
        return Cycle(self.legs[i:] + self.legs[:i])


@dataclass
class Book:
    """Price levels, best first: bids descending, asks ascending. (price, qty in base)."""
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]


def make_leg(src: str, dst: str) -> Leg:
    for sym, (base, quote) in SYMBOLS.items():
        if (base, quote) == (src, dst):
            return Leg(sym, "SELL", src, dst)
        if (base, quote) == (dst, src):
            return Leg(sym, "BUY", src, dst)
    raise KeyError(f"no market between {src} and {dst}")


def enumerate_cycles(min_len: int = 3, max_len: int = 5) -> list[Cycle]:
    """Every directed simple cycle, listed once (started at its lowest-index asset).

    On K5 that is 20 three-leg + 30 four-leg + 24 five-leg = 74 cycles.
    Two-leg cycles (A->B->A on one symbol) always lose the spread, so they are skipped.
    """
    cycles = []
    for k in range(min_len, max_len + 1):
        for nodes in combinations(range(len(ASSETS)), k):
            first, rest = nodes[0], nodes[1:]
            for perm in permutations(rest):
                seq = (first, *perm, first)
                legs = tuple(make_leg(ASSETS[a], ASSETS[b]) for a, b in zip(seq, seq[1:]))
                cycles.append(Cycle(legs))
    return cycles


def leg_fee(fee: Fee, leg: Leg) -> float:
    return fee[leg.symbol] if isinstance(fee, Mapping) else fee


def hurdle(cycle: Cycle, fee: Fee) -> float:
    """Log gross edge the cycle must exceed to break even after fees."""
    return -sum(math.log1p(-leg_fee(fee, leg)) for leg in cycle.legs)


def top_rate(leg: Leg, bid: float, ask: float) -> float:
    """Gross top-of-book conversion rate for one leg (dst per src)."""
    return bid if leg.side == "SELL" else 1.0 / ask


def log_edge(cycle: Cycle, tob: dict[str, tuple[float, float]], fee: Fee = 0.0) -> float:
    """Log of the cycle's top-of-book rate product, net of fees.

    > 0 means profitable at the touch. Cheap enough to run on every book update.
    ``tob`` maps symbol -> (best bid, best ask).
    """
    edge = sum(math.log(top_rate(leg, *tob[leg.symbol])) for leg in cycle.legs)
    return edge - hurdle(cycle, fee)


@dataclass
class Sizing:
    """Result of walking a cycle's depth. Amounts are in each leg's own units."""
    start_in: float = 0.0           # start asset put into leg 1
    start_out: float = 0.0          # start asset returned by the last leg, after fees
    leg_in: list[float] = field(default_factory=list)       # per leg, in leg.src units
    leg_base_qty: list[float] = field(default_factory=list)  # per leg, order quantity in base units
    limit_px: list[float] = field(default_factory=list)     # per leg, worst level touched (IOC limit)

    @property
    def profit(self) -> float:
        return self.start_out - self.start_in


def _leg_levels(leg: Leg, book: Book, fee: float) -> list[tuple[float, float, float]]:
    """Per level: (capacity in leg input units, output per unit input after fee, price)."""
    if leg.side == "SELL":  # spend base, receive quote at each bid
        return [(qty, px * (1.0 - fee), px) for px, qty in book.bids if qty > 0]
    return [(px * qty, (1.0 - fee) / px, px) for px, qty in book.asks if qty > 0]  # spend quote, receive base


def size_cycle(cycle: Cycle, books: dict[str, Book], fee: Fee,
               max_in: float = math.inf, min_edge: float = 0.0) -> Sizing:
    """Find the profit-maximising size for one cycle by walking every leg's depth at once.

    Each leg is a concave piecewise-linear function (output vs input). The cycle is
    their composition, so profit rises while the *marginal* cycle rate is above 1.
    The walk advances the start amount from breakpoint to breakpoint (whichever
    leg's current level runs out first, measured in start-asset units) and stops
    when the marginal rate drops to ``1 + min_edge``, the known depth runs out, or
    ``max_in`` (the packet cap, in start-asset units) is reached.

    ``min_edge`` is a marginal hurdle: liquidity is only taken where the next unit
    still earns at least that much, which keeps a latency buffer on the deep levels.
    """
    levels = [_leg_levels(leg, books[leg.symbol], leg_fee(fee, leg)) for leg in cycle.legs]
    n = len(levels)
    res = Sizing(leg_in=[0.0] * n, leg_base_qty=[0.0] * n, limit_px=[math.nan] * n)
    if any(not lv for lv in levels):
        return res
    idx = [0] * n
    rem = [lv[0][0] for lv in levels]
    while res.start_in < max_in:
        # Multiplier from start-asset units to each leg's input units, at current levels.
        mult, m = [], 1.0
        for lv, j in zip(levels, idx):
            mult.append(m)
            m *= lv[j][1]
        if m <= 1.0 + min_edge:
            break
        step = min(min(r / mu for r, mu in zip(rem, mult)), max_in - res.start_in)
        res.start_in += step
        res.start_out += step * m
        depth_exhausted = False
        for i, leg in enumerate(cycle.legs):
            cap, _, px = levels[i][idx[i]]
            spent = step * mult[i]
            res.leg_in[i] += spent
            # Order quantity is always in base: SELL spends base, BUY receives base (pre-fee).
            res.leg_base_qty[i] += spent if leg.side == "SELL" else spent / px
            res.limit_px[i] = px
            rem[i] -= spent
            if rem[i] <= cap * 1e-12:
                idx[i] += 1
                if idx[i] == len(levels[i]):
                    depth_exhausted = True
                else:
                    rem[i] = levels[i][idx[i]][0]
        if depth_exhausted:
            break
    return res


# --- Open paths: the treasury / rebalancer side --------------------------------------------


def enumerate_paths(src: str, dst: str, max_legs: int = 4) -> list[tuple[Leg, ...]]:
    """Every simple path from ``src`` to ``dst``: 1 + 3 + 6 + 6 = 16 per pair on K5."""
    others = [a for a in ASSETS if a not in (src, dst)]
    paths = []
    for k in range(max_legs):
        for mid in permutations(others, k):
            seq = (src, *mid, dst)
            paths.append(tuple(make_leg(a, b) for a, b in zip(seq, seq[1:])))
    return paths


def fill_path(legs: tuple[Leg, ...], books: dict[str, Book], fee: Fee, amount: float) -> Sizing | None:
    """Push a fixed ``amount`` of the first leg's asset through an open path, walking each book.

    Used for rebalancing and BNB top-ups, where the amount is given and there is no profit to
    maximise. ``start_out`` is in the last leg's ``dst`` asset, so ``profit`` is meaningless here.
    Returns None if the known depth cannot absorb the amount.
    """
    res = Sizing(start_in=amount)
    x = amount
    for leg in legs:
        res.leg_in.append(x)
        out = base = 0.0
        remaining, px = x, math.nan
        for cap, rate, px in _leg_levels(leg, books[leg.symbol], leg_fee(fee, leg)):
            take = min(remaining, cap)
            out += take * rate
            base += take if leg.side == "SELL" else take / px
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            return None
        res.leg_base_qty.append(base)
        res.limit_px.append(px)
        x = out
    res.start_out = x
    return res


def best_path_rate(src: str, dst: str, tob: dict[str, tuple[float, float]], fee: Fee,
                   exclude: str | None = None, max_legs: int = 3) -> tuple[float, tuple[Leg, ...]]:
    """Best top-of-book rate (dst per src, after taker fees) over simple paths, skipping ``exclude``.

    For market making: the price a quote on symbol P is worth is what you could
    flatten a fill for elsewhere in the graph, so call it with ``exclude=P``.
    """
    best: tuple[float, tuple[Leg, ...]] = (0.0, ())
    for legs in enumerate_paths(src, dst, max_legs):
        if any(leg.symbol == exclude for leg in legs):
            continue
        rate = 1.0
        for leg in legs:
            rate *= top_rate(leg, *tob[leg.symbol]) * (1.0 - leg_fee(fee, leg))
        if rate > best[0] * (1 + 1e-12):  # paths come shortest first; on a tie keep fewer legs
            best = (rate, legs)
    return best


def best_route(src: str, dst: str, amount: float, books: dict[str, Book], fee: Fee,
               max_legs: int = 4) -> tuple[tuple[Leg, ...], Sizing] | None:
    """The path that turns ``amount`` of ``src`` into the most ``dst`` (smart order routing)."""
    best = None
    for legs in enumerate_paths(src, dst, max_legs):
        if any(leg.symbol not in books for leg in legs):
            continue
        res = fill_path(legs, books, fee, amount)
        if res and (best is None or res.start_out > best[1].start_out):
            best = (legs, res)
    return best
