import math
import time
from collections import Counter
from dataclasses import dataclass, field
from itertools import count
from typing import Protocol

from scalpr_collector.realtime.events import Event, TradeEvent
from scalpr_core.logger import logger
from scalpr_trader.config import Coin, Config
from scalpr_trader.engine import Engine
from scalpr_trader.journal import Journal

from scalpr_live.account import Account
from scalpr_live.hedger import Hedger
from scalpr_live.orders import MakerOrders, Order, Place, Tolerance
from scalpr_live.settings import Settings
from scalpr_live.specs import Spec, steps

SNAPSHOT = 10_000_000_000  # ns, journal snapshots every 10 s
SECOND = 1_000_000_000


# BUDGET: requests per second to one exchange, a token bucket holding a second's worth; the last `reserve` share is
# only for urgent requests (cancels of orders that became a risk), so new orders never use it up
class Budget:
    def __init__(self, rate: float, reserve: float = 0.3):
        self.rate, self.reserve = rate, rate * reserve
        self.tokens, self.last = rate, None

    def take(self, now: int, urgent: bool) -> bool:
        if self.rate <= 0:
            return True
        if self.last is not None:
            self.tokens = min(self.rate, self.tokens + (now - self.last) / SECOND * self.rate)
        self.last = now
        if self.tokens < (1 if urgent else 1 + self.reserve):
            return False
        self.tokens -= 1
        return True


# what the trader asks of the exchanges (SimExchange for a dry run)
class Exchange(Protocol):
    def place(self, order: Order, now: int) -> None: ...
    def cancel(self, order: Order, now: int) -> None: ...
    def hedge(self, ticker: str, coins: float, limit: float, now: int) -> None: ...
    def tick(self, now: int) -> None: ...
    def on_trade(self, trade: TradeEvent) -> None: ...


@dataclass
class LiveCoin:
    coin: Coin
    spec: Spec
    orders: MakerOrders
    hedger: Hedger
    account: Account = field(default_factory=Account)
    status: str = "starting"
    fills: dict[bool, int] = field(default_factory=lambda: {True: 0, False: 0})
    volume: float = 0.0  # USDT of maker fills
    hedges: int = 0
    overdue_since: int | None = None  # since when a hedgeable amount is owed, ns


