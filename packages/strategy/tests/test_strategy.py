import math

import numpy as np
import pytest
from scalpr_strategy import (
    FEES,
    Ledger,
    Volatility,
    fair_price,
    filled,
    inventory_shift,
    markout,
    quote_edge,
    quote_prices,
    round_trip_fees,
)

TICK = 0.01
SECOND = 1_000_000_000


def test_fair_price_shifts_the_hedge_mid_by_the_basis():
    assert fair_price(100.0, 0.5, "htx") == pytest.approx(100.5)
    # quoting on bybit undoes the same shift
    assert fair_price(100.5, 0.5, "bybit") == pytest.approx(100.0)
    assert np.allclose(fair_price(np.array([100.0, 200.0]), 0.0, "htx"), [100.0, 200.0])


def test_quotes_sit_edge_away_from_fair_on_the_tick_grid():
    bid, ask = quote_prices(100.0, 0.2, TICK, 99.5, 100.5)
    assert bid == pytest.approx(99.8)
    assert ask == pytest.approx(100.2)


def test_quotes_round_away_from_fair():
    bid, ask = quote_prices(100.003, 0.2, TICK, 99.0, 101.0)
    # 99.802994 floors to 99.80, 100.203006 ceils to 100.21
    assert bid == pytest.approx(99.80)
    assert ask == pytest.approx(100.21)


def test_quotes_never_cross_the_book():
    # fair far above the book: the bid would take the ask, it stops a tick below it
    bid, ask = quote_prices(110.0, 0.2, TICK, 99.5, 100.5)
    assert bid == pytest.approx(100.49)
    assert ask > 100.5
    bid, ask = quote_prices(90.0, 0.2, TICK, 99.5, 100.5)
    assert ask == pytest.approx(99.51)


def test_quotes_pass_nan_through():
    bid, ask = quote_prices(np.array([100.0, np.nan]), 0.2, TICK, np.array([99.5, 99.5]), np.array([100.5, 100.5]))
    assert bid[0] == pytest.approx(99.8)
    assert math.isnan(bid[1]) and math.isnan(ask[1])


def test_book_anchor_keeps_one_tick_ahead_of_a_wide_book():
    # fair puts us at 99.80 / 100.20 inside a 99.50 / 100.50 book: one tick better than it is enough
    bid, ask = quote_prices(100.0, 0.2, TICK, 99.5, 100.5, "book")
    assert bid == pytest.approx(99.51)
    assert ask == pytest.approx(100.49)


def test_book_anchor_never_quotes_closer_to_fair_than_edge():
    # a tight book: one tick better than it would be inside fair +- edge, the edge wins
    bid, ask = quote_prices(100.0, 0.2, TICK, 99.95, 100.05, "book")
    assert bid == pytest.approx(99.8)
    assert ask == pytest.approx(100.2)


def test_book_anchor_with_an_empty_side_falls_back_to_fair():
    bid, ask = quote_prices(100.0, 0.2, TICK, math.nan, 100.5, "book")
    assert bid == pytest.approx(99.8)
    # as with the fair anchor, the ask has no bid side to stay above: no ask
    assert math.isnan(ask)
    bid, ask = quote_prices(math.nan, 0.2, TICK, 99.5, 100.5, "book")
    assert math.isnan(bid) and math.isnan(ask)


def test_book_anchor_on_arrays_matches_numbers():
    fair, best_bid, best_ask = np.array([100.0, 100.0, np.nan]), np.array([99.5, 99.95, 99.5]), np.array([100.5, 100.05, 100.5])
    bids, asks = quote_prices(fair, 0.2, TICK, best_bid, best_ask, "book")
    for i in range(2):
        bid, ask = quote_prices(fair[i], 0.2, TICK, best_bid[i], best_ask[i], "book")
        assert bids[i] == pytest.approx(bid) and asks[i] == pytest.approx(ask)
    assert math.isnan(bids[2])


def test_shift_moves_both_quotes():
    bid, ask = quote_prices(100.0, 0.2, TICK, 99.0, 101.0, shift=-0.1)
    # center 99.9
    assert bid == pytest.approx(99.70)
    assert ask == pytest.approx(100.10)


def test_inventory_shift_leans_against_the_inventory_and_is_capped():
    assert inventory_shift(150.0, 300.0, 0.1) == pytest.approx(-0.05)
    assert inventory_shift(-300.0, 300.0, 0.1) == pytest.approx(0.1)
    assert inventory_shift(900.0, 300.0, 0.1) == pytest.approx(-0.1)
    assert inventory_shift(150.0, 300.0, 0.0) == 0.0


