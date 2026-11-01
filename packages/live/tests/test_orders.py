from itertools import count

import pytest
from scalpr_live.account import Account
from scalpr_live.hedger import Hedger
from scalpr_live.orders import Cancel, MakerOrders, Place, Tolerance
from scalpr_live.specs import Spec
from scalpr_live.trader import SECOND, Budget

TICK = 0.0001
DOT = Spec("DOTUSDT", tick=TICK, contract=1.0, qty_step=0.1, min_qty=0.1, min_notional=5.0)


def orders() -> MakerOrders:
    return MakerOrders("DOTUSDT", TICK, count(1))


def test_specs_round_to_whole_steps():
    assert DOT.contracts(100, 0.95) == 105
    assert DOT.hedge_qty(0.3) == pytest.approx(0.3)  # 0.3 / 0.1 is 2.9999999999999996
    assert DOT.min_hedge(0.95) == pytest.approx(5.3)  # $5 at 0.95 is 5.27 coins: 53 steps
    assert DOT.steps_fit
    assert not Spec("X", TICK, contract=0.1, qty_step=1, min_qty=1, min_notional=5).steps_fit


def test_one_order_per_side_until_the_exchange_answers():
    book = orders()
    actions = book.reconcile(0, {True: (0.95, 105), False: (0.96, 104)})
    assert [type(action) for action in actions] == [Place, Place]
    bid = book.sides[True]
    # a new price while the place is unanswered: nothing goes out
    assert book.reconcile(1, {True: (0.951, 105), False: (0.96, 104)}) == []
    book.answered(bid.id, live=True)
    actions = book.reconcile(2, {True: (0.951, 105), False: (0.96, 104)})
    assert actions == [Cancel(bid)] and bid.state == "canceling"
    # the new bid waits for the cancel to be confirmed
    assert book.reconcile(3, {True: (0.951, 105), False: (0.96, 104)}) == []
    book.canceled(bid.id)
    [place] = book.reconcile(4, {True: (0.951, 105), False: (0.96, 104)})
    assert isinstance(place, Place) and place.order.price == 0.951 and place.order.id != bid.id


def test_same_price_in_float_noise_is_no_requote():
    book = orders()
    [place] = book.reconcile(0, {True: (0.95, 105), False: None})
    book.answered(place.order.id, live=True)
    assert book.reconcile(1, {True: (0.9500000000000001, 105), False: None}) == []


def test_a_partly_filled_order_is_replaced_at_full_size():
    book = orders()
    [place] = book.reconcile(0, {True: (0.95, 105), False: None})
    book.answered(place.order.id, live=True)
    book.filled(place.order.id, 5)
    assert book.reconcile(1, {True: (0.95, 105), False: None}) == [Cancel(place.order)]


def test_a_full_fill_frees_the_side_even_while_cancelling():
    book = orders()
    [place] = book.reconcile(0, {True: (0.95, 105), False: None})
    book.answered(place.order.id, live=True)
    book.reconcile(1, {True: None, False: None})
    book.filled(place.order.id, 105)
    assert book.sides[True] is None
    # the cancel's late answer for the filled order changes nothing
    assert book.canceled(place.order.id) is None


def test_a_rejected_order_frees_its_side():
    book = orders()
    [place] = book.reconcile(0, {True: (0.95, 105), False: None})
    book.answered(place.order.id, live=False)
    assert book.sides[True] is None


def test_tolerance_keeps_an_order_that_drifted_a_little():
    book = MakerOrders("DOTUSDT", TICK, count(1), Tolerance(inside=0.02, outside=0.05, size=0.2))
    [place] = book.reconcile(0, {True: (1.0, 100), False: None})
    bid = place.order
    book.answered(bid.id, live=True)
    # wanted 0.0004 higher: our bid is 0.04 % further from fair, within 0.05 %
    assert book.reconcile(1, {True: (1.0004, 100), False: None}) == []
    # wanted 0.0001 lower: ours is 0.01 % closer to fair, within 0.02 %
    assert book.reconcile(2, {True: (0.9999, 100), False: None}) == []
    # a partial fill of 10 % stays
    book.filled(bid.id, 10)
    assert book.reconcile(3, {True: (1.0, 100), False: None}) == []
    # 0.03 % closer to fair than wanted is urgent, 0.06 % further is drift: both replace it
    assert book.urgent(bid, (0.9997, 100)) and not book.drifted(bid, (0.9997, 100))
    assert book.drifted(bid, (1.0006, 100)) and not book.urgent(bid, (1.0006, 100))
    # bigger than wanted is urgent: the position limit shrank
    assert book.urgent(bid, (1.0, 80))


def test_a_drifted_order_waits_for_the_budget_an_urgent_one_does_not():
    book = MakerOrders("DOTUSDT", TICK, count(1), Tolerance(inside=0.02, outside=0.05))
    [place] = book.reconcile(0, {True: (1.0, 100), False: None})
    book.answered(place.order.id, live=True)
    asked = []

    def broke(urgent: bool) -> bool:
        asked.append(urgent)
        return urgent  # only urgent requests get through

    assert book.reconcile(1, {True: (1.001, 100), False: None}, broke) == []
    assert book.reconcile(2, {True: (0.999, 100), False: None}, broke) == [Cancel(place.order)]
    assert asked == [False, True]


def test_budget_holds_a_reserve_for_urgent_requests():
    budget = Budget(rate=10, reserve=0.3)
    taken = sum(budget.take(0, urgent=False) for _ in range(20))
    assert taken == 7  # 10 tokens, 3 kept back
    assert sum(budget.take(0, urgent=True) for _ in range(5)) == 3
    assert budget.take(SECOND // 10, urgent=True)  # 0.1 s later one more token came
    assert Budget(rate=0).take(0, urgent=False)


def test_unanswered_orders_are_reported_after_the_timeout():
    book = orders()
    book.reconcile(0, {True: (0.95, 105), False: None})
    assert book.waiting(5, timeout=10) == []
    assert book.waiting(11, timeout=10) == [book.sides[True]]


def test_hedger_waits_for_a_whole_bybit_order():
    hedger = Hedger(DOT)
    hedger.on_fill(bought=True, coins=3)  # $2.85 at 0.95: under the $5 minimum
    assert hedger.order(0.95) == 0
    assert not hedger.overdue(0.95)
    hedger.on_fill(bought=True, coins=3)  # 6 coins now
    assert hedger.overdue(0.95)
    assert hedger.order(0.95) == pytest.approx(-6)  # sell on bybit what was bought on htx
    assert hedger.order(0.95) == 0  # nothing more while that one is out
    hedger.answered(sent=-6, filled=-4)  # IOC: 2 coins didn't fill and are owed again
    assert hedger.owed == pytest.approx(-2)
    hedger.on_fill(bought=False, coins=2)  # a sell on htx evens it out
    assert hedger.owed == pytest.approx(0)


def test_account_keeps_both_legs_and_fees():
    account = Account()
    maker_fee = account.trade(True, 10, 1.00, 0.02)
    hedge_fee = account.trade(False, -10, 1.01, 0.1)
    assert account.net == 0
    assert maker_fee == pytest.approx(0.002) and hedge_fee == pytest.approx(0.0101)
    assert account.marked(1.0, 1.0) == pytest.approx(0.1 - 0.002 - 0.0101)
