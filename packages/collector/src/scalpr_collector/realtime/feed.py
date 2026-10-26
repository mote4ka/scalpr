import asyncio
import heapq
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from pathlib import Path

import aiohttp
from scalpr_core.logger import logger

from scalpr_collector.orderbook import OrderBook
from scalpr_collector.realtime.events import Event, HeartbeatEvent
from scalpr_collector.realtime.recorder import Recorder, recorded

# no message for this long (pings included) means the connection is dead
RECEIVE_TIMEOUT = 30
MAX_BACKOFF = 30


# a book update came out of order: the local book can't be trusted, the feed reconnects for a fresh snapshot
class Resync(Exception):
    pass


# FEED: one websocket of one exchange for several tickers, local books rebuilt from its messages
# `run` keeps the connection up: reconnects with backoff, starts the books over from new snapshots, records raw
# messages; `handle` turns one decoded message into events and is all that replaying a recording needs
class Feed(ABC):
    exchange: str
    url: str

    def __init__(self, tickers: list[str], depth: int):
        self.tickers, self.depth = tickers, depth
        self.books: dict[str, OrderBook] = {}
        self.reconnects = 0

    async def run(self, session: aiohttp.ClientSession, on_event: Callable[[Event], None], recorder: Recorder | None = None) -> None:
        backoff = 1
        while True:
            try:
                async with session.ws_connect(self.url, heartbeat=None, receive_timeout=RECEIVE_TIMEOUT, max_msg_size=0) as ws:
                    self.reset()
                    await self.subscribe(ws)
                    logger.info(f"{self.exchange}: subscribed to {len(self.tickers)} tickers")
                    keepalive = asyncio.create_task(self.keepalive(ws))
                    try:
                        backoff = 1
                        await self.read(ws, on_event, recorder)
                    finally:
                        keepalive.cancel()
            except Resync as gap:
                logger.warning(f"{self.exchange}: {gap}, reconnecting for fresh snapshots")
            except (aiohttp.ClientError, TimeoutError, ConnectionError) as error:
                logger.warning(f"{self.exchange}: connection lost, {error!r}")
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def read(self, ws: aiohttp.ClientWebSocketResponse, on_event: Callable[[Event], None], recorder: Recorder | None) -> None:
        async for raw in ws:
            if raw.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                break
            received = time.time_ns()
            message = self.decode(raw.data)
            if await self.control(ws, message):
                on_event(HeartbeatEvent(self.exchange, received))
                continue
            if recorder is not None:
                recorder.write(self.exchange, received, message)
            for event in self.handle(message, received):
                on_event(event)

    # REPLAY: recorded messages through `handle`, the books end up as they were live (see replay_together)
    def replay(self, paths: list[Path], on_event: Callable[[Event], None]) -> None:
        replay_together([(self, paths)], on_event)

    # a new connection starts with no books
    def reset(self) -> None:
        self.books = {}

    @abstractmethod
    async def subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None: ...

    # pings and acks: answered or logged here, they are neither recorded nor turned into events
    @abstractmethod
    async def control(self, ws: aiohttp.ClientWebSocketResponse, message: dict) -> bool: ...

    @abstractmethod
    def decode(self, data: str | bytes) -> dict: ...

    @abstractmethod
    def handle(self, message: dict, received: int) -> list[Event]: ...

    # what the exchange needs to keep the connection open
    async def keepalive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        return None


# REPLAY TOGETHER: recordings of several feeds merged by receive time, so the events come in the order they came live;
# a gap that made a live feed reconnect makes its replay start over the same way, the next snapshots rebuild the books
def replay_together(feeds: list[tuple[Feed, list[Path]]], on_event: Callable[[Event], None]) -> None:
    for feed, _ in feeds:
        feed.reset()
    streams = [_tagged(index, paths) for index, (_, paths) in enumerate(feeds)]
    for received, index, message in heapq.merge(*streams, key=lambda item: (item[0], item[1])):
        feed = feeds[index][0]
        try:
            events = feed.handle(message, received)
        except Resync:
            feed.reset()
            continue
        for event in events:
            on_event(event)


# messages of one feed's recording with the feed's index, for merging
def _tagged(index: int, paths: list[Path]) -> Iterator[tuple[int, int, dict]]:
    for received, message in recorded(paths):
        yield received, index, message
