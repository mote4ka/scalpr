import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from itertools import count

import aiohttp
from scalpr_core.logger import logger

from scalpr_live.bybit import BybitError, BybitRest
from scalpr_live.htx import HtxError, HtxRest
from scalpr_live.orders import Order
from scalpr_live.private import OrderUpdate, PrivateFeed
from scalpr_live.sim import Listener
from scalpr_live.specs import Spec

CANCEL_AFTER = 5000  # ms: htx cancels all our futures orders unless told again within this (its minimum)
CANCEL_AFTER_EVERY = 1_500_000_000  # ns between refreshes, three tries before it runs out
NOT_FOUND = 1061  # htx: the order doesn't exist (filled or cancelled already)
NETWORK = (aiohttp.ClientError, TimeoutError)


# POST /v5/trade/order: a post-only quote in one-way cross mode; volume in contracts, our id as client_order_id
def order_body(spec: Spec, order: Order) -> dict:
    return {"contract_code": spec.contract_code, "margin_mode": "cross", "position_side": "both", "side": "buy" if order.bid else "sell",
            "type": "post_only", "price": spec.price_text(order.price), "volume": str(order.volume), "client_order_id": str(order.id)}


def cancel_body(spec: Spec, order_id: int) -> dict:
    return {"contract_code": spec.contract_code, "client_order_id": str(order_id)}


# POST /v5/order/create: the hedge, an IOC limit order `coins` (+ buy, - sell) no worse than `limit`
def hedge_body(spec: Spec, link: str, coins: float, limit: float) -> dict:
    buy = coins > 0
    return {"category": "linear", "symbol": spec.ticker, "side": "Buy" if buy else "Sell", "orderType": "Limit", "qty": spec.qty_text(abs(coins)),
            "price": spec.hedge_price_text(limit, buy), "timeInForce": "IOC", "orderLinkId": link, "positionIdx": 0}


