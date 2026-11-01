import math
import statistics
from collections import deque

import numpy as np

# where a quote is priced from (see quote_prices): "fair" puts it `edge` % from the fair price; "book" also keeps it
# no more than one tick better than the maker book
ANCHORS = ("fair", "book")
SECOND = 1_000_000_000  # ns


# FAIR PRICE on the maker exchange: mid of the hedge exchange moved by the basis
# basis is the htx vs bybit deviation in %, so it shifts bybit up to htx or htx down to bybit
# works on numbers and on numpy arrays alike
def fair_price(hedge_mid, basis, maker: str):
    shift = 1 + basis / 100
    return hedge_mid * shift if maker == "htx" else hedge_mid / shift


# QUOTES: bid and ask `edge` % away from fair on the tick grid; stay a maker: a quote over the other side of the
# book would be a taker order, so it stops a tick short of it
# `shift` % moves the center of both quotes away from fair (see inventory_shift)
# anchor "book": a quote more than a tick better than the book on its side is pulled back to one tick better; it is
# still the best price, so the same taker prints reach it, and the room it gave away is kept as edge; it never gets
# closer to fair than `edge`, and an empty side of the book leaves it where fair puts it
# works on numbers and on numpy arrays alike (NaN in, NaN out)
def quote_prices(fair, edge, tick: float, best_bid, best_ask, anchor: str = "fair", shift=0.0):
    center = fair if np.isscalar(shift) and shift == 0 else fair * (1 + shift / 100)
    bid = np.floor(center * (1 - edge / 100) / tick) * tick
    ask = np.ceil(center * (1 + edge / 100) / tick) * tick
    if anchor == "book":
        # [()] gives numbers back for numbers
        bid = np.where(np.isnan(best_bid), bid, np.minimum(bid, best_bid + tick))[()]
        ask = np.where(np.isnan(best_ask), ask, np.maximum(ask, best_ask - tick))[()]
    bid = np.minimum(bid, best_ask - tick)
    ask = np.maximum(ask, best_bid + tick)
    return bid, ask


# EDGE of the moment: the coin's `edge` plus `vol_edge` times the volatility of the hedge mid (see Volatility, %);
# an unknown volatility adds nothing; numbers or numpy arrays
def quote_edge(edge: float, vol_edge: float, vol):
    if not vol_edge:
        return edge
    return edge + vol_edge * np.nan_to_num(vol)


# INVENTORY SHIFT, % of fair for both quotes: `skew` % at the position limit, in proportion below it and no more above
# it; a long inventory moves the quotes down, so the ask sells it off sooner and the bid buys less; numbers or arrays
def inventory_shift(exposure, max_position: float, skew: float):
    if not skew:
        return 0.0
    return -skew * np.clip(exposure / max_position, -1.0, 1.0)


# VOLATILITY of the hedge mid: std of its 1 s log returns over `window` ns, % (the backtest takes the same returns
# on its 1 s grid); samples come about once a second, a gap longer than 1.5 s starts the returns over from there;
# unknown (NaN) until half the window has returns
class Volatility:
    def __init__(self, window: int):
        self.window = window
        self.needed = max(2, window // SECOND // 2)
        self.returns: deque[tuple[int, float]] = deque()
        self.last: tuple[int, float] | None = None
        self.value = math.nan

    def add(self, now: int, mid: float) -> None:
        if self.last is not None and now - self.last[0] <= 1.5 * SECOND:
            self.returns.append((now, math.log(mid / self.last[1]) * 100))
        self.last = (now, mid)
        while self.returns and self.returns[0][0] <= now - self.window:
            self.returns.popleft()
        self.value = statistics.stdev(r for _, r in self.returns) if len(self.returns) >= self.needed else math.nan
