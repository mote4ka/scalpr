import math
from array import array
from collections.abc import Iterable
from pathlib import Path

import numpy as np
from pandas import DataFrame, read_parquet, to_datetime

from scalpr_collector.storage import write_parquet

# one book message: (ts in ms, is_snapshot, bids, asks), levels are [price, size] string pairs
Message = tuple[int, bool, list[list[str]], list[list[str]]]


# LOCAL L2 BOOK: rebuilt from snapshot + delta messages
class OrderBook:
    def __init__(self, max_levels: int):
        # feed depth (bybit 200, htx 150): levels that fall out of it get no more updates, so they are trimmed
        self.max_levels = max_levels
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def apply(self, is_snapshot: bool, bids: list[list[str]], asks: list[list[str]]) -> None:
        if is_snapshot:
            self.bids.clear()
            self.asks.clear()
        for side, levels in ((self.bids, bids), (self.asks, asks)):
            for price, size in levels:
                price, size = float(price), float(size)
                # zero size removes the level
                if size == 0:
                    side.pop(price, None)
                else:
                    side[price] = size
        for side, worst_first in ((self.bids, False), (self.asks, True)):
            if len(side) > self.max_levels:
                for price in sorted(side, reverse=worst_first)[:len(side) - self.max_levels]:
                    del side[price]

    # best `depth` prices of each side: bids high to low, asks low to high
    def top(self, depth: int) -> tuple[list[float], list[float]]:
        return sorted(self.bids, reverse=True)[:depth], sorted(self.asks)[:depth]

    # best prices without sorting the whole book, NaN for an empty side
    def best_bid(self) -> float:
        return max(self.bids, default=math.nan)

    def best_ask(self) -> float:
        return min(self.asks, default=math.nan)


def book_columns(depth: int) -> list[str]:
    return [f"{side}_{kind}_{i}" for side in ("bid", "ask") for kind in ("px", "sz") for i in range(depth)]


# BOOK FRAME: replays messages and keeps top `depth` levels after every message with start_ms <= ts < end_ms
# index ts (UTC), columns bid_px_0.., bid_sz_0.., ask_px_0.., ask_sz_0.. (level 0 is the best), missing levels are NaN
# size_mult converts sizes to coins (htx sends them in contracts)
def book_frame(messages: Iterable[Message], depth: int, max_levels: int, start_ms: int, end_ms: int, size_mult: float = 1.0) -> DataFrame:
    book = OrderBook(max_levels)
    # flat float buffer instead of list of rows: ~8 bytes per value vs ~30 for python floats
    rows = array("d")
    nan_pad = [math.nan] * depth

    for ts, is_snapshot, bids, asks in messages:
        if ts >= end_ms:
            break
        book.apply(is_snapshot, bids, asks)
        if ts < start_ms:
            continue
        bid_prices, ask_prices = book.top(depth)
        rows.append(ts)
        for side, prices in ((book.bids, bid_prices), (book.asks, ask_prices)):
            missing = nan_pad[len(prices):]
            rows.extend(prices)
            rows.extend(missing)
            rows.extend(side[price] * size_mult for price in prices)
            rows.extend(missing)

    data = np.frombuffer(rows, dtype=np.float64).reshape(-1, 1 + 4 * depth)
    index = to_datetime(data[:, 0].astype(np.int64), unit="ms", utc=True).rename("ts")
    return DataFrame(data[:, 1:], index=index, columns=book_columns(depth))


# PARQUET CACHE: parsed frames are stored as {stem}_d{depth}.parquet next to the archives
# any cached depth >= requested is reused, parquet reads only the needed columns
def load_frame(directory: Path, stem: str, depth: int) -> DataFrame | None:
    for path in sorted(directory.glob(f"{stem}_d*.parquet"), key=_cached_depth):
        if _cached_depth(path) >= depth:
            return read_parquet(path, columns=book_columns(depth))
    return None


def save_frame(frame: DataFrame, directory: Path, stem: str, depth: int) -> Path:
    return write_parquet(frame, directory / f"{stem}_d{depth}.parquet")


def _cached_depth(path: Path) -> int:
    return int(path.stem.rsplit("_d", 1)[1])
