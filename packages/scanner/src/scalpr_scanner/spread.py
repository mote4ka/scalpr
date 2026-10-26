from datetime import date

import numpy as np
from pandas import (
    DataFrame,
    DatetimeIndex,
    Index,
    NaT,
    Series,
    Timedelta,
    Timestamp,
    date_range,
)

TRADE_COLUMNS = ["entry", "buy on", "excess %", "entry %", "exit", "held", "closing %", "pnl %"]


# VWAP: average fill price of `notional` USDT taken from one side of the book (ask to buy, bid to sell)
# NaN when the loaded depth is too thin to fill it
def vwap(frame: DataFrame, side: str, notional: float) -> Series:
    px = frame.filter(regex=f"^{side}_px_").to_numpy()
    sz = frame.filter(regex=f"^{side}_sz_").to_numpy()
    level_notional = np.nan_to_num(px * sz)
    before = np.cumsum(level_notional, axis=1) - level_notional
    # each level gives what is still left to fill, up to its own size
    used = np.clip(notional - before, 0, level_notional)
    qty = np.divide(used, px, out=np.zeros_like(used), where=used > 0).sum(axis=1)
    filled = level_notional.sum(axis=1) >= notional
    price = np.divide(notional, qty, out=np.full_like(qty, np.nan), where=filled)
    return Series(price, index=frame.index)


# NATIVE PRICES: mid, best and VWAP prices of one exchange after every book update
def native_prices(frame: DataFrame, notional: float) -> DataFrame:
    prices = DataFrame({
        "mid": (frame["bid_px_0"] + frame["ask_px_0"]) / 2,
        "bid": frame["bid_px_0"],
        "ask": frame["ask_px_0"],
        "vwap_buy": vwap(frame, "ask", notional),
        "vwap_sell": vwap(frame, "bid", notional),
    })
    # several updates can share a ts, the last one is the book state
    return prices[~prices.index.duplicated(keep="last")]


# GRID PRICES: native prices as-of every grid point
def grid_prices(native: DataFrame, grid: DatetimeIndex) -> DataFrame:
    # row-wise as-of: a NaN (book too thin at that moment) is kept, not filled from older rows
    prices = native.reindex(grid, method="ffill")
    # after the data ends (partial day) the last book would be stretched over the rest of the grid
    prices[prices.index > native.index.max()] = np.nan
    return prices


# EXCHANGE PRICES: native and grid prices of both exchanges, keyed by exchange
# the grid covers the UTC `day`, or [start, end) when given
def exchange_prices(bybit: DataFrame, htx: DataFrame, day: date | None, notional: float, freq: str,
                    start: Timestamp | None = None, end: Timestamp | None = None) -> tuple[dict[str, DataFrame], dict[str, DataFrame]]:
    if start is None:
        start = Timestamp(day, tz="UTC")
        end = start + Timedelta(days=1)
    grid = date_range(start, end, freq=freq, inclusive="left", name="ts")
    native = {"bybit": native_prices(bybit, notional), "htx": native_prices(htx, notional)}
    return native, {exchange: grid_prices(prices, grid) for exchange, prices in native.items()}


# SPREADS: % gained by buying on one exchange and selling on the other at the same moment with taker orders, before fees
def spread_frame(prices: dict[str, DataFrame]) -> DataFrame:
    b, h = prices["bybit"], prices["htx"]
    bybit_to_htx = (h["vwap_sell"] / b["vwap_buy"] - 1) * 100
    htx_to_bybit = (b["vwap_sell"] / h["vwap_buy"] - 1) * 100
    return DataFrame({
        "mid_bybit": b["mid"],
        "mid_htx": h["mid"],
        # how far htx mid is from bybit mid, % (positive: htx is more expensive)
        "deviation": (h["mid"] / b["mid"] - 1) * 100,
        "bybit_to_htx": bybit_to_htx,
        "htx_to_bybit": htx_to_bybit,
        # buy where it's cheaper, sell where it's more expensive: only one direction can be positive at a time
        "spread": np.fmax(bybit_to_htx, htx_to_bybit),
    })


# CROSSING COST: % lost by buying and selling back at once on one exchange with taker orders (its spread + slippage)
def crossing_cost(prices: DataFrame) -> float:
    return ((prices["vwap_buy"] / prices["vwap_sell"] - 1) * 100).median()


