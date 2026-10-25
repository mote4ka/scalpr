import numpy as np
from pandas import DataFrame, Series, Timedelta, Timestamp

from scalpr_scanner.spread import FEES

FILL_RULES = ("through", "touch")
LABELS = {"bybit": "Bybit", "htx": "HTX"}
MAKER_COLUMNS = ["status", "signal", "entry", "buy on", "excess %", "wait", "entry %", "exit", "exit by", "held", "closing %", "pnl %"]
# a book lookup later than this after the last update means the data has ended (partial day)
DATA_END_TOLERANCE = Timedelta("1min").value


# BOOK: native prices of one exchange with as-of lookups at any moment (ns timestamps)
class _Book:
    def __init__(self, prices: DataFrame):
        self.ts = prices.index.as_unit("ns").asi8
        self.columns = {column: prices[column].to_numpy() for column in prices.columns}

    def at(self, ts: int, column: str) -> float:
        i = np.searchsorted(self.ts, ts, side="right") - 1
        if i < 0 or ts > self.ts[-1] + DATA_END_TOLERANCE:
            return np.nan
        return self.columns[column][i]


# TRADES: tape of the maker exchange, tells when a resting limit order would have been filled
class _Tape:
    def __init__(self, trades: DataFrame):
        self.ts = trades.index.as_unit("ns").asi8
        self.price = trades["price"].to_numpy()
        self.taker_sells = (trades["side"] == "sell").to_numpy()

    # first fill in (start, end) of a maker order at `price`: a maker buy is hit by taker sells, a sell by taker buys
    # "through": the print is past our price, so the whole level was eaten wherever we were in the queue
    # "touch": a print at our price is enough, as if we were first in the queue (optimistic)
    def first_fill(self, start: int, end: int, price: float, maker_buys: bool, rule: str) -> int | None:
        a, b = np.searchsorted(self.ts, start, side="right"), np.searchsorted(self.ts, end, side="left")
        prints = self.price[a:b]
        if maker_buys:
            hit = self.taker_sells[a:b] & ((prints < price) if rule == "through" else (prints <= price))
        else:
            hit = ~self.taker_sells[a:b] & ((prints > price) if rule == "through" else (prints >= price))
        found = np.flatnonzero(hit)
        return int(self.ts[a + found[0]]) if len(found) else None


# MAKER TRADES: basis reversion where `maker` exchange quotes and the other one hedges with taker orders
# entry: when the deviation leaves the basis by more than `entry` %, a limit order rests at the maker exchange's best
#   price on our side; it is filled only by a trade from the tape (see _Tape.first_fill) while the signal lasts,
#   otherwise it is cancelled; after the fill the other exchange is hit with a taker order `latency` later
# exit: back at the basis a limit order closes the maker leg the same way; with no fill in `exit_timeout` the maker
#   leg is closed with a taker order; the hedge is always closed with a taker order
def maker_trades(spreads: DataFrame, basis: Series, entry: float, native: dict[str, DataFrame], trades: DataFrame,
                 maker: str, rule: str, exit_timeout: str, latency: str) -> DataFrame:
    taker = "htx" if maker == "bybit" else "bybit"
    books = {exchange: _Book(prices) for exchange, prices in native.items()}
    tape = _Tape(trades)
    grid = spreads.index.as_unit("ns").asi8
    step = grid[1] - grid[0]
    excess = (spreads["deviation"] - basis).to_numpy()
    timeout, delay = Timedelta(exit_timeout).value, Timedelta(latency).value
    rows = []
    i, n = 0, len(grid)
    while i < n:
        # side 1: htx is too expensive -> buy Bybit, sell HTX; side -1 mirrored
        side = 1 if excess[i] > entry else -1 if excess[i] < -entry else 0
        if side == 0:
            i += 1
            continue
        buy_on = "bybit" if side == 1 else "htx"
        maker_buys = maker == buy_on
        # the signal lasts while the deviation stays beyond the entry level on the same side
        end = i
        while end < n and side * excess[end] > entry:
            end += 1
        signal = {"signal": _time(grid[i]), "buy on": LABELS[buy_on], "excess %": excess[i]}

        quote = books[maker].at(grid[i], "bid" if maker_buys else "ask")
        signal_end = grid[end] if end < n else grid[-1] + step
        filled = tape.first_fill(grid[i], signal_end, quote, maker_buys, rule) if not np.isnan(quote) else None
        if filled is None:
            rows.append({**signal, "status": "missed"})
            i = end
            continue
        hedge = books[taker].at(filled + delay, "vwap_sell" if maker_buys else "vwap_buy")
        opened = {**signal, "entry": _time(filled), "wait": Timedelta(filled - grid[i])}
        if np.isnan(hedge):
            rows.append({**opened, "status": "no hedge"})
            i = np.searchsorted(grid, filled, side="right")
            continue

        # hold until the deviation is back at the basis (NaN: no data, keep waiting)
        k = np.searchsorted(grid, filled + delay)
        while k < n and not side * excess[k] <= 0:
            k += 1
        if k == n:
            rows.append({**opened, "status": "open"})
            break
        exit_quote = books[maker].at(grid[k], "ask" if maker_buys else "bid")
        exit_fill = tape.first_fill(grid[k], grid[k] + timeout, exit_quote, not maker_buys, rule) if not np.isnan(exit_quote) else None
        if exit_fill is not None:
            exited, exit_by, exit_price = exit_fill, "maker", exit_quote
            exit_hedge = books[taker].at(exit_fill + delay, "vwap_buy" if maker_buys else "vwap_sell")
        else:
            # no fill in time: both legs are closed with taker orders at once
            exited, exit_by = grid[k] + timeout, "taker"
            exit_price = books[maker].at(exited, "vwap_sell" if maker_buys else "vwap_buy")
            exit_hedge = books[taker].at(exited, "vwap_buy" if maker_buys else "vwap_sell")

        # prices on the exchange we buy on (a) and sell on (b)
        if maker_buys:
            buy_a, sell_b, sell_a, buy_b = quote, hedge, exit_price, exit_hedge
        else:
            sell_b, buy_a, buy_b, sell_a = quote, hedge, exit_price, exit_hedge
        entry_spread = (sell_b / buy_a - 1) * 100
        closing = (sell_a / buy_b - 1) * 100
        fees = FEES[maker]["maker"] + FEES[maker][exit_by] + 2 * FEES[taker]["taker"]
        rows.append({
            **opened,
            "status": "closed",
            "entry %": entry_spread,
            "exit": _time(exited),
            "exit by": exit_by,
            "held": Timedelta(exited - filled),
            "closing %": closing,
            "pnl %": entry_spread + closing - fees,
        })
        i = np.searchsorted(grid, exited, side="right")
    return DataFrame(rows, columns=MAKER_COLUMNS).astype({
        "signal": "datetime64[ns, UTC]", "entry": "datetime64[ns, UTC]", "exit": "datetime64[ns, UTC]",
        "wait": "timedelta64[ns]", "held": "timedelta64[ns]",
    })


def _time(ns: int) -> Timestamp:
    return Timestamp(ns, tz="UTC")
