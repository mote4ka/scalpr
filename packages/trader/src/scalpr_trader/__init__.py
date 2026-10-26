import argparse
import asyncio
import time
from dataclasses import asdict
from pathlib import Path

from rich.live import Live
from scalpr_collector.exchanges import htx as htx_rest
from scalpr_collector.http_client import SessionManager
from scalpr_collector.realtime.bybit import BybitFeed
from scalpr_collector.realtime.events import Event
from scalpr_collector.realtime.feed import Feed, replay_together
from scalpr_collector.realtime.htx import HtxFeed
from scalpr_collector.realtime.recorder import STREAM_DIR, Recorder
from scalpr_core.logger import console, logger

from scalpr_trader.config import Config, load
from scalpr_trader.dashboard import dashboard
from scalpr_trader.engine import Engine
from scalpr_trader.journal import Journal

BYBIT_DEPTH, HTX_DEPTH = 50, 20
HEARTBEAT = 0.2  # s, time-driven work when no event comes


# uv run --package scalpr-trader trader --config packages/trader/trader.toml
# uv run --package scalpr-trader trader --config packages/trader/trader.toml --replay data/stream-test/run1
def main() -> None:
    parser = argparse.ArgumentParser(description="Paper trading of XEMM: quotes on HTX, hedges on Bybit, nothing sent to the exchanges")
    parser.add_argument("--config", type=Path, required=True, help="trader.toml, see packages/trader/config.example.toml")
    parser.add_argument("--replay", type=Path, help="run on a recording of the stream command instead of the live feeds")
    parser.add_argument("--minutes", type=float, default=0, help="live: stop after this long (default 0: until Ctrl+C)")
    parser.add_argument("--record", type=Path, nargs="?", const=STREAM_DIR, help="live: also record the raw feeds (default folder data/stream)")
    parser.add_argument("--no-journal", action="store_true", help="don't write fills and snapshots to data/paper")
    args = parser.parse_args()

    config = load(args.config)
    journal = None if args.no_journal else Journal()
    if journal is not None:
        # what paper-report needs to rerun the same strategy in the backtest
        journal.write_run({
            "mode": "replay" if args.replay else "live",
            "replay": str(args.replay) if args.replay else None,
            "record": str(args.record) if args.record else None,
            "config": asdict(config),
        })
    try:
        if args.replay:
            asyncio.run(replay(config, args.replay, journal))
        else:
            asyncio.run(live(config, args.minutes, args.record, journal))
    except KeyboardInterrupt:
        logger.info("Stopped")
    finally:
        if journal is not None:
            journal.close()
            logger.info(f"Journal in {journal.folder}")


async def live(config: Config, minutes: float, record: Path | None, journal: Journal | None) -> None:
    manager = SessionManager()
    session = await manager.getSession()
    recorder = Recorder(record) if record is not None else None
    tasks: list[asyncio.Task] = []
    try:
        engine, feeds = await setup(session, config, journal)
        tasks = [asyncio.create_task(feed.run(session, engine.on_event, recorder)) for feed in feeds]
        started = time.monotonic()
        with Live(dashboard(engine, "Paper XEMM"), console=console, refresh_per_second=2) as view:
            beats = 0
            while not minutes or time.monotonic() - started < minutes * 60:
                await asyncio.sleep(HEARTBEAT)
                # feeds run until cancelled: one that stopped hit a bug, its error surfaces here
                for task in tasks:
                    if task.done():
                        task.result()
                engine.tick(time.time_ns())
                beats += 1
                if beats % round(1 / HEARTBEAT) == 0:
                    view.update(dashboard(engine, f"Paper XEMM, {time.monotonic() - started:.0f} s"))
                    for sink in (recorder, journal):
                        if sink is not None:
                            sink.flush()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if recorder is not None:
            recorder.close()
        await manager.closeSession()


# the same engine on a recording: events in the order they came live, time follows their receive times
async def replay(config: Config, folder: Path, journal: Journal | None) -> None:
    manager = SessionManager()
    session = await manager.getSession()
    try:
        engine, feeds = await setup(session, config, journal)
    finally:
        await manager.closeSession()
    recordings = [(feed, sorted((folder / feed.exchange).rglob("*.jsonl.gz"))) for feed in feeds]
    for feed, paths in recordings:
        if not paths:
            raise SystemExit(f"no {feed.exchange} recording in {folder}")
    started, first, events = time.monotonic(), None, 0

    def on_event(event: Event) -> None:
        nonlocal first, events
        first, events = first or event.received, events + 1
        engine.on_event(event)

    replay_together(recordings, on_event)
    covered = (engine.now - first) / 60e9 if first else 0
    console.print(dashboard(engine, f"Paper XEMM on {folder}: {covered:.1f} min of feeds, {events:,} events, replayed in {time.monotonic() - started:.0f} s"))


async def setup(session, config: Config, journal: Journal | None) -> tuple[Engine, list[Feed]]:
    tickers = [coin.ticker for coin in config.coins]
    specs = await htx_rest.get_contract_specs(session)
    missing = [ticker for ticker in tickers if ticker not in specs]
    if missing:
        raise SystemExit(f"not traded on htx: {', '.join(missing)}")
    engine = Engine(config, {ticker: specs[ticker]["tick"] for ticker in tickers}, journal)
    feeds: list[Feed] = [BybitFeed(tickers, BYBIT_DEPTH), HtxFeed(tickers, {ticker: specs[ticker]["size"] for ticker in tickers}, HTX_DEPTH)]
    return engine, feeds
