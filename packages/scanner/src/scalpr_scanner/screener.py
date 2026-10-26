import argparse
import asyncio
from datetime import UTC, date, datetime

import numpy as np
from pandas import DataFrame, concat
from rich.table import Table
from scalpr_collector.exchanges import bybit, htx
from scalpr_collector.http_client import SessionManager
from scalpr_collector.storage import write_parquet
from scalpr_core.logger import console, logger
from scalpr_core.paths import DATA_DIR

from scalpr_scanner.history import last_complete_day
from scalpr_scanner.spread import FEES
from scalpr_scanner.toxicity import TOXICITY_WINDOWS, toxicity

SCREENS_DIR = DATA_DIR / "screener"
# same ticker on both exchanges but prices this far apart: another token or another multiplier (1000PEPE)
MAX_PRICE_GAP = 5.0
# at most this many recent trade requests to htx at once
TRADES_REQUESTS = 5


# uv run --package scalpr-scanner screener --samples 6 --interval 10
def main() -> None:
    parser = argparse.ArgumentParser(description="Rank common Bybit / HTX USDT perps for market making on HTX with a taker hedge on Bybit")
    parser.add_argument("--samples", type=int, default=6, help="book snapshots to take, spreads are their median (default 6)")
    parser.add_argument("--interval", type=float, default=10, help="seconds between snapshots (default 10)")
    parser.add_argument("--min-turnover", type=float, default=10_000, help="min 24h HTX turnover in USDT (default 10000)")
    parser.add_argument("--min-hedge-turnover", type=float, default=1_000_000, help="min 24h Bybit turnover in USDT, the hedge has to fill (default 1000000)")
    parser.add_argument("--trades", type=int, default=20, help="check the recent HTX tape of this many best candidates (default 20)")
    parser.add_argument("--top", type=int, default=30, help="rows in the table (default 30)")
    parser.add_argument("--tradfi", action="store_true", help="keep tokenized stocks, indices and commodities (they trade on market hours)")
    parser.add_argument("--latency", choices=TOXICITY_WINDOWS, default="200ms",
                        help="toxicity window taken off the room: about how long a quote change and a hedge take (default 200ms)")
    parser.add_argument("--toxicity-day", type=date.fromisoformat, default=last_complete_day(),
                        help="UTC day whose trade archives measure toxicity (default: the day before yesterday)")
    args = parser.parse_args()

    screen = asyncio.run(run(args.samples, args.interval, args.min_turnover, args.min_hedge_turnover, args.trades, args.tradfi, args.toxicity_day))
    screen = rerank(screen, args.latency)
    print_screen(screen, args.top, args.latency, args.toxicity_day)
    path = SCREENS_DIR / f"{datetime.now(UTC):%Y-%m-%d_%H%M}.parquet"
    write_parquet(screen, path)
    logger.info(f"Screen saved to {path}")


async def run(samples: int, interval: float, min_turnover: float, min_hedge_turnover: float, tape_checks: int, tradfi: bool,
              toxicity_day: date) -> DataFrame:
    manager = SessionManager()
    session = await manager.getSession()
    try:
        snapshots = []
        for sample in range(samples):
            if sample:
                await asyncio.sleep(interval)
            snapshots.append(await asyncio.gather(bybit.get_tickers(session), htx.get_tickers(session)))
            logger.info(f"Snapshot {sample + 1}/{samples}")
        screen = rank(snapshots, min_turnover, min_hedge_turnover, tradfi)

        candidates = screen.index[screen["candidate"]][:tape_checks]
        limit = asyncio.Semaphore(TRADES_REQUESTS)

        # the live htx tape for its profile, the archived tapes of both exchanges for toxicity
        async def check(ticker: str) -> dict:
            async with limit:
                stats = tape_profile(await htx.get_recent_trades(session, ticker))
                try:
                    htx_trades = await htx.get_trades_history(session, ticker, toxicity_day)
                    bybit_trades = await bybit.get_trades_history(session, ticker, toxicity_day)
                except Exception as error:  # noqa: BLE001 - a new listing has no archives yet, the coin stays unchecked
                    logger.warning(f"{ticker}: no toxicity for {toxicity_day}, {error!r}")
                    return stats
                return {**stats, **toxicity(htx_trades, bybit_trades)}

        checks = await asyncio.gather(*(check(ticker) for ticker in candidates))
    finally:
        await manager.closeSession()
    return screen.join(DataFrame(checks, index=candidates))


# RERANK: checked candidates by what is left after toxicity, the unchecked ones keep the order of their ceiling below
# net room % = room - toxicity at `window`: edge left after costs and after the bybit move a quote meets on average
def rerank(screen: DataFrame, window: str) -> DataFrame:
    column = f"toxicity {window} %"
    if column not in screen:
        screen[column] = np.nan
    screen["net room %"] = screen["room %"] - screen[column]
    screen["net ceiling, USDT/day"] = (screen["htx 24h, USDT"] * screen["net room %"] / 100).clip(lower=0)
    screen["checked"] = screen[column].notna()
    return screen.sort_values(["candidate", "checked", "net ceiling, USDT/day", "ceiling, USDT/day"], ascending=False)


