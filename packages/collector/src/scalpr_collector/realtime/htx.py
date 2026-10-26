import gzip
import json

import aiohttp
from scalpr_core.logger import logger

from scalpr_collector.orderbook import OrderBook
from scalpr_collector.realtime.events import BookEvent, Event, TradeEvent
from scalpr_collector.realtime.feed import Feed, Resync

# market data of USDT perps, messages come gzipped: https://www.htx.com/en-us/opend/newApiPages/
URL = "wss://api.hbdm.com/linear-swap-ws"
# book depths of the incremental (high_freq) depth channel
DEPTHS = (20, 150)
# right after subscribing htx sends its last trade, however old: trades this much older than their message are
# dropped (the rule works the same on a replay, it uses the receive time)
STALE_TRADE = 5_000  # ms


# HTX FEED: depth.size_<depth>.high_freq (snapshot, then updates whose `version` grows by one) and trade.detail
# sizes come in contracts and are turned into coins; the server pings, we pong
class HtxFeed(Feed):
    exchange = "htx"
    url = URL

    def __init__(self, tickers: list[str], contract_sizes: dict[str, float], depth: int = 20):
        if depth not in DEPTHS:
            raise ValueError(f"htx streams book depths {DEPTHS}, not {depth}")
        super().__init__(tickers, depth)
        self.contract_sizes = contract_sizes
        self.versions: dict[str, int] = {}

    def reset(self) -> None:
        super().reset()
        self.versions = {}

    async def subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        for ticker in self.tickers:
            contract = _contract(ticker)
            await ws.send_json({"sub": f"market.{contract}.depth.size_{self.depth}.high_freq", "data_type": "incremental", "id": f"depth {ticker}"})
            await ws.send_json({"sub": f"market.{contract}.trade.detail", "id": f"trades {ticker}"})

    async def control(self, ws: aiohttp.ClientWebSocketResponse, message: dict) -> bool:
        if "ping" in message:
            await ws.send_json({"pong": message["ping"]})
            return True
        if "status" in message:
            if message["status"] != "ok":
                logger.error(f"htx: {message.get('id')} failed: {message.get('err-msg')}")
            return True
        return False

    def decode(self, data: str | bytes) -> dict:
        return json.loads(gzip.decompress(data) if isinstance(data, bytes) else data)

    def handle(self, message: dict, received: int) -> list[Event]:
        channel, tick = message.get("ch", ""), message.get("tick")
        if tick is None:
            return []
        ticker = channel.split(".")[1].replace("-", "")
        if channel.endswith(".high_freq"):
            snapshot = tick["event"] == "snapshot"
            if snapshot:
                self.books[ticker] = OrderBook(self.depth)
            elif ticker not in self.books:
                return []  # updates before the first snapshot
            elif tick["version"] != self.versions[ticker] + 1:
                raise Resync(f"{ticker} book version {tick['version']} after {self.versions[ticker]}")
            self.versions[ticker] = tick["version"]
            size = self.contract_sizes[ticker]
            book = self.books[ticker]
            book.apply(snapshot, [[price, contracts * size] for price, contracts in tick["bids"]], [[price, contracts * size] for price, contracts in tick["asks"]])
            return [BookEvent(self.exchange, ticker, tick["ts"], received, book)]
        if channel.endswith(".trade.detail"):
            return [
                TradeEvent(self.exchange, ticker, trade["ts"], received, float(trade["price"]), float(trade["quantity"]), trade["direction"], bool(trade.get("is_rpi_trade")))
                for trade in tick["data"] if trade["ts"] >= received // 1_000_000 - STALE_TRADE
            ]
        return []


def _contract(ticker: str) -> str:
    return f"{ticker.removesuffix('USDT')}-USDT"
