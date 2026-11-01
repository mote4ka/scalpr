from dataclasses import replace

import pytest
from scalpr_collector.orderbook import OrderBook
from scalpr_collector.realtime.events import BookEvent, TradeEvent
from scalpr_live.settings import Settings, load_settings
from scalpr_live.sim import SimExchange, take
from scalpr_live.specs import Spec
from scalpr_live.trader import LiveTrader
from scalpr_trader.config import Coin, Config, duration
from scalpr_trader.engine import Engine

TICKER, TICK = "TESTUSDT", 0.01
SPEC = Spec(TICKER, tick=TICK, contract=0.1, qty_step=0.1, min_qty=0.1, min_notional=5.0)
T0 = 1_700_000_000 * 1_000_000_000  # ns
MS = 1_000_000
SECOND = 1_000_000_000


def config(**changes) -> Config:
    coin = Coin(TICKER, edge=0.2, notional=100.0, max_position=300.0)
    base = Config((coin,), quote_latency=duration("100ms"), hedge_latency=duration("20ms"), warmup=duration("1s"), hedge_fee=0.1)
    return replace(base, **changes)


def book(bid: float, ask: float, size: float = 1000.0) -> OrderBook:
    result = OrderBook(50)
    result.apply(True, [[bid, size]], [[ask, size]])
    return result


