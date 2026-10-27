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

from scalpr_trader.basis import load_samples, save_samples
from scalpr_trader.config import Config, load
from scalpr_trader.dashboard import dashboard, write_file
from scalpr_trader.engine import Engine
from scalpr_trader.journal import Journal

BYBIT_DEPTH, HTX_DEPTH = 50, 20
HEARTBEAT = 0.2  # s, time-driven work when no event comes
BASIS_SAVE = 10  # s, the basis samples go to disk this often and on the way out


# uv run --package scalpr-trader trader --config packages/trader/trader.toml
# uv run --package scalpr-trader trader --config packages/trader/trader.toml --replay data/stream-test/run1
def main() -> None:
    parser = argparse.ArgumentParser(description="Paper trading of XEMM: quotes on HTX, hedges on Bybit, nothing sent to the exchanges")
    parser.add_argument("--config", type=Path, required=True, help="trader.toml, see packages/trader/config.example.toml")
    parser.add_argument("--replay", type=Path, help="run on a recording of the stream command instead of the live feeds")
    parser.add_argument("--minutes", type=float, default=0, help="live: stop after this long (default 0: until Ctrl+C)")
    parser.add_argument("--record", type=Path, nargs="?", const=STREAM_DIR, help="live: also record the raw feeds (default folder data/stream)")
    parser.add_argument("--no-journal", action="store_true", help="don't write fills and snapshots to data/paper")
    parser.add_argument("--dashboard-file", type=Path, help="live: also write the dashboard to this file every second, to watch a trader without a terminal")
    args = parser.parse_args()

    config = load(args.config)
    # live: the basis samples the run before left within the window, no warm-up for what they cover
    since = time.time_ns() - config.basis_window
    warm = {} if args.replay else {coin.ticker: load_samples(coin.ticker, since) for coin in config.coins}
    journal = None if args.no_journal else Journal()
    if journal is not None:
        # what paper-report needs to rerun the same strategy in the backtest
        journal.write_run({
            "mode": "replay" if args.replay else "live",
            "replay": str(args.replay) if args.replay else None,
            "record": str(args.record) if args.record else None,
            # basis samples per coin taken over from the run before
            "warm": {ticker: len(samples) for ticker, samples in warm.items()},
            "config": asdict(config),
        })
    try:
        if args.replay:
            asyncio.run(replay(config, args.replay, journal))
        else:
            asyncio.run(live(config, args.minutes, args.record, journal, args.dashboard_file, warm))
    except KeyboardInterrupt:
        logger.info("Stopped")
    finally:
        if journal is not None:
            journal.close()
            logger.info(f"Journal in {journal.folder}")


async def live(config: Config, minutes: float, record: Path | None, journal: Journal | None, dashboard_file: Path | None = None,
               warm: dict[str, list[tuple[int, float]]] | None = None) -> None:
    manager = SessionManager()
    session = await manager.getSession()
    recorder = Recorder(record) if record is not None else None
    tasks: list[asyncio.Task] = []
    engine = None
    try:
        engine, feeds = await setup(session, config, journal)
        now = time.time_ns()
        for ticker, samples in (warm or {}).items():
            basis = engine.coins[ticker].basis
            basis.restore(samples, now)
            if basis.samples:
                ready = "warm" if basis.value is not None else f"warm in {basis.left(now) / 60e9:.0f} min"
                logger.info(f"{ticker}: basis from {len(basis.samples)} s of samples of the run before, {ready}")
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
                    table = dashboard(engine, f"Paper XEMM, {time.monotonic() - started:.0f} s")
                    view.update(table)
                    if dashboard_file is not None:
                        write_file(table, dashboard_file)
                    for sink in (recorder, journal):
                        if sink is not None:
                            sink.flush()
                if beats % round(BASIS_SAVE / HEARTBEAT) == 0:
                    _save_basis(engine)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if recorder is not None:
            recorder.close()
        if engine is not None:
            _save_basis(engine)
        await manager.closeSession()


def _save_basis(engine: Engine) -> None:
    for ticker, state in engine.coins.items():
        save_samples(state.basis, ticker)


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
