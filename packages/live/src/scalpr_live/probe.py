import asyncio
import statistics
import time

import aiohttp
from rich.table import Table
from scalpr_core import config as env
from scalpr_core.logger import console

from scalpr_live.bybit import BybitRest
from scalpr_live.htx import HtxRest
from scalpr_live.private import BybitPrivate, HtxPrivate, OrderUpdate
from scalpr_live.real import cancel_body, prepare
from scalpr_live.specs import Spec, load_specs

TICKER = "DOTUSDT"  # the cheapest contract the bot trades: 1 DOT on htx
DISTANCE = 0.03  # orders sit this far below the best bid: they fill only if the price falls 3 % within a second
WAIT = 5.0  # s for the private feed to tell about an order
MS = 1_000_000


# what the private feeds told about each probe order: when it was new and when final, and whether anything filled
class Waiter:
    def __init__(self):
        self.seen: dict[tuple[str, str], int] = {}
        self.events: dict[tuple[str, str], asyncio.Event] = {}
        self.fills: list[OrderUpdate] = []

    def on_update(self, update: OrderUpdate) -> None:
        if update.filled:
            self.fills.append(update)
        for kind, happened in (("new", update.state == "new"), ("final", update.final)):
            key = (update.client_id, kind)
            if happened and key not in self.seen:
                self.seen[key] = update.received
                self.events.setdefault(key, asyncio.Event()).set()

    async def wait(self, client_id: str, kind: str) -> int | None:
        event = self.events.setdefault((client_id, kind), asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), WAIT)
        except TimeoutError:
            return None
        return self.seen[(client_id, kind)]


# PROBE: how fast orders go in and out from this machine. Post-only buys of the smallest size DISTANCE below the best
# bid on both exchanges, cancelled at once: REST round trips of the place and the cancel, and how long until the
# private feed tells the order is in and that it is gone. `real` False only shows what would be sent.
async def run(rounds: int, real: bool) -> None:
    async with aiohttp.ClientSession(trust_env=True) as session:
        spec = (await load_specs(session, [TICKER]))[TICKER]
        htx = HtxRest(session, env.HTX_API_KEY or "", env.HTX_API_SECRET or "")
        bybit = BybitRest(session, env.BYBIT_API_KEY or "", env.BYBIT_API_SECRET or "")
        htx_bid = float((await htx.get("/linear-swap-ex/market/detail/merged", {"contract_code": spec.contract_code}, signed=False, raw=True))["tick"]["bid"][0])
        bybit_bid = float((await bybit.get("/v5/market/tickers", {"category": "linear", "symbol": TICKER}, signed=False))["list"][0]["bid1Price"])
        htx_price, bybit_price = htx_bid * (1 - DISTANCE), bybit_bid * (1 - DISTANCE)
        qty = spec.min_hedge(bybit_price)
        console.print(f"{rounds} rounds on {TICKER}, each a post-only buy, cancelled at once:\n"
                      f"  htx:   1 contract ({spec.contract:g} DOT, ${htx_price * spec.contract:.2f}) at {spec.price_text(htx_price)}, best bid {htx_bid:g}\n"
                      f"  bybit: {spec.qty_text(qty)} DOT (${qty * bybit_price:.2f}) at {spec.hedge_price_text(bybit_price, True)}, best bid {bybit_bid:g}")
        if not real:
            console.print("nothing sent: add --real to place these orders")
            return
        if not all((env.HTX_API_KEY, env.HTX_API_SECRET, env.BYBIT_API_KEY, env.BYBIT_API_SECRET)):
            raise SystemExit("HTX_API_KEY/SECRET and BYBIT_API_KEY/SECRET are needed in .env")
        waiter = Waiter()
        feeds = [HtxPrivate(env.HTX_API_KEY, env.HTX_API_SECRET), BybitPrivate(env.BYBIT_API_KEY, env.BYBIT_API_SECRET)]
        tasks = [asyncio.create_task(feed.run(session, waiter.on_update)) for feed in feeds]
        rows: list[dict] = []
        try:
            for _ in range(int(WAIT * 10)):
                if all(feed.ready for feed in feeds):
                    break
                await asyncio.sleep(0.1)
            else:
                raise SystemExit(f"private feeds not ready: {[(feed.exchange, feed.error) for feed in feeds]}")
            await prepare(htx, bybit, {TICKER: spec})
            start = time.time_ns() // MS
            for n in range(rounds):
                rows.append(await _htx_round(htx, spec, waiter, str(start * 1000 + n), htx_price))
                rows.append(await _bybit_round(bybit, spec, waiter, f"p{start}-{n}", qty, bybit_price))
                if waiter.fills:
                    console.print(f"[red]a probe order filled[/]: {waiter.fills}; stopping")
                    break
                await asyncio.sleep(1)
        finally:
            await _clean(htx, bybit, spec)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        _report(rows)