# WINDOWS: stretches of the grid where buying on the cheaper exchange and selling on the other beats fees
# aligned after: from window start until that spread is back to <= 0 (prices are level again)
# exit after: until the reverse trades close the position without loss: entry % + closing % >= fees
def windows(spreads: DataFrame, fees: float) -> DataFrame:
    index = spreads.index
    step = index[1] - index[0]
    rows = []
    for buy_on, column, closing_column in (("Bybit", "bybit_to_htx", "htx_to_bybit"), ("HTX", "htx_to_bybit", "bybit_to_htx")):
        spread = spreads[column].to_numpy()
        closing = spreads[closing_column].to_numpy()
        above = spread > fees
        starts = np.flatnonzero(above & ~np.r_[False, above[:-1]])
        ends = np.flatnonzero(above & ~np.r_[above[1:], False])
        aligned = _next_true(spread <= 0)
        for start, end in zip(starts, ends, strict=True):
            entry = spread[start]
            exits = np.flatnonzero(closing[start + 1:] >= fees - entry)
            rows.append({
                "start": index[start],
                "buy on": buy_on,
                "entry %": entry,
                "peak %": spread[start:end + 1].max(),
                "above fees": index[end] - index[start] + step,
                # NaT: didn't happen before the data ends
                "aligned after": index[aligned[start]] - index[start] if aligned[start] < len(index) else NaT,
                "exit after": index[start + 1 + exits[0]] - index[start] if len(exits) else NaT,
            })
    columns = ["start", "buy on", "entry %", "peak %", "above fees", "aligned after", "exit after"]
    return DataFrame(rows, columns=columns).sort_values("start", ignore_index=True)


# BASIS: normal level of the deviation, rolling median of the past `window` (no look-ahead)
def basis(spreads: DataFrame, window: str, warmup: str | None = None) -> Series:
    step = spreads.index[1] - spreads.index[0]
    # warm-up: no basis until `warmup` (by default half of the window) is seen
    warm_up = max(1, int(Timedelta(warmup) / step) if warmup is not None else int(Timedelta(window) / step / 2))
    return spreads["deviation"].rolling(window, min_periods=warm_up).median()


# BASIS TRADES: one position at a time, enter when the deviation leaves the basis by more than `entry` %,
# exit when it is back at the basis; above the basis htx is too expensive -> buy Bybit, sell HTX, and mirrored
# pnl % ~ entry spread + closing spread - fees, with the execution prices of the chosen modes
def basis_trades(spreads: DataFrame, basis: Series, entry: float, fees: float) -> DataFrame:
    index = spreads.index
    excess = (spreads["deviation"] - basis).to_numpy()
    bybit_to_htx = spreads["bybit_to_htx"].to_numpy()
    htx_to_bybit = spreads["htx_to_bybit"].to_numpy()
    rows = []
    opened = side = entry_spread = None
    for i in range(len(index)):
        if opened is None:
            if excess[i] > entry and not np.isnan(bybit_to_htx[i]):
                opened, side, entry_spread = i, 1, bybit_to_htx[i]
            elif excess[i] < -entry and not np.isnan(htx_to_bybit[i]):
                opened, side, entry_spread = i, -1, htx_to_bybit[i]
            continue
        # closing does the reverse trades
        closing = htx_to_bybit[i] if side == 1 else bybit_to_htx[i]
        if side * excess[i] <= 0 and not np.isnan(closing):
            rows.append(_trade(index, opened, i, side, excess[opened], entry_spread, closing, fees))
            opened = None
    if opened is not None:
        # still open when the data ends: no exit, no pnl
        rows.append(_trade(index, opened, None, side, excess[opened], entry_spread, np.nan, fees))
    return DataFrame(rows, columns=TRADE_COLUMNS)


def _trade(index: Index, opened: int, closed: int | None, side: int, excess: float, entry_spread: float, closing: float, fees: float) -> dict:
    return {
        "entry": index[opened],
        "buy on": "Bybit" if side == 1 else "HTX",
        "excess %": excess,
        "entry %": entry_spread,
        "exit": index[closed] if closed is not None else NaT,
        "held": index[closed] - index[opened] if closed is not None else NaT,
        "closing %": closing,
        "pnl %": entry_spread + closing - fees,
    }


# index of the first True at or after every position, len(mask) when there is none
def _next_true(mask: np.ndarray) -> np.ndarray:
    positions = np.where(mask, np.arange(len(mask)), len(mask))
    return np.minimum.accumulate(positions[::-1])[::-1]