def book_event(exchange: str, at: int, bid: float, ask: float) -> BookEvent:
    return BookEvent(exchange, TICKER, at // MS, at, book(bid, ask))


def trade(at: int, price: float, size: float, side: str) -> TradeEvent:
    return TradeEvent("htx", TICKER, at // MS, at, price, size, side, False)


# books in place and the basis warm (0 %): orders go out at T0 + 1 s
def warm_trader(cfg: Config | None = None, exchange=None) -> LiveTrader:
    cfg = cfg or config()
    engine = Engine(cfg, {TICKER: TICK})
    trader = LiveTrader(cfg, Settings(), engine, {TICKER: SPEC})
    trader.exchange = exchange or SimExchange(engine, cfg, {TICKER: SPEC}, trader)
    trader.on_event(book_event("bybit", T0, 99.99, 100.01))
    trader.on_event(book_event("htx", T0 + MS, 99.5, 100.5))
    trader.step(T0 + SECOND)
    return trader


# records what the trader sends and never answers
class Silent:
    def __init__(self):
        self.sent = []

    def place(self, order, now):
        self.sent.append(("place", order.id))

    def cancel(self, order, now):
        self.sent.append(("cancel", order.id))

    def hedge(self, ticker, coins, limit, now):
        self.sent.append(("hedge", coins))

    def tick(self, now):
        pass

    def on_trade(self, trade):
        pass


def test_orders_are_real_contracts_edge_away_from_fair():
    trader = warm_trader()
    live = trader.coins[TICKER]
    bid, ask = live.orders.sides[True], live.orders.sides[False]
    assert (bid.price, bid.volume, bid.state) == (pytest.approx(99.8), 10, "new")  # $100 at 99.8 is 1.002 coins: 10 contracts
    assert (ask.price, ask.volume) == (pytest.approx(100.2), 9)  # $100 at 100.2 is 0.998 coins: 9 contracts
    assert trader.requests["htx"] == 2
    trader.step(T0 + SECOND + 100 * MS)
    assert bid.state == "live" and ask.state == "live"


def test_a_fill_is_hedged_and_the_side_quoted_again():
    trader = warm_trader()
    live = trader.coins[TICKER]
    bid = live.orders.sides[True]
    trader.on_event(trade(T0 + SECOND + 200 * MS, 99.5, 5.0, "sell"))  # through our 99.8 bid
    assert live.account.maker == pytest.approx(1.0) and live.hedger.owed == pytest.approx(-1.0)
    assert trader.requests["bybit"] == 1  # the hedge went out right away
    new_bid = live.orders.sides[True]
    assert new_bid is not None and new_bid.id != bid.id  # a full fill frees the side for a new order
    trader.step(T0 + SECOND + 230 * MS)
    assert live.account.hedge == pytest.approx(-1.0) and live.account.net == 0
    fees = 99.8 * 0.02 / 100 + 99.99 * 0.1 / 100
    assert live.account.cash == pytest.approx(99.99 - 99.8 - fees)
    assert live.fills[True] == 1 and live.hedges == 1


def test_size_shrinks_so_a_full_fill_stays_within_max_position():
    trader = warm_trader()
    live = trader.coins[TICKER]
    trader.step(T0 + SECOND + 100 * MS)  # orders live
    live.account.maker = 2.5  # $250 long: room for 0.5 coin more on the bid
    bid = live.orders.sides[True]
    trader.step(T0 + SECOND + 150 * MS)
    assert bid.state == "canceling"
    trader.step(T0 + SECOND + 260 * MS)  # cancel confirmed, the smaller bid goes out
    assert live.orders.sides[True].volume == 5


def test_a_quiet_feed_takes_the_quotes_off():
    trader = warm_trader()
    live = trader.coins[TICKER]
    trader.step(T0 + SECOND + 100 * MS)
    trader.step(T0 + 4 * SECOND)  # 4 s without an event, stale after 3
    assert live.status == "stale"
    assert all(order.state == "canceling" for order in live.orders.sides.values())


def test_an_unanswered_order_halts_quoting():
    silent = Silent()
    trader = warm_trader(exchange=silent)
    for second in range(2, 8):  # the feeds keep talking, the exchange doesn't
        trader.on_event(book_event("bybit", T0 + second * SECOND, 99.99, 100.01))
        trader.on_event(book_event("htx", T0 + second * SECOND + MS, 99.5, 100.5))
    assert trader.halted and "unanswered" in trader.halted
    assert trader.coins[TICKER].status == "halted"


def test_an_unhedged_fill_halts_quoting_but_hedging_goes_on():
    silent = Silent()
    trader = warm_trader(exchange=silent)
    live = trader.coins[TICKER]
    for order in live.orders.sides.values():
        trader.answered(TICKER, order.id, True, T0 + SECOND + 100 * MS)
    trader.filled(TICKER, live.orders.sides[True].id, 10, 99.8, T0 + SECOND + 200 * MS)
    trader.on_event(book_event("bybit", T0 + SECOND + 210 * MS, 99.99, 100.01))
    assert ("hedge", pytest.approx(-1.0)) in silent.sent
    for second in range(2, 9):
        trader.on_event(book_event("bybit", T0 + second * SECOND, 99.99, 100.01))
    assert trader.halted and "unhedged" in trader.halted


def test_ioc_take_stops_at_the_limit():
    levels = OrderBook(50)
    levels.apply(True, [[99.9, 1.0], [99.8, 1.0]], [[100.1, 1.0], [100.2, 1.0], [100.5, 5.0]])
    assert take(levels, 3.0, 100.25) == (pytest.approx(2.0), pytest.approx(100.15))
    assert take(levels, -1.5, 99.0) == (pytest.approx(-1.5), pytest.approx((99.9 + 0.5 * 99.8) / 1.5))
    assert take(levels, 1.0, 100.0)[0] == 0


def test_live_settings_from_the_config(tmp_path):
    path = tmp_path / "trader.toml"
    path.write_text('[trader]\nhedge_fee = 0.1\n[coins.DOTUSDT]\nedge = 0.25\n[live]\nstale = "2s"\nmax_loss = 5\n', encoding="utf-8")
    settings = load_settings(path)
    assert settings.stale == duration("2s") and settings.max_loss == 5 and settings.hedge_timeout == duration("5s")
    path.write_text('[coins.DOTUSDT]\nedge = 0.25\n[live]\nstael = "2s"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="stael"):
        load_settings(path)