async def _htx_round(htx: HtxRest, spec: Spec, waiter: Waiter, client_id: str, price: float) -> dict:
    body = {"contract_code": spec.contract_code, "margin_mode": "cross", "position_side": "both", "side": "buy", "type": "post_only",
            "price": spec.price_text(price), "volume": "1", "client_order_id": client_id}
    return await _round("htx", waiter, client_id,
                        lambda: htx.post("/v5/trade/order", body),
                        lambda: htx.post("/v5/trade/cancel_order", cancel_body(spec, int(client_id))))


async def _bybit_round(bybit: BybitRest, spec: Spec, waiter: Waiter, link: str, qty: float, price: float) -> dict:
    body = {"category": "linear", "symbol": TICKER, "side": "Buy", "orderType": "Limit", "qty": spec.qty_text(qty),
            "price": spec.hedge_price_text(price, True), "timeInForce": "PostOnly", "orderLinkId": link, "positionIdx": 0}
    return await _round("bybit", waiter, link,
                        lambda: bybit.post("/v5/order/create", body),
                        lambda: bybit.post("/v5/order/cancel", {"category": "linear", "symbol": TICKER, "orderLinkId": link}))


async def _round(exchange: str, waiter: Waiter, client_id: str, place, cancel) -> dict:
    row = {"exchange": exchange}
    sent = time.time_ns()
    try:
        await place()
    except Exception as error:  # noqa: BLE001 - every failure is a result of the probe
        return {**row, "error": f"place: {error}"}
    row["place"] = (time.time_ns() - sent) / MS
    pushed = await waiter.wait(client_id, "new")
    row["place push"] = (pushed - sent) / MS if pushed else None
    sent = time.time_ns()
    try:
        await cancel()
    except Exception as error:  # noqa: BLE001
        return {**row, "error": f"cancel: {error}"}
    row["cancel"] = (time.time_ns() - sent) / MS
    pushed = await waiter.wait(client_id, "final")
    row["cancel push"] = (pushed - sent) / MS if pushed else None
    return row


# nothing may stay behind: every open order cancelled, positions shown
async def _clean(htx: HtxRest, bybit: BybitRest, spec: Spec) -> None:
    try:
        held = await prepare(htx, bybit, {TICKER: spec})
    except Exception as error:  # noqa: BLE001 - say it, whatever it is
        console.print(f"[red]cleaning up failed: {error}; check both accounts for open orders and positions[/]")
        return
    htx_coins, bybit_coins = held[TICKER]
    if htx_coins or bybit_coins:
        console.print(f"[red]positions after the probe: htx {htx_coins:+g} DOT, bybit {bybit_coins:+g} DOT[/]")
    else:
        console.print("no open orders and no positions left")


def _report(rows: list[dict]) -> None:
    table = Table(title="Order latency from this machine, ms (median / min / max)")
    for column in ("exchange", "place, REST", "place -> private push", "cancel, REST", "cancel -> private push", "errors"):
        table.add_column(column, justify="right")
    for exchange in ("htx", "bybit"):
        mine = [row for row in rows if row["exchange"] == exchange]
        cells = []
        for key in ("place", "place push", "cancel", "cancel push"):
            values = [row[key] for row in mine if row.get(key) is not None]
            cells.append(f"{statistics.median(values):.0f} / {min(values):.0f} / {max(values):.0f}" if values else "-")
        errors = [row["error"] for row in mine if "error" in row]
        missing = sum(row.get(key) is None for row in mine if "error" not in row for key in ("place push", "cancel push"))
        table.add_row(exchange, *cells, "; ".join(errors[:2]) + (f" {missing} pushes missing" if missing else ""))
    console.print(table)
