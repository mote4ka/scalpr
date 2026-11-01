import pytest
from pandas import DataFrame, Series, Timedelta, Timestamp, date_range
from scalpr_collector.orderbook import book_columns
from scalpr_scanner.spread import exchange_prices, spread_frame
from scalpr_scanner.xemm import xemm

START = Timestamp("2026-10-20 00:00", tz="UTC")


# one-level books: every half second, the htx bid steps between 99.50 and 99.51 so the backtest sees a 0.01 tick
def books() -> tuple[DataFrame, DataFrame]:
    index = date_range(START, periods=20, freq="500ms", name="ts")
    columns = book_columns(1)
    # book_columns order: bid price, bid size, ask price, ask size
    bybit = DataFrame([[99.99, 1000.0, 100.01, 1000.0]] * len(index), index=index, columns=columns)
    htx_rows = [[99.50 if i % 2 == 0 else 99.51, 1000.0, 100.50, 1000.0] for i in range(len(index))]
    htx = DataFrame(htx_rows, index=index, columns=columns)
    return bybit, htx


def run(**options) -> DataFrame:
    bybit, htx = books()
    native, grid = exchange_prices(bybit, htx, None, 100, "1s", START, START + Timedelta("10s"))
    spreads = spread_frame(grid)
    basis = Series(0.0, index=spreads.index)
    # a taker sell at the htx best bid, 6.2 s in: our bid stands by then
    trades = DataFrame({"price": [99.50], "size": [0.5], "side": ["sell"]}, index=[START + Timedelta("6.2s")]).rename_axis("ts")
    return xemm(spreads, basis, native, grid, trades, "htx", 100, 0.2, 300, "through", "100ms", "tick", "20ms", **options).fills


def test_columns_are_the_ones_the_books_are_built_with():
    assert book_columns(1) == ["bid_px_0", "bid_sz_0", "ask_px_0", "ask_sz_0"]


def test_fair_anchor_fills_at_fair_minus_edge():
    fills = run()
    assert len(fills) == 1
    assert fills["price"].iloc[0] == pytest.approx(99.80)
    assert fills["hedge"].iloc[0] == pytest.approx(99.99)


def test_book_anchor_fills_one_tick_better_than_the_book():
    fills = run(anchor="book")
    assert len(fills) == 1
    # the quote was made 100 ms before the print, at 6.1 s, from the 6.0 s book: htx bid 99.50
    assert fills["price"].iloc[0] == pytest.approx(99.51)
    assert fills["edge %"].iloc[0] > 0.4
