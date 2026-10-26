import math
from array import array
from pathlib import Path

import numpy as np
from pandas import DataFrame, to_datetime
from scalpr_collector.orderbook import book_columns
from scalpr_collector.realtime.bybit import BybitFeed
from scalpr_collector.realtime.events import BookEvent, Event, TradeEvent
from scalpr_collector.realtime.feed import replay_together
from scalpr_collector.realtime.htx import HtxFeed
from scalpr_trader import BYBIT_DEPTH, HTX_DEPTH


# STREAM FRAMES: a recording of the stream command (or trader --record) as backtest input, in the shapes the archive
# loaders give: per ticker the bybit and htx books (top `depth` levels after every update) and the htx trades, all
# indexed by the local receive time, the clock the live trader works on; the feeds are the trader's, so the books are
# the ones it saw
def stream_frames(folder: Path, tickers: list[str], contract_sizes: dict[str, float], depth: int = 20) -> dict[str, tuple[DataFrame, DataFrame, DataFrame]]:
    feeds = [BybitFeed(tickers, BYBIT_DEPTH), HtxFeed(tickers, contract_sizes, HTX_DEPTH)]
    # ns timestamps don't fit a float64 exactly, they go apart from the levels
    times = {(exchange, ticker): [] for exchange in ("bybit", "htx") for ticker in tickers}
    levels = {key: array("d") for key in times}
    trades = {ticker: {"ts": [], "price": [], "size": [], "side": []} for ticker in tickers}
    nan_pad = [math.nan] * depth

    def on_event(event: Event) -> None:
        if isinstance(event, BookEvent):
            key = (event.exchange, event.ticker)
            book, row = event.book, levels[key]
            times[key].append(event.received)
            for side, prices in zip((book.bids, book.asks), book.top(depth), strict=True):
                missing = nan_pad[len(prices):]
                row.extend(prices)
                row.extend(missing)
                row.extend(side[price] for price in prices)
                row.extend(missing)
        elif isinstance(event, TradeEvent) and event.exchange == "htx" and not event.rpi:
            tape = trades[event.ticker]
            for column, value in (("ts", event.received), ("price", event.price), ("size", event.size), ("side", event.side)):
                tape[column].append(value)

    replay_together([(feed, sorted((folder / feed.exchange).rglob("*.jsonl.gz"))) for feed in feeds], on_event)

    frames = {}
    for ticker in tickers:
        books = []
        for exchange in ("bybit", "htx"):
            key = (exchange, ticker)
            values = np.frombuffer(levels[key], dtype=np.float64).reshape(-1, 4 * depth)
            books.append(DataFrame(values, index=to_datetime(times[key], unit="ns", utc=True).rename("ts"), columns=book_columns(depth)))
        tape = trades[ticker]
        frames[ticker] = (*books, DataFrame(
            {"price": tape["price"], "size": tape["size"], "side": tape["side"]},
            index=to_datetime(tape["ts"], unit="ns", utc=True).rename("ts"),
        ))
    return frames