# RANK: median over snapshots of both books for every common perp
# room % = htx half spread - costs of one fill (htx maker fee + bybit taker fee + bybit half spread):
#   how much edge is left when our quote sits at the edge of the htx book; above 0 a quote inside the htx spread
#   can beat the costs
# ceiling = 24h htx turnover x room: what a day would bring if every htx taker traded with us at the full room,
#   an upper bound to sort by, not a forecast
def rank(snapshots: list[tuple[DataFrame, DataFrame]], min_turnover: float, min_hedge_turnover: float, tradfi: bool) -> DataFrame:
    common = snapshots[-1][0].index.intersection(snapshots[-1][1].index)
    rows = []
    for bybit_tickers, htx_tickers in snapshots:
        b, h = bybit_tickers.reindex(common), htx_tickers.reindex(common)
        b_mid, h_mid = (b["bid"] + b["ask"]) / 2, (h["bid"] + h["ask"]) / 2
        rows.append(DataFrame({
            "htx half %": (h["ask"] - h["bid"]) / h_mid / 2 * 100,
            "bybit half %": (b["ask"] - b["bid"]) / b_mid / 2 * 100,
            "deviation %": (h_mid / b_mid - 1) * 100,
            # what sits at the htx best prices, the thinner side
            "htx top, USDT": np.minimum(h["bid"] * h["bid_size"], h["ask"] * h["ask_size"]),
            # how many ticks wide the htx spread is: 1 tick leaves no room to step in front
            "htx spread, ticks": (h["ask"] - h["bid"]) / h["tick"],
        }))
    screen = concat(rows).groupby(level=0).median()
    last_bybit, last_htx = snapshots[-1][0].reindex(common), snapshots[-1][1].reindex(common)
    screen["costs %"] = FEES["htx"]["maker"] + FEES["bybit"]["taker"] + screen["bybit half %"]
    screen["room %"] = screen["htx half %"] - screen["costs %"]
    screen["htx 24h, USDT"] = last_htx["turnover"]
    screen["htx 24h trades"] = last_htx["trades"]
    screen["bybit 24h, USDT"] = last_bybit["turnover"]
    screen["ceiling, USDT/day"] = (screen["htx 24h, USDT"] * screen["room %"] / 100).clip(lower=0)

    screen["mismatch"] = screen["deviation %"].abs() > MAX_PRICE_GAP
    # stocks and indices follow market hours: off hours their books widen and the room is not there on weekdays
    screen["tradfi"] = last_htx["tradfi"].astype(bool)
    screen["candidate"] = (
        ~screen["mismatch"] & (tradfi | ~screen["tradfi"])
        & (screen["room %"] > 0) & (screen["htx 24h, USDT"] >= min_turnover) & (screen["htx spread, ticks"] > 1)
        # a thin bybit book can't take the hedge: some tradfi perps there trade a few hundred USDT a day
        & (screen["bybit 24h, USDT"] >= min_hedge_turnover)
    )
    screen = screen.sort_values(["candidate", "ceiling, USDT/day"], ascending=False).rename_axis("ticker")
    logger.info(f"{len(common)} common perps: {screen['tradfi'].sum()} tradfi, {screen['mismatch'].sum()} with a price mismatch, {screen['candidate'].sum()} candidates")
    return screen


# TAPE PROFILE: what the recent htx flow looks like
# same size: share of trades with the single most common size, same gap: share of pauses between trades of the most
# common length (whole seconds); high shares smell of a bot trading with itself on a timer
def tape_profile(tape: DataFrame) -> dict:
    gaps = np.diff(tape.index.as_unit("ms").asi8) / 1000
    # several fills of one taker order share a timestamp
    gaps = np.round(gaps[gaps > 0])
    if len(gaps) < 2:
        return dict.fromkeys(("tape hours", "trades/hour", "USDT/hour", "median trade, USDT", "same size", "same gap"), np.nan)
    hours = (tape.index[-1] - tape.index[0]).total_seconds() / 3600
    usdt = tape["price"] * tape["size"]
    return {
        "tape hours": hours,
        "trades/hour": len(tape) / hours if hours else np.nan,
        "USDT/hour": usdt.sum() / hours if hours else np.nan,
        "median trade, USDT": usdt.median(),
        "same size": tape["size"].value_counts(normalize=True).iloc[0],
        "same gap": np.unique(gaps, return_counts=True)[1].max() / len(gaps),
    }


def print_screen(screen: DataFrame, top_rows: int, window: str, toxicity_day: date) -> None:
    candidates = screen[screen["candidate"]]
    table = Table(title=f"XEMM candidates: quote HTX, hedge on Bybit ({len(candidates)} of {len(screen)} common perps), "
                        f"by net room after {window} toxicity of {toxicity_day}")
    columns = ("ticker", "htx half %", "costs %", "room %", "toxicity 50ms", "toxicity 200ms", "net room %", "spread ticks",
               "htx 24h, USDT", "net ceiling/day", "ceiling/day", "deviation %", "trades/hour", "median trade", "same size", "same gap")
    for column in columns:
        table.add_column(column, justify="right")
    for ticker, row in candidates.head(top_rows).iterrows():
        table.add_row(
            ticker,
            f"{row['htx half %']:.3f}",
            f"{row['costs %']:.3f}",
            f"{row['room %']:+.3f}",
            _number(row.get("toxicity 50ms %"), "{:+.3f}"),
            _number(row.get("toxicity 200ms %"), "{:+.3f}"),
            _number(row.get("net room %"), "{:+.3f}"),
            f"{row['htx spread, ticks']:.0f}",
            f"{row['htx 24h, USDT']:,.0f}",
            _number(row.get("net ceiling, USDT/day"), "{:,.0f}"),
            f"{row['ceiling, USDT/day']:,.0f}",
            f"{row['deviation %']:+.2f}",
            _number(row.get("trades/hour"), "{:.0f}"),
            _number(row.get("median trade, USDT"), "{:,.0f}"),
            _number(row.get("same size"), "{:.0%}"),
            _number(row.get("same gap"), "{:.0%}"),
        )
    console.print(table)


def _number(value: float | None, template: str) -> str:
    return "-" if value is None or np.isnan(value) else template.format(value)
