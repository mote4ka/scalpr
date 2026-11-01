import asyncio
import base64
import hashlib
import hmac
import math

import pytest
from scalpr_live.bybit import BybitError
from scalpr_live.htx import HtxError, canonical
from scalpr_live.orders import Order
from scalpr_live.private import (
    BybitPrivate,
    HtxPrivate,
    OrderUpdate,
    bybit_updates,
    htx_update,
)
from scalpr_live.real import (
    NOT_FOUND,
    RealExchange,
    cancel_body,
    hedge_body,
    order_body,
)
from scalpr_live.specs import Spec

DOT = Spec("DOTUSDT", tick=0.0001, contract=1.0, qty_step=0.1, min_qty=0.1, min_notional=5.0, hedge_tick=0.0001)
NOW = 1_700_000_000_000_000_000


def test_prices_and_sizes_go_out_on_the_grids():
    assert DOT.contract_code == "DOT-USDT"
    assert DOT.price_text(0.94740000000001) == "0.9474"
    assert DOT.hedge_price_text(0.94736, buy=True) == "0.9473"  # a buy limit rounds down
    assert DOT.hedge_price_text(0.94731, buy=False) == "0.9474"  # a sell limit up
    assert DOT.qty_text(5.30000001) == "5.3"
    assert Spec("X", tick=1e-05, contract=1, qty_step=1, min_qty=1, min_notional=5, hedge_tick=10.0).hedge_price_text(1234, True) == "1230"


def test_request_bodies():
    order = Order(1793536060016000, "DOTUSDT", True, 0.9474, 105, NOW)
    assert order_body(DOT, order) == {
        "contract_code": "DOT-USDT", "margin_mode": "cross", "position_side": "both", "side": "buy", "type": "post_only",
        "price": "0.9474", "volume": "105", "client_order_id": "1793536060016000"}
    assert cancel_body(DOT, order.id) == {"contract_code": "DOT-USDT", "client_order_id": "1793536060016000"}
    assert hedge_body(DOT, "x1-1", -5.3, 0.9443) == {
        "category": "linear", "symbol": "DOTUSDT", "side": "Sell", "orderType": "Limit", "qty": "5.3", "price": "0.9443",
        "timeInForce": "IOC", "orderLinkId": "x1-1", "positionIdx": 0}


def _htx(state: str, filled: str, turnover: str, volume: str = "10", cancel: str | None = "0") -> dict:
    data = {"contract_code": "DOT-USDT", "client_order_id": "42", "state": state, "volume": volume, "trade_volume": filled,
            "trade_turnover": turnover}
    if cancel is not None:
        data["cancel_volume"] = cancel
    return {"op": "notify", "topic": "orders", "contract_code": "DOT-USDT", "data": data}


def test_htx_order_pushes():
    new = htx_update(_htx("new", "0", "0"), NOW)
    assert (new.client_id, new.ticker, new.state, new.filled, new.final) == ("42", "DOTUSDT", "new", 0, False)
    # one match in several pushes: "filled" before the volume adds up is not final yet
    assert not htx_update(_htx("filled", "4", "3.8"), NOW).final
    assert htx_update(_htx("filled", "10", "9.5"), NOW).final
    canceled = htx_update(_htx("partially_canceled", "4", "3.8", cancel="6"), NOW)
    assert (canceled.state, canceled.final) == ("canceled", True)
    assert htx_update(_htx("canceled", "0", "0", cancel=None), NOW).final  # an older push without cancel_volume
    assert htx_update({"op": "notify", "topic": "positions", "data": {}}, NOW) is None


def test_bybit_order_pushes():
    message = {"topic": "order", "data": [
        {"category": "linear", "symbol": "DOTUSDT", "orderLinkId": "x1-1", "orderStatus": "PartiallyFilledCanceled", "cumExecQty": "4",
         "cumExecValue": "3.796", "avgPrice": "0.949"},
        {"category": "spot", "symbol": "DOTUSDT", "orderLinkId": "s", "orderStatus": "Filled", "cumExecQty": "1", "cumExecValue": "1"},
    ]}
    [update] = bybit_updates(message, NOW)
    assert (update.client_id, update.state, update.filled, update.value, update.final) == ("x1-1", "canceled", 4, 3.796, True)


def test_private_auth_signatures():
    htx = HtxPrivate("key", "secret").auth_message(now=1_700_000_000)
    params = {name: htx[name] for name in ("AccessKeyId", "SignatureMethod", "SignatureVersion", "Timestamp")}
    payload = f"GET\napi.hbdm.com\n/ws/v5/notification\n{canonical(params)}"
    assert htx["Timestamp"] == "2023-11-14T22:13:20"
    assert htx["Signature"] == base64.b64encode(hmac.new(b"secret", payload.encode(), hashlib.sha256).digest()).decode()
    assert (htx["op"], htx["type"]) == ("auth", "api")
    key, expires, signature = BybitPrivate("key", "secret").auth_message(now=1_700_000_000)["args"]
    assert key == "key" and expires == 1_700_000_010_000
    assert signature == hmac.new(b"secret", f"GET/realtime{expires}".encode(), hashlib.sha256).hexdigest()


class FakeRest:
    def __init__(self, fail: dict[str, Exception] | None = None):
        self.sent, self.fail = [], fail or {}

    async def post(self, path: str, body: dict) -> dict:
        self.sent.append((path, body))
        if path in self.fail:
            raise self.fail[path]
        return {}


