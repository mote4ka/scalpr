import math
from dataclasses import replace
from itertools import pairwise

import numpy as np
import pytest
from scalpr_collector.orderbook import OrderBook
from scalpr_collector.realtime.events import BookEvent, TradeEvent
from scalpr_trader.config import Coin, Config, duration, from_dict, load
from scalpr_trader.engine import Engine

TICKER, TICK = "TESTUSDT", 0.01
T0 = 1_700_000_000 * 1_000_000_000  # ns
MS = 1_000_000
SECOND = 1_000_000_000


def config(**changes) -> Config:
    coin = Coin(TICKER, edge=0.2, notional=100.0, max_position=300.0)
    base = Config((coin,), quote_latency=duration("100ms"), hedge_latency=duration("20ms"), warmup=duration("1s"))
    return replace(base, **changes)


def book(bid: float, ask: float, size: float = 1000.0) -> OrderBook:
    result = OrderBook(50)
    result.apply(True, [[bid, size]], [[ask, size]])
    return result


def book_event(exchange: str, at: int, bid: float, ask: float) -> BookEvent:
    return BookEvent(exchange, TICKER, at // MS, at, book(bid, ask))


def trade(at: int, price: float, size: float, side: str) -> TradeEvent:
    return TradeEvent("htx", TICKER, at // MS, at, price, size, side, False)


# books in place and the basis warm (0 %): quotes go out at T0 + 1 s and are live 100 ms later
def warm_engine(cfg: Config, htx: tuple[float, float] = (99.5, 100.5), bybit: tuple[float, float] = (99.99, 100.01)) -> Engine:
    engine = Engine(cfg, {TICKER: TICK})
    engine.on_event(book_event("bybit", T0, *bybit))
    engine.on_event(book_event("htx", T0 + MS, *htx))
    engine.tick(T0 + SECOND)
    return engine


def test_quotes_after_warmup_edge_away_from_fair():
    engine = warm_engine(config())
    state = engine.coins[TICKER]
    assert state.status == "quoting"
    assert state.fair == pytest.approx(100.0)
    assert state.sent[True] == pytest.approx(99.8)
    assert state.sent[False] == pytest.approx(100.2)


def test_a_quote_is_not_live_before_the_quote_latency():
    engine = warm_engine(config())
    engine.on_event(trade(T0 + SECOND + 50 * MS, 99.5, 0.5, "sell"))
    assert engine.coins[TICKER].fills[True] == 0


def test_a_fill_is_hedged_after_the_hedge_latency():
    engine = warm_engine(config())
    state = engine.coins[TICKER]
    engine.on_event(trade(T0 + SECOND + 200 * MS, 99.5, 0.5, "sell"))
    assert state.fills[True] == 1
    assert state.ledger.inventory == 0  # not hedged yet
    engine.tick(T0 + SECOND + 230 * MS)
    assert state.ledger.inventory == pytest.approx(0.5)
    fee = (99.8 * 0.02 + 99.99 * 0.055) * 0.5 / 100
    assert state.ledger.cash == pytest.approx((99.99 - 99.8) * 0.5 - fee)


def test_a_side_stops_quoting_at_the_position_limit():
    engine = warm_engine(config())
    state = engine.coins[TICKER]
    state.ledger.inventory = 3.0  # 300 USDT long
    engine.tick(T0 + 2 * SECOND)
    assert state.sent[True] is None
    assert state.sent[False] == pytest.approx(100.2)
    assert state.status == "one side"


def test_quotes_are_pulled_when_a_feed_goes_quiet():
    engine = warm_engine(config())
    engine.tick(T0 + 12 * SECOND)
    state = engine.coins[TICKER]
    assert state.status == "stale"
    assert state.sent[True] is None and state.sent[False] is None


def test_book_anchor_quotes_one_tick_inside_a_wide_book():
    engine = warm_engine(config(anchor="book"))
    state = engine.coins[TICKER]
    assert state.sent[True] == pytest.approx(99.51)
    assert state.sent[False] == pytest.approx(100.49)
    # a print at the old book best still reaches the bid, at a better price for us
    engine.on_event(trade(T0 + SECOND + 200 * MS, 99.5, 0.5, "sell"))
    assert state.fills[True] == 1
    assert state.hedges[-1].price == pytest.approx(99.51)


def test_skew_leans_the_quotes_against_the_inventory():
    coin = Coin(TICKER, edge=0.2, notional=100.0, max_position=300.0, skew=0.1)
    engine = warm_engine(config(coins=(coin,)))
    state = engine.coins[TICKER]
    state.ledger.inventory = 1.5  # 150 USDT long: half the limit, -0.05 %
    engine.tick(T0 + 2 * SECOND)
    assert state.sent[True] == pytest.approx(99.75)
    assert state.sent[False] == pytest.approx(100.15)


def test_vol_edge_widens_the_quotes_when_the_hedge_mid_moves():
    coin = Coin(TICKER, edge=0.2, notional=100.0, max_position=300.0, vol_edge=2.0)
    engine = Engine(config(coins=(coin,), vol_window=duration("10s"), basis_window=duration("10min")), {TICKER: TICK})
    engine.on_event(book_event("htx", T0, 99.5, 100.5))
    mids = []
    for second in range(10):
        mid = 100.0 + (0.1 if second % 2 else -0.1)
        mids.append(mid)
        engine.on_event(book_event("bybit", T0 + second * SECOND, mid - 0.01, mid + 0.01))
        engine.on_event(book_event("htx", T0 + second * SECOND + MS, 99.5, 100.5))
    state = engine.coins[TICKER]
    # a second's sample is taken before the event that crosses into it is applied: the last mid is not in yet
    sampled = mids[:-1]
    returns = [math.log(b / a) * 100 for a, b in pairwise(sampled)]
    vol = float(np.std(returns, ddof=1))
    assert state.vol.value == pytest.approx(vol)
    assert state.edge == pytest.approx(0.2 + 2.0 * vol)
    # the bid sits the wider edge below fair, give or take the tick it is rounded to
    assert (state.fair - state.sent[True]) / state.fair * 100 >= state.edge - TICK / state.fair * 100


def test_config_reads_the_new_settings(tmp_path):
    path = tmp_path / "trader.toml"
    path.write_text('[trader]\nanchor = "book"\nvol_window = "2min"\nvol_edge = 1.5\nskew = 0.1\n\n'
                    '[coins.DOTUSDT]\nedge = 0.25\n\n[coins.AVAXUSDT]\nedge = 0.2\nskew = 0\n', encoding="utf-8")
    loaded = load(path)
    assert loaded.anchor == "book"
    assert loaded.vol_window == duration("2min")
    dot, avax = loaded.coins
    assert (dot.vol_edge, dot.skew) == (1.5, 0.1)
    assert (avax.vol_edge, avax.skew) == (1.5, 0.0)
    path.write_text('[trader]\nanchor = "mid"\n\n[coins.DOTUSDT]\nedge = 0.25\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load(path)


def test_config_of_an_older_journal_still_loads():
    raw = {"coins": [{"ticker": TICKER, "edge": 0.2, "notional": 100.0, "max_position": 300.0}], "maker": "htx",
           "quote_latency": 50 * MS, "hedge_latency": 50 * MS, "fill": "through", "basis_window": 3600 * SECOND,
           "warmup": 1800 * SECOND, "stale": 10 * SECOND, "markout": 60 * SECOND}
    loaded = from_dict(raw)
    assert loaded.coins[0].edge == 0.2
    assert loaded.quote_latency == 50 * MS


def test_duration():
    assert duration("50ms") == 50 * MS
    assert duration("1.5s") == 1500 * MS
    assert duration("30min") == 1800 * SECOND
    with pytest.raises(ValueError):
        duration("5 minutes")
    assert not math.isnan(duration("1h"))
