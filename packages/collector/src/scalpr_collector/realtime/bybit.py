import asyncio
import json

import aiohttp
from scalpr_core.logger import logger

from scalpr_collector.orderbook import OrderBook
from scalpr_collector.realtime.events import BookEvent, Event, TradeEvent
from scalpr_collector.realtime.feed import Feed, Resync

# public stream of USDT perps: https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
URL = "wss://stream.bybit.com/v5/public/linear"
# book depths bybit streams for linear perps; 50 levels come every 20 ms
DEPTHS = (1, 50, 200, 1000)
# topics per subscribe request
SUBSCRIBE_BATCH = 10
PING_EVERY = 20  # s


# BYBIT FEED: orderbook.<depth> (snapshot, then deltas whose update id `u` grows by one) and publicTrade
class BybitFeed(Feed):
    exchange = "bybit"
    url = URL

    def __init__(self, tickers: list[str], depth: int = 50):
        if depth not in DEPTHS:
            raise ValueError(f"bybit streams book depths {DEPTHS}, not {depth}")
        super().__init__(tickers, depth)
        self.update_ids: dict[str, int] = {}

    def reset(self) -> None:
        super().reset()
        self.update_ids = {}

    async def subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        topics = [topic for ticker in self.tickers for topic in (f"orderbook.{self.depth}.{ticker}", f"publicTrade.{ticker}")]
        for start in range(0, len(topics), SUBSCRIBE_BATCH):
            await ws.send_json({"op": "subscribe", "args": topics[start:start + SUBSCRIBE_BATCH]})

    async def keepalive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(PING_EVERY)
            await ws.send_json({"op": "ping"})

    async def control(self, ws: aiohttp.ClientWebSocketResponse, message: dict) -> bool:
        if "op" not in message:
            return False
        if not message.get("success", True):
            logger.error(f"bybit: {message.get('op')} failed: {message.get('ret_msg')}")
        return True

    def decode(self, data: str | bytes) -> dict:
        return json.loads(data)

    def handle(self, message: dict, received: int) -> list[Event]:
        topic = message.get("topic", "")
        if topic.startswith("orderbook."):
            data = message["data"]
            ticker, update = data["s"], data["u"]
            # update id 1 is a fresh snapshot after a restart on bybit's side
            snapshot = message["type"] == "snapshot" or update == 1
            if snapshot:
                self.books[ticker] = OrderBook(self.depth)
            elif ticker not in self.books:
                return []  # deltas before the first snapshot
            elif update != self.update_ids[ticker] + 1:
                raise Resync(f"{ticker} book update {update} after {self.update_ids[ticker]}")
            self.update_ids[ticker] = update
            book = self.books[ticker]
            book.apply(snapshot, data["b"], data["a"])
            return [BookEvent(self.exchange, ticker, message["ts"], received, book)]
        if topic.startswith("publicTrade."):
            return [
                TradeEvent(self.exchange, trade["s"], trade["T"], received, float(trade["p"]), float(trade["v"]), trade["S"].lower(), bool(trade.get("RPI")))
                for trade in message["data"]
            ]
        return []
