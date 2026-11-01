import asyncio
import math
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import aiohttp
from rich.table import Table
from scalpr_core import config as env
from scalpr_core.logger import console
from scalpr_strategy import FEES
from scalpr_trader.config import Config

from scalpr_live.bybit import BybitError, BybitRest
from scalpr_live.htx import SPOT_REST, HtxError, HtxRest
from scalpr_live.private import BybitPrivate, HtxPrivate
from scalpr_live.specs import Spec

# ACCOUNT CHECK: read-only requests to both real accounts, nothing is changed or ordered.
# Keys and secrets are never printed.
PINGS = 5  # signed requests behind the latency figures
OK, WARN, BAD = "ok", "warn", "bad"
# ascii: a Windows console in cp1251 can't print check marks
MARKS = {OK: "[green]ok[/]", WARN: "[yellow]![/]", BAD: "[red]XX[/]", "": ""}
ERRORS = (BybitError, HtxError, aiohttp.ClientError, TimeoutError, KeyError, IndexError, TypeError, ValueError)


@dataclass
class Report:
    exchange: str
    rows: list[tuple[str, str, str]] = field(default_factory=list)  # what, value, level
    coins: dict[str, dict] = field(default_factory=dict)  # ticker -> facts for the sizing table
    balance: float | None = None  # USDT equity
    leverage: dict[str, float] = field(default_factory=dict)

    def add(self, what: str, value: object, level: str = "") -> None:
        self.rows.append((what, str(value), level))

    # one group of requests; an error becomes a row instead of ending the check (answer shapes may surprise)
    async def step(self, what: str, run: Callable[[], Awaitable[None]]) -> None:
        try:
            await run()
        except ERRORS as error:
            self.add(what, f"{type(error).__name__}: {error}", BAD)


def _clock_row(report: Report, server_ns: float, before: int, after: int) -> None:
    offset = (server_ns - (before + after) / 2) / 1e6
    report.add("clock: exchange - ours", f"{offset:+.0f} ms", OK if abs(offset) < 1000 else WARN)


def _latency_row(report: Report, times: list[float]) -> None:
    report.add("signed REST round trip, median / min", f"{statistics.median(times) * 1000:.0f} / {min(times) * 1000:.0f} ms ({len(times)} requests from here)")


