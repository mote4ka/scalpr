import argparse
import asyncio
import time
from dataclasses import asdict
from pathlib import Path

from rich.live import Live
from scalpr_collector.http_client import SessionManager
from scalpr_collector.realtime.feed import replay_together
from scalpr_core import config as env
from scalpr_core.logger import console, logger
from scalpr_core.paths import DATA_DIR, PROJECT_ROOT
from scalpr_trader import setup
from scalpr_trader.basis import load_samples
from scalpr_trader.config import Config, load
from scalpr_trader.dashboard import write_file
from scalpr_trader.journal import Journal

from scalpr_live import check
from scalpr_live.bybit import BybitRest
from scalpr_live.dashboard import dashboard
from scalpr_live.htx import HtxRest
from scalpr_live.private import BybitPrivate, HtxPrivate
from scalpr_live.probe import run as probe_run
from scalpr_live.real import RealExchange, prepare
from scalpr_live.settings import Settings, load_settings
from scalpr_live.sim import SimExchange
from scalpr_live.specs import load_specs
from scalpr_live.trader import LiveTrader

EXAMPLE_CONFIG = PROJECT_ROOT / "packages" / "trader" / "config.example.toml"
LIVE_DIR = DATA_DIR / "live"
HEARTBEAT = 0.2  # s, time-driven work when no event comes


# uv run --package scalpr-live live check [--config trader.toml]
# uv run --package scalpr-live live run --config trader.toml [--minutes N] [--replay DIR] [--real]
# uv run --package scalpr-live live probe [--rounds N] [--real]
def main() -> None:
    parser = argparse.ArgumentParser(description="The real HTX and Bybit accounts, keys from .env; orders are sent only with --real")
    commands = parser.add_subparsers(dest="command", required=True)
    account = commands.add_parser("check", help="read-only: keys and their permissions, balances, account modes, fees, contract sizes, REST latency")
    account.add_argument("--config", type=Path, default=EXAMPLE_CONFIG, help="trader.toml for the coins, notional and max_position (default: config.example.toml)")
    run = commands.add_parser("run", help="the order layer on live feeds (or a recording): against simulated exchanges, or with --real the real ones")
    run.add_argument("--config", type=Path, required=True, help="trader.toml: the strategy, plus an optional [live] table (see scalpr_live.settings)")
    run.add_argument("--minutes", type=float, default=0, help="stop after this long (default 0: until Ctrl+C)")
    run.add_argument("--replay", type=Path, help="run on a recording of the stream command instead of the live feeds")
    run.add_argument("--no-journal", action="store_true", help="don't write orders, fills, hedges and snapshots to data/live")
    run.add_argument("--dashboard-file", type=Path, help="also write the dashboard to this file every second")
    run.add_argument("--real", action="store_true", help="REAL ORDERS on the accounts of the keys in .env (the bot's subaccounts)")
    probe = commands.add_parser("probe", help="order latency: post-only buys 3%% below the market on both exchanges, cancelled at once")
    probe.add_argument("--rounds", type=int, default=10, help="orders per exchange (default 10)")
    probe.add_argument("--real", action="store_true", help="send them; without it only shows what would be sent")
    args = parser.parse_args()
    if args.command == "check":
        asyncio.run(check.run(load(args.config)))
        return
    if args.command == "probe":
        asyncio.run(probe_run(args.rounds, args.real))
        return
    if args.real and args.replay:
        raise SystemExit("--real trades on the live feeds, not on a recording")
    config, settings = load(args.config), load_settings(args.config)
    journal = None if args.no_journal else Journal(LIVE_DIR)
    if journal is not None:
        journal.write_run({"mode": "replay" if args.replay else "live", "dry_run": not args.real, "replay": str(args.replay) if args.replay else None,
                           "config": asdict(config), "settings": asdict(settings)})
    try:
        if args.replay:
            asyncio.run(replay(config, settings, args.replay, journal))
        else:
            asyncio.run(live(config, settings, args.minutes, journal, args.dashboard_file, args.real))
    except KeyboardInterrupt:
        logger.info("Stopped")
    finally:
        if journal is not None:
            journal.close()
            logger.info(f"Journal in {journal.folder}")


async def build(session, config: Config, settings: Settings, journal: Journal | None) -> tuple[LiveTrader, list]:
    specs = await load_specs(session, [coin.ticker for coin in config.coins])
    for spec in specs.values():
        if not spec.steps_fit:
            raise SystemExit(f"{spec.ticker}: an htx contract ({spec.contract:g}) is not a whole number of bybit steps ({spec.qty_step:g})")
    engine, feeds = await setup(session, config, None)
    trader = LiveTrader(config, settings, engine, specs, journal)
    trader.exchange = SimExchange(engine, config, specs, trader)
    if journal is not None:
        journal.write("specs", {ticker: asdict(spec) for ticker, spec in specs.items()})
    return trader, feeds