class Recorder:
    def __init__(self):
        self.calls = []

    def answered(self, ticker, order_id, live, now):
        self.calls.append(("answered", order_id, live))

    def canceled(self, ticker, order_id, now):
        self.calls.append(("canceled", order_id))

    def filled(self, ticker, order_id, contracts, price, bid, now):
        self.calls.append(("filled", order_id, contracts, pytest.approx(price), bid))

    def hedged(self, ticker, sent, filled, price, now):
        self.calls.append(("hedged", sent, filled, price if math.isnan(price) else pytest.approx(price)))


def exchange(htx_fail=None, bybit_fail=None) -> tuple[RealExchange, FakeRest, FakeRest, Recorder]:
    htx, bybit, recorder = FakeRest(htx_fail), FakeRest(bybit_fail), Recorder()
    return RealExchange(htx, bybit, {"DOTUSDT": DOT}, recorder), htx, bybit, recorder


async def settle(real: RealExchange) -> None:
    while real.tasks:
        await asyncio.gather(*list(real.tasks))


def update(state: str, filled: float, value: float, final: bool, client_id: str = "42", exchange_name: str = "htx") -> OrderUpdate:
    return OrderUpdate(exchange_name, client_id, "DOTUSDT", state, filled, value, final, NOW)


def test_an_order_placed_filled_in_two_parts_and_done():
    async def scenario():
        real, htx, _, recorder = exchange()
        real.place(Order(42, "DOTUSDT", True, 0.95, 10, NOW), NOW)
        await settle(real)
        assert htx.sent[0][0] == "/v5/trade/order"
        real.on_update(update("new", 0, 0, False))
        real.on_update(update("partially_filled", 3, 3 * 0.95, False))
        real.on_update(update("filled", 10, 3 * 0.95 + 7 * 0.951, True))
        real.on_update(update("filled", 10, 3 * 0.95 + 7 * 0.951, True))  # a repeated push changes nothing
        return real, recorder.calls
    real, calls = asyncio.run(scenario())
    assert calls == [("answered", 42, True), ("answered", 42, True), ("filled", 42, 3, pytest.approx(0.95), True),
                     ("answered", 42, True), ("filled", 42, 7, pytest.approx(0.951), True)]
    assert real.makers == {}


def test_a_refused_order_is_rejected_a_lost_answer_is_left_to_the_feed():
    async def scenario():
        refused, _, _, refused_calls = exchange(htx_fail={"/v5/trade/order": HtxError("/v5/trade/order", 1047, "insufficient margin")})
        refused.place(Order(42, "DOTUSDT", True, 0.95, 10, NOW), NOW)
        await settle(refused)
        lost, _, _, lost_calls = exchange(htx_fail={"/v5/trade/order": TimeoutError()})
        lost.place(Order(43, "DOTUSDT", True, 0.95, 10, NOW), NOW)
        await settle(lost)
        return refused, refused_calls.calls, lost, lost_calls.calls
    refused, refused_calls, lost, lost_calls = asyncio.run(scenario())
    assert refused_calls == [("answered", 42, False)] and refused.makers == {}
    assert lost_calls == [] and "43" in lost.makers


def test_a_cancel_is_confirmed_by_the_feed_and_not_found_is_no_error():
    async def scenario():
        real, htx, _, recorder = exchange(htx_fail={"/v5/trade/cancel_order": HtxError("/v5/trade/cancel_order", NOT_FOUND, "no order")})
        order = Order(42, "DOTUSDT", False, 0.96, 10, NOW)
        real.place(order, NOW)
        await settle(real)
        real.cancel(order, NOW)
        await settle(real)
        tries = sum(path == "/v5/trade/cancel_order" for path, _ in htx.sent)
        real.on_update(update("canceled", 0, 0, True))
        return tries, recorder.calls
    tries, calls = asyncio.run(scenario())
    assert tries == 1  # not found: no second try, the feed tells what happened
    assert calls[-1] == ("canceled", 42)


def test_a_hedge_is_answered_by_its_final_push_or_at_once_when_refused():
    async def scenario():
        real, _, bybit, recorder = exchange()
        real.hedge("DOTUSDT", -5.3, 0.9443, NOW)
        await settle(real)
        [(path, body)] = bybit.sent
        link = body["orderLinkId"]
        real.on_update(update("partially_filled", 4, 3.796, False, link, "bybit"))
        real.on_update(update("canceled", 4, 3.796, True, link, "bybit"))
        refused, _, _, refused_recorder = exchange(bybit_fail={"/v5/order/create": BybitError("/v5/order/create", 110007, "no balance")})
        refused.hedge("DOTUSDT", 5.3, 0.96, NOW)
        await settle(refused)
        return path, recorder.calls, refused_recorder.calls, refused.hedges
    path, calls, refused_calls, refused_hedges = asyncio.run(scenario())
    assert path == "/v5/order/create"
    assert calls == [("hedged", -5.3, -4.0, pytest.approx(0.949))]
    assert refused_calls[0][:3] == ("hedged", 5.3, 0.0) and math.isnan(refused_calls[0][3]) and refused_hedges == {}


def test_trouble_when_a_private_feed_is_down_or_cancel_after_keeps_failing():
    async def scenario():
        real, _, _, _ = exchange(htx_fail={"/v5/trade/cancel-after": HtxError("/v5/trade/cancel-after", 1, "down")})
        feed = HtxPrivate("key", "secret")
        real.private = [feed]
        down = real.trouble()
        feed.ready = True
        up = real.trouble()
        for second in range(3):
            real.tick(NOW + second * 2_000_000_000)
            await settle(real)
        return down, up, real.trouble()
    down, up, failing = asyncio.run(scenario())
    assert "htx private feed down" in down and up is None
    assert "cancel-after failed 3 times" in failing