async def check_bybit(session: aiohttp.ClientSession, config: Config) -> Report:
    report = Report("Bybit")
    if not (env.BYBIT_API_KEY and env.BYBIT_API_SECRET):
        report.add("keys", "BYBIT_API_KEY / BYBIT_API_SECRET missing in .env", BAD)
        return report
    rest = BybitRest(session, env.BYBIT_API_KEY, env.BYBIT_API_SECRET)

    async def clock() -> None:
        before = time.time_ns()
        server = await rest.get("/v5/market/time", signed=False)
        _clock_row(report, int(server["timeNano"]), before, time.time_ns())

    async def api_key() -> None:
        info = await rest.get("/v5/user/query-api")
        report.add("key accepted", "yes", OK)
        report.add("read-only key", "yes" if info["readOnly"] else "no", BAD if info["readOnly"] else OK)
        permissions = {name: values for name, values in info["permissions"].items() if values}
        report.add("permissions", "; ".join(f"{name}: {','.join(values)}" for name, values in permissions.items()))
        trade = "Order" in permissions.get("ContractTrade", []) or "DerivativesTrade" in permissions.get("Derivatives", [])
        report.add("perp trading", "yes" if trade else "no", OK if trade else BAD)
        withdraw = any("Withdraw" in values for values in permissions.values())
        report.add("withdrawals", "ON" if withdraw else "off", BAD if withdraw else OK)
        ips = [ip for ip in info.get("ips", []) if ip != "*"]
        report.add("bound to IPs", ", ".join(ips) or "no", OK if ips else WARN)
        report.add("key expires", f"{info.get('expiredAt') or 'never'} ({info.get('deadlineDay')} days)")
        report.add("vip level / unified", f"{info.get('vipLevel')} / uta {info.get('uta')} unified {info.get('unified')}")

    async def account() -> None:
        info = await rest.get("/v5/account/info")
        report.add("margin mode", info.get("marginMode"), OK if info.get("marginMode") == "REGULAR_MARGIN" else WARN)
        report.add("unified margin status", info.get("unifiedMarginStatus"))
        for account_type in ("UNIFIED", "CONTRACT"):
            try:
                wallet = await rest.get("/v5/account/wallet-balance", {"accountType": account_type})
            except BybitError:
                continue
            total = wallet["list"][0]
            usdt = next((coin for coin in total["coin"] if coin["coin"] == "USDT"), {})
            report.balance = float(total.get("totalEquity") or usdt.get("equity") or 0)
            report.add(f"balance ({account_type})", f"equity ${report.balance:,.2f}, USDT wallet {float(usdt.get('walletBalance') or 0):,.2f}")
            return
        report.add("balance", "no UNIFIED or CONTRACT wallet answered", BAD)

    async def coin(ticker: str) -> None:
        inst = (await rest.get("/v5/market/instruments-info", {"category": "linear", "symbol": ticker}, signed=False))["list"][0]
        price = float((await rest.get("/v5/market/tickers", {"category": "linear", "symbol": ticker}, signed=False))["list"][0]["lastPrice"])
        fee = (await rest.get("/v5/account/fee-rate", {"category": "linear", "symbol": ticker}))["list"][0]
        positions = (await rest.get("/v5/position/list", {"category": "linear", "symbol": ticker}))["list"]
        lots = inst["lotSizeFilter"]
        report.coins[ticker] = {
            "price": price, "status": inst["status"], "tick": float(inst["priceFilter"]["tickSize"]),
            "qty_step": float(lots["qtyStep"]), "min_qty": float(lots["minOrderQty"]), "min_notional": float(lots.get("minNotionalValue") or 0),
            "maker": float(fee["makerFeeRate"]) * 100, "taker": float(fee["takerFeeRate"]) * 100,
            "position": sum(float(p["size"]) * (1 if p["side"] == "Buy" else -1) for p in positions),
            "one_way": all(p["positionIdx"] == 0 for p in positions),
        }
        if positions:
            report.leverage[ticker] = float(positions[0]["leverage"])

    # whatever else the account holds shares the margin with the bot (one cross-margin wallet)
    async def holdings() -> None:
        held = [p for p in (await rest.get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"}))["list"] if float(p["size"])]
        report.add("open positions (USDT perps)", "; ".join(f"{p['symbol']} {p['side']} {p['size']} (${float(p['positionValue']):,.0f}, {p['leverage']}x)" for p in held) or "none",
                   WARN if held else OK)
        found = (await rest.get("/v5/order/realtime", {"category": "linear", "settleCoin": "USDT"}))["list"]
        report.add("open orders (USDT perps)", "; ".join(f"{o['symbol']} {o['side']} {o['orderType']} {o['qty']}{' reduce-only' if o['reduceOnly'] else ''}" for o in found) or "none",
                   WARN if found else OK)

    async def latency() -> None:
        times = []
        for _ in range(PINGS):
            await rest.get("/v5/position/list", {"category": "linear", "symbol": config.coins[0].ticker})
            times.append(rest.latency)
        _latency_row(report, times)

    await report.step("clock", clock)
    await report.step("api key", api_key)
    await report.step("account", account)
    for item in config.coins:
        await report.step(item.ticker, lambda ticker=item.ticker: coin(ticker))
    await report.step("positions and orders", holdings)
    await report.step("latency", latency)
    return report


async def check_htx(session: aiohttp.ClientSession, config: Config) -> Report:
    report = Report("HTX")
    if not (env.HTX_API_KEY and env.HTX_API_SECRET):
        report.add("keys", "HTX_API_KEY / HTX_API_SECRET missing in .env", BAD)
        return report
    rest = HtxRest(session, env.HTX_API_KEY, env.HTX_API_SECRET)
    spot = HtxRest(session, env.HTX_API_KEY, env.HTX_API_SECRET, SPOT_REST)
    contracts = {item.ticker: f"{item.ticker.removesuffix('USDT')}-USDT" for item in config.coins}

    async def clock() -> None:
        before = time.time_ns()
        server = await rest.get("/api/v1/timestamp", signed=False, raw=True)
        _clock_row(report, int(server["ts"]) * 1e6, before, time.time_ns())

    async def api_key() -> None:
        uid = await spot.get("/v2/user/uid")
        info = (await spot.get("/v2/user/api-key", {"uid": uid, "accessKey": env.HTX_API_KEY}))[0]
        permission = info["permission"].split(",")
        report.add("key accepted", "yes", OK)
        report.add("permissions", info["permission"])
        report.add("trading", "yes" if "trade" in permission else "no", OK if "trade" in permission else BAD)
        report.add("withdrawals", "ON" if "withdraw" in permission else "off", BAD if "withdraw" in permission else OK)
        ips = info.get("ipAddresses") or ""
        report.add("bound to IPs", ips or "no", OK if ips else WARN)
        report.add("key valid for / status", f"{info.get('validDays')} days / {info.get('status')}")

    positions: list[dict] = []

    # a multi-asset account answers only the v5 api: the v1/v3 account endpoints refuse with 6023
    async def account() -> None:
        mode = (await rest.get("/v5/account/asset_mode"))["asset_mode"]
        report.add("asset mode", {1: "single-asset", 2: "multi-asset"}.get(mode, mode))
        balance = await rest.get("/v5/account/balance")
        usdt = next((asset for asset in balance.get("details") or [] if asset.get("currency") == "USDT"), {})
        report.balance = float(balance.get("equity") or 0)
        report.add("balance", f"equity ${report.balance:,.2f}, USDT available {usdt.get('available', 0)}, state {balance.get('state')}",
                   OK if report.balance > 0 else BAD)
        side = (await rest.get("/v5/position/mode"))["position_mode"]
        report.add("position mode", side, OK if side == "single_side" else WARN)
        positions.extend(await rest.get("/v5/trade/position/opens") or [])
        report.add("open positions (all perps)", "; ".join(str(p) for p in positions) or "none", WARN if positions else OK)
        orders = await rest.get("/v5/trade/order/opens") or []
        report.add("open orders (all perps)", len(orders), WARN if orders else OK)

    async def coin(ticker: str) -> None:
        contract = contracts[ticker]
        info = (await rest.get("/linear-swap-api/v1/swap_contract_info", {"contract_code": contract}, signed=False))[0]
        price = float((await rest.get("/linear-swap-ex/market/detail/merged", {"contract_code": contract}, signed=False, raw=True))["tick"]["close"])
        fee = (await rest.post("/linear-swap-api/v1/swap_fee", {"contract_code": contract}))[0]
        lever = (await rest.get("/v5/position/lever", {"contract_code": contract, "margin_mode": "cross"}))[0]
        size = float(info["contract_size"])
        held = [p for p in positions if p.get("contract_code") == contract]
        report.coins[ticker] = {
            "price": price, "status": info["contract_status"], "tick": float(info["price_tick"]), "contract_size": size,
            "maker": float(fee["open_maker_fee"]) * 100, "taker": float(fee["open_taker_fee"]) * 100,
            # the v5 position shape isn't seen yet (no positions so far): volume in contracts, short by its side
            "position": sum(float(p.get("volume") or 0) * size * (-1 if "short" in (p.get("position_side"), p.get("side")) or p.get("direction") == "sell" else 1) for p in held),
        }
        report.leverage[ticker] = float(lever["lever_rate"])

    async def latency() -> None:
        times = []
        for _ in range(PINGS):
            await rest.get("/v5/trade/order/opens", {"contract_code": contracts[config.coins[0].ticker]})
            times.append(rest.latency)
        _latency_row(report, times)

    await report.step("clock", clock)
    await report.step("api key", api_key)
    await report.step("account", account)
    for item in config.coins:
        await report.step(item.ticker, lambda ticker=item.ticker: coin(ticker))
    await report.step("latency", latency)
    return report


# SIZES: what the config's quotes and hedges turn into on the real contracts, and the fees against the model
def sizing(config: Config, htx: Report, bybit: Report) -> tuple[Table, list[tuple[str, str, str]]]:
    table = Table(title="Coins: config vs contracts")
    for column in ("ticker", "price", "htx contract", "quote", "bybit qty step / min", "smallest hedge", "steps fit",
                   "htx maker %", "bybit maker / taker %", "position htx / bybit", "leverage htx / bybit"):
        table.add_column(column, justify="right")
    notes: list[tuple[str, str, str]] = []
    for coin in config.coins:
        h, b = htx.coins.get(coin.ticker), bybit.coins.get(coin.ticker)
        if h is None or b is None:
            table.add_row(coin.ticker, "no data")
            continue
        price, size = b["price"], h["contract_size"]
        spec = Spec(coin.ticker, h.get("tick", 0.0), size, b["qty_step"], b["min_qty"], b["min_notional"])
        contracts = spec.contracts(coin.notional, price)
        hedge = spec.min_hedge(price)
        fits = spec.steps_fit
        table.add_row(
            coin.ticker, f"{price:g}", f"{size:g} coin = ${price * size:.3f}",
            f"${coin.notional:g} -> {contracts} contracts (${contracts * price * size:.2f})",
            f"{b['qty_step']:g} / {b['min_qty']:g}, min ${b['min_notional']:g}",
            f"{hedge:g} coin = ${hedge * price:.2f} = {math.ceil(hedge / size)} htx contracts",
            MARKS[OK] if fits else MARKS[BAD],
            f"{h['maker']:.4f} (model {FEES['htx']['maker']})", f"{b['maker']:.4f} / {b['taker']:.4f} (model taker {FEES['bybit']['taker']})",
            f"{h['position']:+g} / {b['position']:+g}",
            f"{htx.leverage.get(coin.ticker, '?')} / {bybit.leverage.get(coin.ticker, '?')}",
        )
        if not fits:
            notes.append((coin.ticker, f"htx contract {size:g} is not a whole number of bybit steps {b['qty_step']:g}: hedges leave remainders", BAD))
        if hedge * price > price * size:
            notes.append((coin.ticker, f"a fill under ${hedge * price:.2f} can't be hedged alone on bybit: remainders must add up first", WARN))
        cost = h["maker"] + b["taker"]
        model = FEES["htx"]["maker"] + FEES["bybit"]["taker"]
        if cost > model + 1e-9:
            notes.append((coin.ticker, f"fees {cost:.4f}% per hedged fill vs {model:.4f}% in the model: results drop by the difference", WARN))
        if h["position"] or b["position"]:
            notes.append((coin.ticker, f"open positions: htx {h['position']:+g}, bybit {b['position']:+g} coins", WARN))
        if not b["one_way"]:
            notes.append((coin.ticker, "bybit position mode is hedge (two sides), the trader expects one-way", WARN))
    # every coin can be at max_position at once; the margin it takes depends on the leverage
    exposure = sum(coin.max_position for coin in config.coins)
    for report in (htx, bybit):
        leverage = min(report.leverage.values(), default=0)
        if report.balance is None or not leverage:
            continue
        margin = exposure / leverage
        level = OK if report.balance >= 2 * margin else WARN
        notes.append((report.exchange, f"max exposure ${exposure:g} at leverage {leverage:g}x takes ${margin:.0f} margin; balance ${report.balance:,.2f}", level))
    return table, notes


def render(report: Report) -> Table:
    table = Table(title=report.exchange, show_header=False)
    table.add_column("", width=2)
    table.add_column("check")
    table.add_column("value", overflow="fold")
    for what, value, level in report.rows:
        table.add_row(MARKS[level], what, value)
    return table


# the private websockets the order layer listens to: logged in and subscribed within `seconds`
async def check_private(session: aiohttp.ClientSession, reports: dict[str, Report], seconds: float = 10) -> None:
    keys = {"htx": (env.HTX_API_KEY, env.HTX_API_SECRET), "bybit": (env.BYBIT_API_KEY, env.BYBIT_API_SECRET)}
    feeds = [kind(*keys[kind.exchange]) for kind in (HtxPrivate, BybitPrivate) if all(keys[kind.exchange])]
    tasks = [asyncio.create_task(feed.run(session, lambda update: None)) for feed in feeds]
    started = time.monotonic()
    while time.monotonic() - started < seconds and not all(feed.ready for feed in feeds):
        await asyncio.sleep(0.1)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for feed in feeds:
        took = f"in {time.monotonic() - started:.1f} s" if feed.ready else f"not within {seconds:g} s: {feed.error}"
        reports[feed.exchange].add("private websocket (orders)", f"{'logged in and subscribed' if feed.ready else 'FAILED'} {took}",
                                   OK if feed.ready else BAD)


async def run(config: Config) -> None:
    # trust_env: HTTP(S)_PROXY from the environment, as the collector does
    async with aiohttp.ClientSession(trust_env=True) as session:
        htx = await check_htx(session, config)
        bybit = await check_bybit(session, config)
        await check_private(session, {"htx": htx, "bybit": bybit})
    coins, notes = sizing(config, htx, bybit)
    for table in (render(htx), render(bybit), coins):
        console.print(table)
    # the summary: the sizing notes and every flagged row, the blocking ones first
    summary = notes + [(report.exchange, f"{what}: {value}", level) for report in (htx, bybit) for what, value, level in report.rows if level in (WARN, BAD)]
    for where, what, level in sorted(summary, key=lambda note: (BAD, WARN, OK).index(note[2])):
        console.print(f"{MARKS[level]} {where}: {what}")
    levels = [level for _, _, level in summary]
    console.print(f"\n{levels.count(BAD)} blocking, {levels.count(WARN)} to look at")
