from collections.abc import Callable, Iterator
from dataclasses import dataclass


@dataclass
class Order:
    id: int  # ours, the client order id
    ticker: str
    bid: bool
    price: float
    volume: int  # htx contracts
    sent: int  # local time of the last request about it (place or cancel), ns
    filled: int = 0  # contracts
    state: str = "new"  # new: sent, no answer yet / live / canceling / done

    @property
    def left(self) -> int:
        return self.volume - self.filled


@dataclass(frozen=True)
class Place:
    order: Order


@dataclass(frozen=True)
class Cancel:
    order: Order


Action = Place | Cancel


# how far a resting order may drift from the wanted one before it is replaced; all zero: it follows every tick
@dataclass(frozen=True)
class Tolerance:
    inside: float = 0.0  # % of price an order may sit closer to fair than wanted (it gives up that much edge)
    outside: float = 0.0  # % further from fair (it misses some fills)
    size: float = 0.0  # share of the wanted contracts an order may be short of (after a partial fill or a price move)


# MAKER ORDERS: our resting quotes of one coin on the maker exchange and the requests that make them match what the
# strategy wants. At most one order per side: while the exchange hasn't answered a place or a cancel that side waits,
# so a slow answer never leaves two orders on one side. HTX can't amend an order: a new price or size is a cancel
# first, then a new order once the cancel is confirmed.
# An order closer to fair than `tolerance.inside` allows, bigger than wanted or not wanted at all is urgent to take
# off; one that only drifted further away or got small (a partial fill) is replaced when the request budget allows
# (`allow(urgent)`, see trader.Budget). With a zero tolerance a partly filled order is replaced at full size, as the
# paper engine refills a quote after a fill.
class MakerOrders:
    def __init__(self, ticker: str, tick: float, ids: Iterator[int], tolerance: Tolerance | None = None):
        self.ticker, self.tick, self.ids, self.tolerance = ticker, tick, ids, tolerance or Tolerance()
        self.sides: dict[bool, Order | None] = {True: None, False: None}  # True: bid

    # `want` per side: (price, contracts), or None for nothing on that side
    def reconcile(self, now: int, want: dict[bool, tuple[float, int] | None], allow: Callable[[bool], bool] = lambda urgent: True) -> list[Action]:
        actions: list[Action] = []
        for bid, target in want.items():
            order = self.sides[bid]
            if order is None:
                if target is not None and allow(False):
                    order = self.sides[bid] = Order(next(self.ids), self.ticker, bid, target[0], target[1], now)
                    actions.append(Place(order))
            elif order.state == "live":
                urgent = self.urgent(order, target)
                if (urgent or self.drifted(order, target)) and allow(urgent):
                    order.state, order.sent = "canceling", now
                    actions.append(Cancel(order))
        return actions

    # not wanted, bigger than allowed, or closer to fair than the tolerance: a risk to take off now
    def urgent(self, order: Order, target: tuple[float, int] | None) -> bool:
        if target is None or order.left > target[1]:
            return True
        sign = 1 if order.bid else -1  # a bid closer to fair is higher, an ask lower
        limit = target[0] * (1 + sign * self.tolerance.inside / 100)
        return sign * (order.price - limit) > self.tick / 2

    # further from fair than the tolerance or short of the wanted size: worth replacing when there is time
    def drifted(self, order: Order, target: tuple[float, int]) -> bool:
        sign = 1 if order.bid else -1
        limit = target[0] * (1 - sign * self.tolerance.outside / 100)
        return sign * (limit - order.price) > self.tick / 2 or order.left < target[1] * (1 - self.tolerance.size) - 1e-9

    def find(self, order_id: int) -> Order | None:
        return next((order for order in self.sides.values() if order is not None and order.id == order_id), None)

    # the exchange took the order (live) or turned it down
    def answered(self, order_id: int, live: bool) -> Order | None:
        order = self.find(order_id)
        if order is not None and order.state == "new":
            if live:
                order.state = "live"
            else:
                self._done(order)
        return order

    # the order is off the book: cancelled, or a cancel found it already gone
    def canceled(self, order_id: int) -> Order | None:
        order = self.find(order_id)
        if order is not None:
            self._done(order)
        return order

    # `contracts` of the order traded; a fully filled order frees its side. A fill can come while the order is still
    # new (the answer is slower than the trade) or being cancelled.
    def filled(self, order_id: int, contracts: int) -> Order | None:
        order = self.find(order_id)
        if order is not None:
            order.filled += contracts
            if order.left <= 0:
                self._done(order)
        return order

    # orders the exchange hasn't answered for longer than `timeout`
    def waiting(self, now: int, timeout: int) -> list[Order]:
        return [order for order in self.sides.values() if order is not None and order.state in ("new", "canceling") and now - order.sent > timeout]

    def _done(self, order: Order) -> None:
        order.state = "done"
        self.sides[order.bid] = None
