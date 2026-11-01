import argparse
import asyncio
import time
from dataclasses import asdict
from pathlib import Path

from rich.live import Live
from scalpr_collector.http_client import SessionManager
from scalpr_collector.realtime.feed import replay_together
from scalpr_core.logger import console, logger
from scalpr_core.paths import DATA_DIR, PROJECT_ROOT
from scalpr_trader import setup
from scalpr_trader.basis import load_samples
from scalpr_trader.config import Config, load
from scalpr_trader.dashboard import write_file
from scalpr_trader.journal import Journal

from scalpr_live import check
from scalpr_live.dashboard import dashboard
from scalpr_live.settings import Settings, load_settings
from scalpr_live.sim import SimExchange
from scalpr_live.specs import load_specs
from scalpr_live.trader import LiveTrader

EXAMPLE_CONFIG = PROJECT_ROOT / "packages" / "trader" / "config.example.toml"
LIVE_DIR = DATA_DIR / "live"
HEARTBEAT = 0.2  # s, time-driven work when no event comes


# uv run --package scalpr-live live check [--config trader.toml]
# uv run --package scalpr-live live run --config trader.toml [--minutes N] [--replay DIR]
def main() -> None:
    parser = argparse.ArgumentParser(description="The real HTX and Bybit accounts, keys from .env; nothing here places orders yet")
    commands = parser.add_subparsers(dest="command", required=True)
    account = commands.add_parser("check", help="read-only: keys and their permissions, balances, account modes, fees, contract sizes, REST latency")
    account.add_argument("--config", type=Path, default=EXAMPLE_CONFIG, help="trader.toml for the coins, notional and max_position (default: config.example.toml)")
    run = commands.add_parser("run", help="dry run: the order layer on live feeds (or a recording) against simulated exchanges, nothing is sent")
    run.add_argument("--config", type=Path, required=True, help="trader.toml: the strategy, plus an optional [live] table (see scalpr_live.settings)")
    run.add_argument("--minutes", type=float, default=0, help="stop after this long (default 0: until Ctrl+C)")
    run.add_argument("--replay", type=Path, help="run on a recording of the stream command instead of the live feeds")
    run.add_argument("--no-journal", action="store_true", help="don't write orders, fills, hedges and snapshots to data/live")
    run.add_argument("--dashboard-file", type=Path, help="also write the dashboard to this file every second")
    args = parser.parse_args()
    if args.command == "check":
        asyncio.run(check.run(load(args.config)))
        return
    config, settings = load(args.config), load_settings(args.config)
    journal = None if args.no_journal else Journal(LIVE_DIR)
    if journal is not None:
        journal.write_run({"mode": "replay" if args.replay else "live", "dry_run": True, "replay": str(args.replay) if args.replay else None,
                           "config": asdict(config), "settings": asdict(settings)})
    try:
        if args.replay:
            asyncio.run(replay(config, settings, args.replay, journal))
        else:
            asyncio.run(live(config, settings, args.minutes, journal, args.dashboard_file))
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


async def live(config: Config, settings: Settings, minutes: float, journal: Journal | None, dashboard_file: Path | None) -> None:
    manager = SessionManager()
    session = await manager.getSession()
    tasks: list[asyncio.Task] = []
    try:
        trader, feeds = await build(session, config, settings, journal)
        # the basis the paper trader saved on this machine, within its window (read only: that trader keeps writing it)
        now = time.time_ns()
        for ticker, state in trader.engine.coins.items():
            state.basis.restore(load_samples(ticker, now - config.basis_window), now)
            if state.basis.samples:
                logger.info(f"{ticker}: basis from {len(state.basis.samples)} s of saved samples")
        tasks = [asyncio.create_task(feed.run(session, trader.on_event, None)) for feed in feeds]
        started = time.monotonic()
        with Live(dashboard(trader, "Live XEMM, dry run"), console=console, refresh_per_second=2) as view:
            beats = 0
            while not minutes or time.monotonic() - started < minutes * 60:
                await asyncio.sleep(HEARTBEAT)
                for task in tasks:
                    if task.done():
                        task.result()
                trader.step(time.time_ns())
                beats += 1
                if beats % round(1 / HEARTBEAT) == 0:
                    table = dashboard(trader, f"Live XEMM, dry run, {time.monotonic() - started:.0f} s")
                    view.update(table)
                    if dashboard_file is not None:
                        write_file(table, dashboard_file)
                    if journal is not None:
                        journal.flush()
    finally:
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
