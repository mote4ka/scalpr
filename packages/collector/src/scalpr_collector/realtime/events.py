from dataclasses import dataclass

from scalpr_collector.orderbook import OrderBook


# the local book of `ticker` after an update; `book` is the live object, read it right away
@dataclass(slots=True)
class BookEvent:
    exchange: str
    ticker: str  # bybit-style, ACEUSDT
    ts: int  # exchange time of the update, ms
    received: int  # local wall clock when the message came, ns
    book: OrderBook  # sizes in coins


@dataclass(slots=True)
class TradeEvent:
    exchange: str
    ticker: str
    ts: int  # exchange time of the trade, ms
    received: int  # ns
    price: float
    size: float  # coins
    side: str  # side of the taker: buy / sell
    rpi: bool  # filled a retail-only order that isn't in the public book: a regular maker order never gets it


# a ping or pong of the connection: the exchange is there even when its books are quiet; live only, not recorded
@dataclass(slots=True)
class HeartbeatEvent:
    exchange: str
    received: int  # ns


Event = BookEvent | TradeEvent | HeartbeatEvent
