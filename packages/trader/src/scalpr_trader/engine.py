import math
from collections import deque
from dataclasses import asdict, dataclass
from itertools import count

from scalpr_collector.orderbook import OrderBook
from scalpr_collector.realtime.events import (
    BookEvent,
    Event,
    HeartbeatEvent,
    TradeEvent,
)
from scalpr_strategy import FEES, Ledger, fair_price, markout, quote_prices

from scalpr_trader.basis import SAMPLE, Basis
from scalpr_trader.config import Coin, Config
from scalpr_trader.journal import Journal
from scalpr_trader.paper import PaperQuotes

SNAPSHOT = 10 * SAMPLE  # journal snapshots every 10 s


@dataclass
class Fill:
    id: int
    ticker: str
    ts: int  # local time the trade that filled us arrived, ns
    buys: bool  # we bought on the maker exchange
    price: float
    qty: float  # coins
    fair: float
    ahead: bool  # our quote was better than the book: the taker met us first
    due: int  # when the hedge order fills, ns
    hedge: float = math.nan
    fee: float = math.nan  # USDT, both legs
    markout: float = math.nan  # % some time later (see Config.markout)


# everything the engine keeps for one coin
class CoinState:
    def __init__(self, coin: Coin, tick: float, config: Config):
        self.coin, self.tick = coin, tick
        self.quotes = PaperQuotes(config.quote_latency)
        self.ledger = Ledger()
        self.basis = Basis(config.basis_window, config.warmup)
        self.books: dict[str, OrderBook | None] = {"bybit": None, "htx": None}
        # maker book best prices by exchange time: the one right before a trade tells if our quote was the best
        self.maker_tops: deque[tuple[int, float, float]] = deque(maxlen=50)
        self.sent: dict[bool, float | None] = {True: None, False: None}  # last price sent per side, True: bid
        self.refill: dict[bool, bool] = {True: False, False: False}  # a side got filled: send it again at full size
        self.fair = math.nan
        self.status = "starting"
        self.hedges: deque[Fill] = deque()  # fills waiting for their hedge
        self.markouts: deque[Fill] = deque()  # hedged fills waiting to be marked again
        self.recent: deque[float] = deque(maxlen=100)  # last markouts, %
        self.fills = {True: 0, False: 0}
        self.volume = 0.0  # USDT on the maker exchange

    def maker_best_before(self, ts: int, bid: bool) -> float:
        for top_ts, best_bid, best_ask in reversed(self.maker_tops):
            if top_ts < ts:
                return best_bid if bid else best_ask
        return math.nan


