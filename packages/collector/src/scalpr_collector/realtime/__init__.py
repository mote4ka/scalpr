import argparse
import asyncio
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from rich.live import Live
from rich.table import Table
from scalpr_core.logger import console, logger

from scalpr_collector.exchanges import htx as htx_rest
from scalpr_collector.http_client import SessionManager
from scalpr_collector.realtime.bybit import BybitFeed
from scalpr_collector.realtime.events import BookEvent, Event, HeartbeatEvent
from scalpr_collector.realtime.feed import Feed
from scalpr_collector.realtime.htx import HtxFeed
from scalpr_collector.realtime.recorder import STREAM_DIR, Recorder


# uv run --package scalpr-collector stream --tickers DOTUSDT,AVAXUSDT
def main() -> None:
    parser = argparse.ArgumentParser(description="Live Bybit and HTX books and trades of USDT perps, recorded to data/stream")
    parser.add_argument("--tickers", required=True, help="comma separated bybit-style tickers, e.g. DOTUSDT,AVAXUSDT")
    parser.add_argument("--bybit-depth", type=int, default=50, help="bybit book levels: 1, 50 (every 20 ms, default), 200 or 1000")
    parser.add_argument("--htx-depth", type=int, default=20, help="htx book levels: 20 (default) or 150")
    parser.add_argument("--minutes", type=float, default=0, help="stop after this long (default 0: until Ctrl+C)")
    parser.add_argument("--no-record", action="store_true", help="only show the feeds, don't write data/stream")
    parser.add_argument("--out", type=Path, default=STREAM_DIR, help="folder to record to (default data/stream)")
    args = parser.parse_args()
    try:
        asyncio.run(stream(args.tickers.split(","), args.bybit_depth, args.htx_depth, args.minutes, None if args.no_record else args.out))
    except KeyboardInterrupt:
        logger.info("Stopped")


async def stream(tickers: list[str], bybit_depth: int, htx_depth: int, minutes: float, out: Path | None) -> None:
    manager = SessionManager()
    session = await manager.getSession()
    recorder = Recorder(out) if out is not None else None
    tasks: list[asyncio.Task] = []
    try:
        contract_sizes = await htx_rest.get_contract_sizes(session)
        missing = [ticker for ticker in tickers if ticker not in contract_sizes]
        if missing:
            raise SystemExit(f"not traded on htx: {', '.join(missing)}")
        feeds: list[Feed] = [BybitFeed(tickers, bybit_depth), HtxFeed(tickers, contract_sizes, htx_depth)]
        monitor = Monitor(tickers)
        tasks = [asyncio.create_task(feed.run(session, monitor.on_event, recorder)) for feed in feeds]
        if recorder is not None:
            logger.info(f"Recording to {out}")
        started = time.monotonic()
        with Live(monitor.table(feeds), console=console, refresh_per_second=2) as live:
            while not minutes or time.monotonic() - started < minutes * 60:
                await asyncio.sleep(1)
                live.update(monitor.table(feeds))
                if recorder is not None:
                    recorder.flush()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if recorder is not None:
            recorder.close()
        await manager.closeSession()


# what came lately from one exchange for one ticker
@dataclass
class Activity:
    books: int = 0
    trades: int = 0
    bid: float = math.nan
    ask: float = math.nan
    last: int = 0  # local receive time of the last message, ns
    # local receive time - exchange time of recent messages, ms: network delay plus the clock offset
    lags: deque = field(default_factory=lambda: deque(maxlen=200))


# MONITOR: live table of both feeds, one row per exchange and ticker
class Monitor:
    def __init__(self, tickers: list[str]):
        self.tickers = tickers
        self.activity = {(exchange, ticker): Activity() for ticker in tickers for exchange in ("bybit", "htx")}
        self.started = time.time_ns()

    def on_event(self, event: Event) -> None:
        if isinstance(event, HeartbeatEvent):
            return
        activity = self.activity[event.exchange, event.ticker]
        activity.last = event.received
        activity.lags.append(event.received / 1e6 - event.ts)
        if isinstance(event, BookEvent):
            activity.books += 1
            activity.bid, activity.ask = event.book.best_bid(), event.book.best_ask()
        else:
            activity.trades += 1

    def table(self, feeds: list[Feed]) -> Table:
        now = time.time_ns()
        elapsed = max((now - self.started) / 1e9, 1)
        reconnects = ", ".join(f"{feed.exchange} {feed.reconnects}" for feed in feeds)
        table = Table(title=f"Live feeds, {elapsed:.0f} s, reconnects: {reconnects}")
        for column in ("ticker", "exchange", "bid", "ask", "spread %", "htx vs bybit %", "books/s", "trades", "lag ms", "silent s"):
            table.add_column(column, justify="right")
        for ticker in self.tickers:
            bybit, htx = self.activity["bybit", ticker], self.activity["htx", ticker]
            for exchange, activity in (("bybit", bybit), ("htx", htx)):
                mid = (activity.bid + activity.ask) / 2
                deviation = ((htx.bid + htx.ask) / (bybit.bid + bybit.ask) - 1) * 100 if exchange == "htx" else math.nan
                table.add_row(
                    ticker if exchange == "bybit" else "",
                    exchange,
                    f"{activity.bid:.6g}",
                    f"{activity.ask:.6g}",
                    _number((activity.ask - activity.bid) / mid * 100, "{:.3f}"),
                    _number(deviation, "{:+.3f}"),
                    f"{activity.books / elapsed:.1f}",
                    str(activity.trades),
                    _number(statistics.median(activity.lags) if activity.lags else math.nan, "{:.0f}"),
                    _number((now - activity.last) / 1e9 if activity.last else math.nan, "{:.1f}"),
                )
        return table


def _number(value: float, template: str) -> str:
    return "-" if math.isnan(value) else template.format(value)