# LIVE TRADER: the strategy with real-sized orders. The paper engine runs alongside on the same events: its books,
# basis and volatility price the quotes (Engine.decide with this trader's own position) and its paper fills are the
# twin to compare with. Quotes become htx contracts within the position limit counting fills not yet hedged; maker
# fills are hedged by the Hedger; quoting halts for good on a hedge or an answer that doesn't come in time or on
# the loss limit (hedging goes on), and pauses while a feed is quieter than `settings.stale`.
class LiveTrader:
    def __init__(self, config: Config, settings: Settings, engine: Engine, specs: dict[str, Spec], journal: Journal | None = None):
        self.config, self.settings, self.engine, self.journal = config, settings, engine, journal
        self.maker, self.hedge = config.maker, config.hedge
        # client order ids: unique across runs, htx takes integers
        ids = count(time.time_ns() // 1_000_000 * 1000)
        tolerance = Tolerance(settings.keep_inside, settings.keep_outside, settings.keep_size)
        self.coins = {coin.ticker: LiveCoin(coin, specs[coin.ticker], MakerOrders(coin.ticker, specs[coin.ticker].tick, ids, tolerance),
                                            Hedger(specs[coin.ticker]))
                      for coin in config.coins}
        self.exchange: Exchange | None = None
        self.halted: str | None = None
        self.now = self.next_snapshot = self.turn = 0
        self.budget = Budget(settings.max_requests)  # of the maker exchange, shared by all coins
        self.deferred = 0  # times the budget said wait (the request is tried again with the next step)
        self.requests: Counter[str] = Counter()  # sent to each exchange
        self.per_second: Counter[tuple[str, int]] = Counter()
        self.peak: Counter[str] = Counter()  # most requests to an exchange within one second

    def on_event(self, event: Event) -> None:
        self.engine.on_event(event)
        if isinstance(event, TradeEvent) and event.exchange == self.maker and not event.rpi:
            self.exchange.on_trade(event)
        self.step(event.received)

    # all time-driven work, with every event and from the live loop's heartbeat
    def step(self, now: int) -> None:
        now = self.now = max(now, self.now)
        self.engine.tick(now)
        self.exchange.tick(now)
        # a halt takes the quotes off in this same step
        self.check(now)
        # coins take turns at the front, so a short request budget isn't always spent on the first one
        coins = list(self.coins.values())
        self.turn = (self.turn + 1) % len(coins)
        for live in coins[self.turn:] + coins[:self.turn]:
            self.requote(live, now)
            self.send_hedge(live, now)
        if now >= self.next_snapshot:
            self.next_snapshot = (now // SNAPSHOT + 1) * SNAPSHOT
            for live in self.coins.values():
                self.write("snapshots", self.snapshot(live, now))

    def requote(self, live: LiveCoin, now: int) -> None:
        want: dict[bool, tuple[float, int] | None] = {True: None, False: None}
        quiet = [exchange for exchange, seen in self.engine.seen.items() if now - seen > self.settings.stale]
        if self.halted:
            live.status = "halted"
        elif quiet:
            live.status = "stale"
        else:
            decision = self.engine.decide(self.engine.coins[live.coin.ticker], now, live.account.maker)
            live.status = decision.status
            for bid, price in ((True, decision.bid), (False, decision.ask)):
                if price is not None:
                    contracts = self.contracts(live, bid, float(price))
                    want[bid] = (float(price), contracts) if contracts > 0 else None
        for action in live.orders.reconcile(now, want, lambda urgent: self.allow(now, urgent)):
            self.request(self.maker, now)
            if isinstance(action, Place):
                self.exchange.place(action.order, now)
            else:
                self.exchange.cancel(action.order, now)
            self.write("orders", {"t": now, "event": "place" if isinstance(action, Place) else "cancel", **_order(action.order)})

    def allow(self, now: int, urgent: bool) -> bool:
        if self.budget.take(now, urgent):
            return True
        self.deferred += 1
        return False

    # the quote's contracts: the notional, cut so that a full fill keeps the maker position within max_position
    # (the position already holds the fills still waiting for their hedge)
    def contracts(self, live: LiveCoin, bid: bool, price: float) -> int:
        position = live.account.maker if bid else -live.account.maker
        room = live.coin.max_position / price - position
        return max(0, min(live.spec.contracts(live.coin.notional, price), steps(room, live.spec.contract)))

    def send_hedge(self, live: LiveCoin, now: int) -> None:
        book = self.engine.coins[live.coin.ticker].books[self.hedge]
        if book is None:
            return
        best_bid, best_ask = book.best_bid(), book.best_ask()
        if math.isnan(best_bid) or math.isnan(best_ask):
            return
        coins = live.hedger.order(_mid(book))
        if not coins:
            return
        slip = self.settings.hedge_slippage / 100
        limit = best_ask * (1 + slip) if coins > 0 else best_bid * (1 - slip)
        self.request(self.hedge, now)
        self.exchange.hedge(live.coin.ticker, coins, limit, now)
        self.write("hedges", {"t": now, "event": "send", "ticker": live.coin.ticker, "coins": coins, "limit": limit})

    # HALTS: quoting stops for the rest of the run (hedges still go out); pauses (stale feeds) are in requote
    def check(self, now: int) -> None:
        if self.halted:
            return
        reason = self.halt_reason(now)
        if reason is not None:
            self.halt(now, reason)

    def halt_reason(self, now: int) -> str | None:
        for live in self.coins.values():
            book = self.engine.coins[live.coin.ticker].books[self.hedge]
            mid = _mid(book) if book is not None else math.nan
            if not math.isnan(mid) and live.hedger.overdue(mid):
                live.overdue_since = live.overdue_since or now
                if now - live.overdue_since > self.settings.hedge_timeout:
                    return f"{live.coin.ticker}: {live.hedger.owed:+g} coins unhedged for {(now - live.overdue_since) / 1e9:.1f} s"
            else:
                live.overdue_since = None
            for order in live.orders.waiting(now, self.settings.answer_timeout):
                return f"{live.coin.ticker}: order {order.id} ({order.state}) unanswered for {(now - order.sent) / 1e9:.1f} s"
        pnl = self.pnl()
        if pnl < -self.settings.max_loss:
            return f"pnl {pnl:+.2f} below the loss limit -{self.settings.max_loss:g}"
        return None

    def halt(self, now: int, reason: str) -> None:
        self.halted = reason
        logger.warning(f"quoting halted: {reason}")
        self.write("halts", {"t": now, "reason": reason})

    # EXCHANGE ANSWERS (the Listener of sim.SimExchange)
    def answered(self, ticker: str, order_id: int, live: bool, now: int) -> None:
        order = self.coins[ticker].orders.answered(order_id, live)
        if order is not None:
            self.write("orders", {"t": now, "event": "live" if live else "rejected", **_order(order)})

    def canceled(self, ticker: str, order_id: int, now: int) -> None:
        order = self.coins[ticker].orders.canceled(order_id)
        if order is not None:
            self.write("orders", {"t": now, "event": "canceled", **_order(order)})

    def filled(self, ticker: str, order_id: int, contracts: int, price: float, now: int) -> None:
        live = self.coins[ticker]
        order = live.orders.filled(order_id, contracts)
        if order is None:
            logger.warning(f"{ticker}: fill of an unknown order {order_id}")
            return
        coins = contracts * live.spec.contract
        fee = live.account.trade(True, coins if order.bid else -coins, price, self.config.fees[0])
        live.hedger.on_fill(order.bid, coins)
        live.fills[order.bid] += 1
        live.volume += coins * price
        self.write("fills", {"t": now, "ticker": ticker, "id": order_id, "buys": order.bid, "price": price, "contracts": contracts,
                             "coins": coins, "fee": fee, "fair": self.engine.coins[ticker].fair})

    def hedged(self, ticker: str, sent: float, filled: float, price: float, now: int) -> None:
        live = self.coins[ticker]
        live.hedger.answered(sent, filled)
        fee = live.account.trade(False, filled, price, self.config.fees[1]) if filled else 0.0
        live.hedges += 1
        self.write("hedges", {"t": now, "event": "filled", "ticker": ticker, "coins": sent, "filled": filled, "price": price, "fee": fee})

    # marked pnl of a coin and of the run
    def coin_pnl(self, live: LiveCoin) -> float:
        books = self.engine.coins[live.coin.ticker].books
        if books[self.maker] is None or books[self.hedge] is None:
            return live.account.cash
        return live.account.marked(_mid(books[self.maker]), _mid(books[self.hedge]))

    def pnl(self) -> float:
        return sum(self.coin_pnl(live) for live in self.coins.values())

    def request(self, exchange: str, now: int) -> None:
        self.requests[exchange] += 1
        second = (exchange, now // 1_000_000_000)
        self.per_second[second] += 1
        self.peak[exchange] = max(self.peak[exchange], self.per_second[second])
        if len(self.per_second) > 1000:
            self.per_second = Counter({key: value for key, value in self.per_second.items() if key[1] >= second[1] - 1})

    def snapshot(self, live: LiveCoin, now: int) -> dict:
        paper = self.engine.coins[live.coin.ticker]
        return {
            "t": now, "ticker": live.coin.ticker, "status": live.status, "halted": self.halted,
            "bid": _side(live.orders.sides[True]), "ask": _side(live.orders.sides[False]),
            "maker": live.account.maker, "hedge": live.account.hedge, "owed": live.hedger.owed, "cash": live.account.cash,
            "fees": live.account.fees, "pnl": self.coin_pnl(live), "fills": sum(live.fills.values()),
            "paper_inventory": paper.ledger.inventory, "paper_pnl": self.engine.pnl(paper),
            "requests": dict(self.requests), "peak": dict(self.peak), "deferred": self.deferred,
        }

    def write(self, name: str, record: dict) -> None:
        if self.journal is not None:
            self.journal.write(name, record)


def _order(order: Order) -> dict:
    return {"ticker": order.ticker, "id": order.id, "bid": order.bid, "price": order.price, "volume": order.volume, "filled": order.filled}


def _side(order: Order | None) -> list | None:
    return None if order is None else [order.price, order.left, order.state]


def _mid(book) -> float:
    return (book.best_bid() + book.best_ask()) / 2
