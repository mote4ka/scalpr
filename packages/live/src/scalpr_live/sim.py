import heapq
import math
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
from typing import Protocol

from scalpr_collector.orderbook import OrderBook
from scalpr_collector.realtime.events import TradeEvent
from scalpr_strategy import filled
from scalpr_trader.config import Config
from scalpr_trader.engine import Engine

from scalpr_live.orders import Order
from scalpr_live.specs import Spec, steps


# what an exchange (real or simulated) tells the trader
class Listener(Protocol):
    def answered(self, ticker: str, order_id: int, live: bool, now: int) -> None: ...
    def canceled(self, ticker: str, order_id: int, now: int) -> None: ...
    def filled(self, ticker: str, order_id: int, contracts: int, price: float, now: int) -> None: ...
    def hedged(self, ticker: str, sent: float, filled: float, price: float, now: int) -> None: ...


# an order as the simulated maker exchange holds it
@dataclass
class Resting:
    id: int
    ticker: str
    bid: bool
    price: float
    left: int  # contracts


# SIM: both exchanges for a dry run, nothing is sent anywhere. A place goes live and a cancel takes effect
# `quote_latency` after it was sent (the answer comes then too); a maker trade through a live order fills it by the
# paper engine's rule (scalpr_strategy.filled, the maker book right before the trade from the engine); a hedge fills
# `hedge_latency` after it was sent at the hedge book of then, IOC up to its limit price
class SimExchange:
    def __init__(self, engine: Engine, config: Config, specs: dict[str, Spec], listener: Listener):
        self.engine, self.config, self.specs, self.listener = engine, config, specs, listener
        self.resting: dict[int, Resting] = {}
        self.due: list[tuple[int, int, Callable[[int], None]]] = []  # (time, order of sending, what happens then)
        self.order = count()

    def place(self, order: Order, now: int) -> None:
        resting = Resting(order.id, order.ticker, order.bid, order.price, order.volume)
        self._at(now + self.config.quote_latency, lambda at: self._live(resting, at))

    def cancel(self, order: Order, now: int) -> None:
        self._at(now + self.config.quote_latency, lambda at: self._cancel(order.ticker, order.id, at))

    # `coins` to buy (+) or sell (-), no worse than `limit`
    def hedge(self, ticker: str, coins: float, limit: float, now: int) -> None:
        self._at(now + self.config.hedge_latency, lambda at: self._hedge(ticker, coins, limit, at))

    # what was due by `now` happens, in the order it was due
    def tick(self, now: int) -> None:
        while self.due and self.due[0][0] <= now:
            at, _, happen = heapq.heappop(self.due)
            happen(at)

    # a maker trade meets the orders live when it arrives
    def on_trade(self, trade: TradeEvent) -> None:
        self.tick(trade.received)
        sells = trade.side == "sell"  # a taker sell can hit our bid
        state = self.engine.coins.get(trade.ticker)
        if state is None:
            return
        best = state.maker_best_before(trade.ts, sells)
        if math.isnan(best):
            return
        spec = self.specs[trade.ticker]
        for resting in list(self.resting.values()):
            if resting.ticker != trade.ticker or resting.bid != sells:
                continue
            coins = filled(trade.price, trade.size, resting.price, best, resting.left * spec.contract, sells, self.config.fill, spec.tick)
            contracts = min(resting.left, steps(coins, spec.contract))
            if contracts <= 0:
                continue
            resting.left -= contracts
            if resting.left <= 0:
                del self.resting[resting.id]
            self.listener.filled(trade.ticker, resting.id, contracts, resting.price, trade.received)

    def _at(self, at: int, happen: Callable[[int], None]) -> None:
        heapq.heappush(self.due, (at, next(self.order), happen))

    def _live(self, resting: Resting, at: int) -> None:
        self.resting[resting.id] = resting
        self.listener.answered(resting.ticker, resting.id, True, at)

    def _cancel(self, ticker: str, order_id: int, at: int) -> None:
        self.resting.pop(order_id, None)
        self.listener.canceled(ticker, order_id, at)

    def _hedge(self, ticker: str, coins: float, limit: float, at: int) -> None:
        book = self.engine.coins[ticker].books[self.config.hedge]
        qty, price = take(book, coins, limit) if book is not None else (0.0, math.nan)
        self.listener.hedged(ticker, coins, qty, price, at)


# an IOC order on `book`: buy (+) or sell (-) `coins` no worse than `limit`; returns (signed coins filled, average price)
def take(book: OrderBook, coins: float, limit: float) -> tuple[float, float]:
    buy = coins > 0
    levels = sorted(book.asks.items()) if buy else sorted(book.bids.items(), reverse=True)
    left, got, cost = abs(coins), 0.0, 0.0
    for price, size in levels:
        if (price > limit) if buy else (price < limit):
            break
        part = min(left, size)
        got, cost, left = got + part, cost + part * price, left - part
        if left <= 1e-12:
            break
    if got <= 0:
        return 0.0, math.nan
    return math.copysign(round(got, 10), coins), cost / got