def test_quote_edge_adds_volatility():
    assert quote_edge(0.15, 0.0, 0.05) == 0.15
    assert quote_edge(0.15, 2.0, 0.05) == pytest.approx(0.25)
    assert quote_edge(0.15, 2.0, math.nan) == pytest.approx(0.15)
    assert np.allclose(quote_edge(0.15, 2.0, np.array([0.0, 0.05, np.nan])), [0.15, 0.25, 0.15])


def test_volatility_is_the_std_of_1s_returns():
    vol = Volatility(10 * SECOND)
    mids = [100.0, 100.1, 100.0, 100.2, 100.1, 100.3]
    for i, mid in enumerate(mids):
        vol.add(i * SECOND, mid)
    returns = np.diff(np.log(mids)) * 100
    assert vol.value == pytest.approx(np.std(returns, ddof=1))


def test_volatility_is_unknown_until_half_the_window_and_forgets_old_returns():
    vol = Volatility(10 * SECOND)
    for i in range(5):
        vol.add(i * SECOND, 100.0 + i)
    assert math.isnan(vol.value)  # 4 returns, 5 needed
    vol.add(5 * SECOND, 100.0)
    assert not math.isnan(vol.value)
    for i in range(6, 30):
        vol.add(i * SECOND, 100.0)
    assert vol.value == pytest.approx(0.0)  # the moves left the window


def test_volatility_skips_a_gap():
    vol = Volatility(60 * SECOND)
    vol.add(0, 100.0)
    vol.add(10 * SECOND, 110.0)  # 10 s later: not a 1 s return
    assert len(vol.returns) == 0


def test_round_trip_fees_count_both_legs_twice():
    assert round_trip_fees({"htx": "maker", "bybit": "taker"}) == pytest.approx(2 * (FEES["htx"]["maker"] + FEES["bybit"]["taker"]))


# our bid at 99.80 with the book best bid `best`
@pytest.mark.parametrize(("price", "size", "best", "rule", "expected"), [
    # alone ahead of the book: a print at or through our price fills up to its size
    (99.70, 0.3, 99.50, "through", 0.3),
    (99.80, 0.3, 99.50, "through", 0.3),
    (99.85, 0.3, 99.50, "through", 0.0),
    # level with the book: a print through our price ate the level, the whole order is filled
    (99.70, 0.3, 99.80, "through", 1.0),
    # a print at our price fills only with the touch rule, up to its size
    (99.80, 0.3, 99.80, "through", 0.0),
    (99.80, 0.3, 99.80, "touch", 0.3),
])
def test_filled(price, size, best, rule, expected):
    assert filled(price, size, 99.80, best, 1.0, True, rule, TICK) == pytest.approx(expected)


def test_filled_ask_side_mirrors_the_bid():
    # our ask 100.20 ahead of the book ask 100.50, a taker buy through it
    assert filled(100.30, 0.4, 100.20, 100.50, 1.0, False, "through", TICK) == pytest.approx(0.4)
    assert filled(100.10, 0.4, 100.20, 100.50, 1.0, False, "through", TICK) == 0.0


def test_markout_counts_the_locked_spread_and_the_deviation():
    # bought htx at 99.8, hedged at 100: +0.2004 % locked; htx 0.1 % above bybit later adds to the long htx leg
    assert markout(1, 99.8, 100.0, 0.1, "htx") == pytest.approx((100.0 / 99.8 - 1) * 100 + 0.1)
    assert markout(-1, 100.2, 100.0, 0.1, "htx") == pytest.approx(-((100.0 / 100.2 - 1) * 100 + 0.1))


def test_ledger_books_both_legs_and_fees():
    ledger = Ledger()
    fee = ledger.fill(True, 99.8, 2.0, 100.0, 0.02, 0.055)
    assert fee == pytest.approx((99.8 * 0.02 + 100.0 * 0.055) * 2.0 / 100)
    assert ledger.inventory == pytest.approx(2.0)
    assert ledger.cash == pytest.approx((100.0 - 99.8) * 2.0 - fee)
    # marked with htx 0.1 above bybit: the long htx leg gains it
    assert ledger.marked(100.1, 100.0) == pytest.approx(ledger.cash + 2.0 * 0.1)