# REAL: the exchanges behind the trader. The private feeds come up first, then whatever the subaccounts hold is
# cleaned up (open orders cancelled) and taken over (positions); legs that don't offset stop the start
async def go_real(session, trader: LiveTrader) -> tuple[RealExchange, list[asyncio.Task]]:
    if not all((env.HTX_API_KEY, env.HTX_API_SECRET, env.BYBIT_API_KEY, env.BYBIT_API_SECRET)):
        raise SystemExit("HTX_API_KEY/SECRET and BYBIT_API_KEY/SECRET are needed in .env")
    specs = {ticker: live.spec for ticker, live in trader.coins.items()}
    htx, bybit = HtxRest(session, env.HTX_API_KEY, env.HTX_API_SECRET), BybitRest(session, env.BYBIT_API_KEY, env.BYBIT_API_SECRET)
    exchange = RealExchange(htx, bybit, specs, trader, after=trader.step, write=trader.write)
    exchange.private = [HtxPrivate(env.HTX_API_KEY, env.HTX_API_SECRET), BybitPrivate(env.BYBIT_API_KEY, env.BYBIT_API_SECRET)]
    tasks = [asyncio.create_task(feed.run(session, exchange.on_update)) for feed in exchange.private]
    for _ in range(100):
        if all(feed.ready for feed in exchange.private):
            break
        await asyncio.sleep(0.1)
    else:
        for task in tasks:
            task.cancel()
        raise SystemExit(f"private feeds not ready: {[(feed.exchange, feed.error) for feed in exchange.private]}")
    positions = await prepare(htx, bybit, specs)
    for ticker, (maker, hedge) in positions.items():
        price = float((await bybit.get("/v5/market/tickers", {"category": "linear", "symbol": ticker}, signed=False))["list"][0]["lastPrice"])
        if abs(maker + hedge) >= specs[ticker].min_hedge(price):
            raise SystemExit(f"{ticker}: htx {maker:+g} and bybit {hedge:+g} coins don't offset; even them out by hand first")
        if maker or hedge:
            logger.info(f"{ticker}: taking over htx {maker:+g}, bybit {hedge:+g} coins")
    trader.restore(positions)
    trader.exchange, trader.real = exchange, True
    trader.write("positions", {"t": time.time_ns(), "start": {ticker: list(held) for ticker, held in positions.items()}})
    return exchange, tasks


async def live(config: Config, settings: Settings, minutes: float, journal: Journal | None, dashboard_file: Path | None, real: bool = False) -> None:
    manager = SessionManager()
    session = await manager.getSession()
    tasks: list[asyncio.Task] = []
    exchange: RealExchange | None = None
    try:
        trader, feeds = await build(session, config, settings, journal)
        if real:
            exchange, tasks = await go_real(session, trader)
        # the basis the paper trader saved on this machine, within its window (read only: that trader keeps writing it)
        now = time.time_ns()
        for ticker, state in trader.engine.coins.items():
            state.basis.restore(load_samples(ticker, now - config.basis_window), now)
            if state.basis.samples:
                logger.info(f"{ticker}: basis from {len(state.basis.samples)} s of saved samples")
        tasks += [asyncio.create_task(feed.run(session, trader.on_event, None)) for feed in feeds]
        started = time.monotonic()
        name = "Live XEMM" if real else "Live XEMM, dry run"
        with Live(dashboard(trader, name), console=console, refresh_per_second=2) as view:
            beats = 0
            while not minutes or time.monotonic() - started < minutes * 60:
                await asyncio.sleep(HEARTBEAT)
                for task in tasks:
                    if task.done():
                        task.result()
                trader.step(time.time_ns())
                beats += 1
                if beats % round(1 / HEARTBEAT) == 0:
                    table = dashboard(trader, f"{name}, {time.monotonic() - started:.0f} s")
                    view.update(table)
                    if dashboard_file is not None:
                        write_file(table, dashboard_file)
                    if journal is not None:
                        journal.flush()
    finally:
        # real orders come off first, whatever ended the run
        if exchange is not None:
            await exchange.close()
            logger.info("all htx orders cancelled")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.closeSession()


async def replay(config: Config, settings: Settings, folder: Path, journal: Journal | None) -> None:
    manager = SessionManager()
    session = await manager.getSession()
    try:
        trader, feeds = await build(session, config, settings, journal)
    finally:
        await manager.closeSession()
    recordings = [(feed, sorted((folder / feed.exchange).rglob("*.jsonl.gz"))) for feed in feeds]
    for feed, paths in recordings:
        if not paths:
            raise SystemExit(f"no {feed.exchange} recording in {folder}")
    started = time.monotonic()
    replay_together(recordings, trader.on_event)
    console.print(dashboard(trader, f"Live XEMM dry run on {folder}, replayed in {time.monotonic() - started:.0f} s"))