# REAL EXCHANGE: the same calls as sim.SimExchange, sent to HTX and Bybit. Requests go out as tasks; what happened
# to an order comes from the private websockets (on_update): an htx order's cumulative trade volume and turnover
# give each fill and its price, its final state the cancel; a bybit hedge is answered by its final state.
# A request the exchange refused is answered at once (rejected, or a hedge that filled nothing); one whose answer
# was lost on the way (timeout) is left to the private feed, and if that never tells, the trader's timeouts halt it.
# `after` (the trader's step) runs after every update so a fill is hedged right away.
class RealExchange:
    def __init__(self, htx: HtxRest, bybit: BybitRest, specs: dict[str, Spec], listener: Listener,
                 after: Callable[[int], None] | None = None, write: Callable[[str, dict], None] | None = None):
        self.htx, self.bybit, self.specs, self.listener = htx, bybit, specs, listener
        self.after = after or (lambda now: None)
        self.write = write or (lambda name, record: None)
        self.makers: dict[str, Order] = {}  # our htx orders by client id, until their final state
        self.seen: dict[str, tuple[float, float]] = {}  # client id -> contracts and turnover already passed on
        self.hedges: dict[str, tuple[str, float]] = {}  # bybit orderLinkId -> ticker, signed coins
        self.links = (f"x{time.time_ns() // 1_000_000}-{n}" for n in count(1))
        self.tasks: set[asyncio.Task] = set()
        self.private: list[PrivateFeed] = []
        self.next_cancel_after = 0
        self.cancel_after_failures = 0
        self.problem: str | None = None

    # TRADER SIDE (trader.Exchange)
    def place(self, order: Order, now: int) -> None:
        self.makers[str(order.id)] = order
        self._spawn(self._place(order))

    def cancel(self, order: Order, now: int) -> None:
        self._spawn(self._cancel(order))

    def hedge(self, ticker: str, coins: float, limit: float, now: int) -> None:
        link = next(self.links)
        self.hedges[link] = (ticker, coins)
        self._spawn(self._hedge(link, ticker, coins, limit))

    def tick(self, now: int) -> None:
        if now >= self.next_cancel_after:
            self.next_cancel_after = now + CANCEL_AFTER_EVERY
            self._spawn(self._cancel_after())

    def on_trade(self, trade) -> None:
        pass

    def trouble(self) -> str | None:
        if self.problem:
            return self.problem
        return next((f"{feed.exchange} private feed down ({feed.error})" for feed in self.private if not feed.ready), None)

    # EXCHANGE SIDE
    def on_update(self, update: OrderUpdate) -> None:
        if update.exchange == "htx":
            self._maker_update(update)
        else:
            self._hedge_update(update)
        self.after(update.received)

    def _maker_update(self, update: OrderUpdate) -> None:
        order = self.makers.get(update.client_id)
        if order is None:
            return  # not ours or long done
        now = update.received
        done, turnover = self.seen.get(update.client_id, (0.0, 0.0))
        if update.filled > done + 1e-9:
            contract = self.specs[order.ticker].contract
            price = (update.value - turnover) / ((update.filled - done) * contract)
            self.seen[update.client_id] = (update.filled, update.value)
            self.listener.filled(order.ticker, order.id, round(update.filled - done), price, order.bid, now)
        if update.state in ("new", "partially_filled"):
            self.listener.answered(order.ticker, order.id, True, now)
        if update.final:
            if update.state == "canceled":
                self.listener.canceled(order.ticker, order.id, now)
            self.makers.pop(update.client_id, None)
            self.seen.pop(update.client_id, None)

    def _hedge_update(self, update: OrderUpdate) -> None:
        if not update.final or update.client_id not in self.hedges:
            return
        ticker, coins = self.hedges.pop(update.client_id)
        filled = math.copysign(update.filled, coins)
        price = update.value / update.filled if update.filled else math.nan
        self.listener.hedged(ticker, coins, filled, price, update.received)

    # REQUESTS
    async def _place(self, order: Order) -> None:
        try:
            await self.htx.post("/v5/trade/order", order_body(self.specs[order.ticker], order))
        except HtxError as error:
            self._error("place", order.ticker, error)
            self.makers.pop(str(order.id), None)
            self._answer(lambda now: self.listener.answered(order.ticker, order.id, False, now))
        except NETWORK as error:
            self._error("place (left to the private feed)", order.ticker, error)
        else:
            self._answer(lambda now: self.listener.answered(order.ticker, order.id, True, now))

    async def _cancel(self, order: Order) -> None:
        for attempt in range(2):
            try:
                await self.htx.post("/v5/trade/cancel_order", cancel_body(self.specs[order.ticker], order.id))
                return
            except HtxError as error:
                if error.code == NOT_FOUND:
                    return  # filled or cancelled already: the private feed tells which
                self._error(f"cancel, try {attempt + 1}", order.ticker, error)
            except NETWORK as error:
                self._error(f"cancel, try {attempt + 1}", order.ticker, error)
            await asyncio.sleep(0.2)

    async def _hedge(self, link: str, ticker: str, coins: float, limit: float) -> None:
        try:
            await self.bybit.post("/v5/order/create", hedge_body(self.specs[ticker], link, coins, limit))
        except BybitError as error:
            self._error("hedge", ticker, error)
            if self.hedges.pop(link, None) is not None:
                self._answer(lambda now: self.listener.hedged(ticker, coins, 0.0, math.nan, now))
        except NETWORK as error:
            self._error("hedge (left to the private feed)", ticker, error)

    async def _cancel_after(self) -> None:
        try:
            await self.htx.post("/v5/trade/cancel-after", {"on_off": "1", "time_out": str(CANCEL_AFTER)})
            self.cancel_after_failures = 0
        except (HtxError, *NETWORK) as error:
            self.cancel_after_failures += 1
            self._error("cancel-after", "", error)
            if self.cancel_after_failures >= 3:
                self.problem = f"htx cancel-after failed {self.cancel_after_failures} times in a row: {error}"

    # on the way out: requests still in flight are dropped and every maker order is cancelled; an order a dropped
    # request still placed is taken off by the cancel-after timer, which nobody refreshes any more
    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        try:
            await self.htx.post("/v5/trade/cancel_all_orders", {})
        except (HtxError, *NETWORK) as error:
            logger.error(f"htx cancel all on the way out failed: {error!r}, the cancel-after timer still takes the orders off")

    def _answer(self, call: Callable[[int], None]) -> None:
        now = time.time_ns()
        call(now)
        self.after(now)

    def _error(self, what: str, ticker: str, error: Exception) -> None:
        logger.warning(f"{ticker} {what}: {error!r}")
        self.write("errors", {"t": time.time_ns(), "what": what, "ticker": ticker, "error": repr(error)})

    def _spawn(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(work)
        self.tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.problem = f"request failed: {task.exception()!r}"
            logger.error(self.problem)


# START: the bot's subaccounts hold nothing but the bot's own leftovers, so every open order goes; positions are read
# per coin as (htx coins, bybit coins). A position in a coin the config doesn't trade is an error.
async def prepare(htx: HtxRest, bybit: BybitRest, specs: dict[str, Spec]) -> dict[str, tuple[float, float]]:
    try:
        await htx.post("/v5/trade/cancel_all_orders", {})
    except HtxError as error:  # nothing to cancel may come back as an error: what's left is checked below
        logger.info(f"htx cancel all: {error}")
    await bybit.post("/v5/order/cancel-all", {"category": "linear", "settleCoin": "USDT"})
    left = len(await htx.get("/v5/trade/order/opens") or []) + len((await bybit.get("/v5/order/realtime", {"category": "linear", "settleCoin": "USDT"}))["list"])
    if left:
        raise SystemExit(f"{left} open orders are still there after cancelling them all, check the accounts")
    held = {ticker: [0.0, 0.0] for ticker in specs}
    foreign = []
    for position in await htx.get("/v5/trade/position/opens") or []:
        ticker = position["contract_code"].replace("-", "")
        if ticker not in specs:
            foreign.append(f"htx {ticker}")
            continue
        sign = -1 if position.get("direction") == "sell" else 1
        held[ticker][0] += sign * float(position["volume"]) * specs[ticker].contract
    for position in (await bybit.get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"}))["list"]:
        size = float(position["size"] or 0)
        if not size:
            continue
        if position["symbol"] not in specs:
            foreign.append(f"bybit {position['symbol']}")
            continue
        held[position["symbol"]][1] += size if position["side"] == "Buy" else -size
    if foreign:
        raise SystemExit(f"positions in coins this config doesn't trade: {', '.join(foreign)}; the bot's subaccounts should hold only its own")
    return {ticker: (round(htx_coins, 10), round(bybit_coins, 10)) for ticker, (htx_coins, bybit_coins) in held.items()}