# ENGINE: XEMM on live (or replayed) events, nothing is sent to the exchanges
# every update of the hedge or maker book re-prices the quotes (scalpr_strategy.quote_prices around the fair price
# from the basis); a maker exchange trade can fill a standing paper quote (PaperQuotes); the fill is hedged
# `hedge_latency` later at the hedge book of that moment and booked in the Ledger; time-driven work (basis samples,
# stale feeds, hedges, markouts, snapshots) runs in `tick`, called with every event and by the live loop
# a feed is stale when nothing (heartbeats included) came from its exchange for `stale`: a quiet book alone is not
class Engine:
    def __init__(self, config: Config, ticks: dict[str, float], journal: Journal | None = None):
        self.config, self.journal = config, journal
        self.maker, self.hedge = config.maker, config.hedge
        self.coins = {coin.ticker: CoinState(coin, ticks[coin.ticker], config) for coin in config.coins}
        self.now = self.next_sample = self.next_snapshot = 0
        self.ids = count(1)
        self.seen = {"bybit": 0, "htx": 0}  # local time of the last event of each exchange, ns

    def on_event(self, event: Event) -> None:
        self.seen[event.exchange] = max(self.seen[event.exchange], event.received)
        self.tick(event.received)
        if isinstance(event, HeartbeatEvent):
            return
        state = self.coins.get(event.ticker)
        if state is None:
            return
        if isinstance(event, BookEvent):
            state.books[event.exchange] = event.book
            if event.exchange == self.maker:
                state.maker_tops.append((event.ts, event.book.best_bid(), event.book.best_ask()))
            else:
                self.run_hedges(state, event.received)
            self.requote(state, event.received)
        elif isinstance(event, TradeEvent) and event.exchange == self.maker and not event.rpi:
            self.on_trade(state, event)

    def tick(self, now: int) -> None:
        # two feeds deliver from two tasks, a late one never moves time back
        now = self.now = max(now, self.now)
        for state in self.coins.values():
            self.run_hedges(state, now)
            self.run_markouts(state, now)
        if now >= self.next_sample:
            self.next_sample = (now // SAMPLE + 1) * SAMPLE
            for state in self.coins.values():
                deviation = self.deviation(state, now)
                if not math.isnan(deviation):
                    state.basis.add(now, deviation)
                # a silent feed pulls the quotes even when no event comes for that coin
                self.requote(state, now)
        if now >= self.next_snapshot:
            self.next_snapshot = (now // SNAPSHOT + 1) * SNAPSHOT
            if self.journal is not None:
                for state in self.coins.values():
                    self.journal.write("snapshots", self.snapshot(state, now))

    # htx vs bybit deviation of the mids, %, NaN without fresh books on both sides
    def deviation(self, state: CoinState, now: int) -> float:
        if self.stale(state, now):
            return math.nan
        htx, bybit = state.books["htx"], state.books["bybit"]
        return (_mid(htx) / _mid(bybit) - 1) * 100

    def stale(self, state: CoinState, now: int) -> bool:
        return any(book is None or now - self.seen[exchange] > self.config.stale for exchange, book in state.books.items())

    def requote(self, state: CoinState, now: int) -> None:
        coin = state.coin
        want = {True: None, False: None}
        if self.stale(state, now):
            state.status = "stale"
        elif state.basis.value is None:
            state.status = "warmup"
        else:
            maker_book = state.books[self.maker]
            state.fair = fair_price(_mid(state.books[self.hedge]), state.basis.value, self.maker)
            bid, ask = quote_prices(state.fair, coin.edge, state.tick, maker_book.best_bid(), maker_book.best_ask())
            exposure = state.ledger.inventory * state.fair
            want = {True: bid if exposure < coin.max_position else None, False: ask if -exposure < coin.max_position else None}
            # an empty side of the maker book leaves nothing to price against
            for side, price in want.items():
                if price is not None and math.isnan(price):
                    want[side] = None
            state.status = "quoting" if None not in want.values() else "one side" if any(want.values()) else "no quotes"
        for side, price in want.items():
            if price != state.sent[side] or (price is not None and state.refill[side]):
                state.quotes.send(now, side, price, coin.notional / price if price is not None else 0.0)
                state.sent[side], state.refill[side] = price, False

    def on_trade(self, state: CoinState, trade: TradeEvent) -> None:
        sells = trade.side == "sell"  # a taker sell can hit our bid
        best = state.maker_best_before(trade.ts, sells)
        if math.isnan(best):
            return
        result = state.quotes.match(trade.received, sells, trade.price, trade.size, best, self.config.fill, state.tick)
        if result is None:
            return
        price, qty = result
        sign = 1 if sells else -1
        state.hedges.append(Fill(next(self.ids), state.coin.ticker, trade.received, sells, float(price), qty, float(state.fair),
                                 bool(sign * (price - best) > state.tick / 2), trade.received + self.config.hedge_latency))
        state.refill[sells] = True
        state.fills[sells] += 1
        state.volume += price * qty

    # due hedges fill at the hedge book of now: VWAP for the size, the best price when the book is too thin
    def run_hedges(self, state: CoinState, now: int) -> None:
        book = state.books[self.hedge]
        while state.hedges and state.hedges[0].due <= now and book is not None:
            fill = state.hedges.popleft()
            # we bought on the maker exchange, so the hedge sells
            fill.hedge = _vwap(book, fill.buys, fill.qty)
            if math.isnan(fill.hedge):
                fill.hedge = book.best_bid() if fill.buys else book.best_ask()
            fill.fee = state.ledger.fill(fill.buys, fill.price, fill.qty, fill.hedge, FEES[self.maker]["maker"], FEES[self.hedge]["taker"])
            state.markouts.append(fill)
            if self.journal is not None:
                self.journal.write("fills", asdict(fill))

    def run_markouts(self, state: CoinState, now: int) -> None:
        while state.markouts and state.markouts[0].ts + self.config.markout <= now:
            fill = state.markouts.popleft()
            fill.markout = markout(1 if fill.buys else -1, fill.price, fill.hedge, self.deviation(state, now), self.maker)
            if not math.isnan(fill.markout):
                state.recent.append(fill.markout)
            if self.journal is not None:
                self.journal.write("markouts", {"id": fill.id, "ticker": fill.ticker, "markout": fill.markout})

    # pnl with the maker leg at its mid and the hedge at the other mid
    def pnl(self, state: CoinState) -> float:
        if state.books["htx"] is None or state.books["bybit"] is None:
            return state.ledger.cash
        return state.ledger.marked(_mid(state.books[self.maker]), _mid(state.books[self.hedge]))

    def snapshot(self, state: CoinState, now: int) -> dict:
        return {
            "t": now, "ticker": state.coin.ticker, "status": state.status, "basis": state.basis.value, "fair": state.fair,
            "bid": state.sent[True], "ask": state.sent[False], "inventory": state.ledger.inventory, "cash": state.ledger.cash,
            "fees": state.ledger.fees, "pnl": self.pnl(state),
        }


def _mid(book: OrderBook) -> float:
    return (book.best_bid() + book.best_ask()) / 2


# average price of taking `qty` coins from the book: selling hits the bids, buying lifts the asks; NaN if too thin
def _vwap(book: OrderBook, sell: bool, qty: float) -> float:
    side = book.bids if sell else book.asks
    left, cost = qty, 0.0
    for price in sorted(side, reverse=sell):
        take = min(left, side[price])
        cost += take * price
        left -= take
        if left <= 1e-12:
            return cost / qty
    return math.nan
