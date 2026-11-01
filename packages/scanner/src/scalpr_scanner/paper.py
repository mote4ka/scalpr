import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from pandas import (
    DataFrame,
    Timedelta,
    Timestamp,
    concat,
    isna,
    merge_asof,
    notna,
    to_datetime,
)
from rich.table import Table
from scalpr_collector.exchanges import bybit, htx
from scalpr_collector.http_client import SessionManager
from scalpr_core.logger import console, logger
from scalpr_trader.config import Config, from_dict
from scalpr_trader.journal import PAPER_DIR

from scalpr_scanner.history import last_complete_day
from scalpr_scanner.spread import basis, exchange_prices, spread_frame
from scalpr_scanner.stream_data import stream_frames
from scalpr_scanner.xemm import Xemm, xemm

DEPTH = 20


# a journal of the paper trader (data/paper/<start>/)
@dataclass
class Run:
    folder: Path
    meta: dict
    config: Config
    fills: DataFrame  # one row per hedged fill, its markout joined
    snapshots: DataFrame  # every coin every 10 s

    # from the first snapshot to the last snapshot or fill, a fill can come after the last snapshot
    @property
    def window(self) -> tuple[Timestamp, Timestamp]:
        times = self.snapshots["t"] if self.fills.empty else concat([self.snapshots["t"], self.fills["ts"]])
        return times.min().floor("s"), times.max().ceil("s")


# uv run --package scalpr-scanner paper-report                      (the latest journal)
# uv run --package scalpr-scanner paper-report data/paper/2026-10-26_150707 --source archive
def main() -> None:
    parser = argparse.ArgumentParser(description="Daily report of a paper trader journal and its reconciliation with the backtest on the same data")
    parser.add_argument("journal", type=Path, nargs="?", help="data/paper/<start> folder (default: the latest)")
    parser.add_argument("--source", default="auto",
                        help="backtest data: a stream recording folder, 'archive' (exchange daily archives) or 'auto' (default): "
                             "the recording the run replayed or recorded, else the archives")
    parser.add_argument("--tolerance", default="2s", help="paper and backtest fills this close in time, same side and price, are the same fill (default 2s)")
    args = parser.parse_args()

    folder = args.journal or max((path for path in PAPER_DIR.iterdir() if (path / "run.json").exists()), default=None)
    if folder is None:
        raise SystemExit(f"no journal with run.json in {PAPER_DIR}")
    run = load_run(folder)
    print_day_report(run)

    source = args.source
    if source == "auto":
        source = run.meta.get("replay") or run.meta.get("record") or "archive"
    results = asyncio.run(backtest(run, source))
    if results:
        print_reconciliation(run, results, Timedelta(args.tolerance), source)


def load_run(folder: Path) -> Run:
    meta = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    fills, markouts, snapshots = (_lines(folder / f"{name}.jsonl") for name in ("fills", "markouts", "snapshots"))
    if not fills.empty:
        fills = fills.drop(columns="markout").merge(markouts[["id", "markout"]], on="id", how="left") if not markouts.empty else fills
        fills["ts"] = to_datetime(fills["ts"], unit="ns", utc=True)
    snapshots["t"] = to_datetime(snapshots["t"], unit="ns", utc=True)
    return Run(folder, meta, from_dict(meta["config"]), fills, snapshots)


# DAY REPORT: per UTC day and coin; pnl is marked at the mids, the change over the day
def day_report(run: Run) -> DataFrame:
    snapshots = run.snapshots.assign(day=run.snapshots["t"].dt.date)
    fills = run.fills.assign(day=run.fills["ts"].dt.date, usdt=run.fills["price"] * run.fills["qty"]) if not run.fills.empty else None
    rows = []
    for (day, ticker), part in snapshots.groupby(["day", "ticker"], sort=True):
        earlier = snapshots[(snapshots["ticker"] == ticker) & (snapshots["day"] < day)]
        own = fills[(fills["day"] == day) & (fills["ticker"] == ticker)] if fills is not None else DataFrame()
        last = part.iloc[-1]
        rows.append({
            "day": day, "ticker": ticker,
            "hours": (part["t"].max() - part["t"].min()).total_seconds() / 3600,
            "quoting": (part["status"] == "quoting").mean(),
            "stale": (part["status"] == "stale").mean(),
            "buys": int(own["buys"].sum()) if len(own) else 0,
            "sells": int((~own["buys"]).sum()) if len(own) else 0,
            "volume": own["usdt"].sum() if len(own) else 0.0,
            "fees": own["fee"].sum() if len(own) else 0.0,
            "pnl": last["pnl"] - (earlier.iloc[-1]["pnl"] if len(earlier) else 0.0),
            "markout": own["markout"].median() if len(own) else np.nan,
            "ahead": own["ahead"].mean() if len(own) else np.nan,
            "inventory": last["inventory"] * last["fair"] if notna(last["fair"]) else 0.0,
        })
    return DataFrame(rows)


def print_day_report(run: Run) -> None:
    report = day_report(run)
    start, end = run.window
    table = Table(title=f"Paper XEMM {run.folder.name} ({run.meta['mode']}): {start:%Y-%m-%d %H:%M} .. {end:%H:%M} UTC, pnl in USDT marked at mids")
    for column in ("day", "ticker", "edge %", "hours", "quoting", "stale", "fills buy / sell", "volume", "fees", "pnl", "markout %", "ahead", "inventory"):
        table.add_column(column, justify="right")
    edges = {coin.ticker: coin.edge for coin in run.config.coins}
    for _, row in report.iterrows():
        table.add_row(
            str(row["day"]), row["ticker"], f"{edges[row['ticker']]:g}", f"{row['hours']:.2f}", f"{row['quoting']:.0%}", f"{row['stale']:.0%}",
            f"{row['buys']} / {row['sells']}", f"{row['volume']:,.0f}", f"{row['fees']:.2f}", f"{row['pnl']:+.2f}",
            _number(row["markout"], "{:+.3f}"), _number(row["ahead"], "{:.0%}"), f"{row['inventory']:+.0f}",
        )
    table.add_section()
    table.add_row("total", "", "", "", "", "", f"{report['buys'].sum()} / {report['sells'].sum()}", f"{report['volume'].sum():,.0f}",
                  f"{report['fees'].sum():.2f}", f"{report['pnl'].sum():+.2f}", "", "", "")
    console.print(table)


# BACKTEST of the same coins over the run's window with the run's config, on a recording or on the daily archives
async def backtest(run: Run, source: str) -> dict[str, Xemm]:
    config, (start, end) = run.config, run.window
    # a live run that took over the basis samples of the run before quotes from the start, so the backtest basis gets
    # the window before the start too; the strategy itself still runs from the start
    history = start - Timedelta(config.basis_window, unit="ns") if any(run.meta.get("warm", {}).values()) else start
    tickers = [coin.ticker for coin in config.coins]
    manager = SessionManager()
    session = await manager.getSession()
    try:
        if source == "archive":
            days = [history.date() + timedelta(days=offset) for offset in range((end - Timedelta(1)).date().toordinal() - history.date().toordinal() + 1)]
            if days[-1] > last_complete_day():
                logger.warning(f"archives for {days[-1]} are complete only after {days[-1] + timedelta(days=2)} UTC: rerun then, or pass a recording")
                return {}
            data = {ticker: await _archive_frames(session, ticker, days) for ticker in tickers}
        else:
            sizes = {ticker: spec["size"] for ticker, spec in (await htx.get_contract_specs(session)).items() if ticker in tickers}
            data = stream_frames(Path(source), tickers, sizes, DEPTH)
    finally:
        await manager.closeSession()

    results = {}
    for coin in config.coins:
        bybit_book, htx_book, htx_trades = data[coin.ticker]
        native, grid = exchange_prices(bybit_book, htx_book, None, coin.notional, "1s", history, end)
        spreads = spread_frame(grid)
        line = basis(spreads, f"{config.basis_window}ns", f"{config.warmup}ns")
        spreads, line, grid = spreads.loc[start:], line.loc[start:], {exchange: prices.loc[start:] for exchange, prices in grid.items()}
        results[coin.ticker] = xemm(spreads, line, native, grid, htx_trades, config.maker, coin.notional, coin.edge, coin.max_position,
                                    config.fill, f"{config.quote_latency}ns", "tick", f"{config.hedge_latency}ns",
                                    config.anchor, coin.vol_edge, coin.skew, f"{config.vol_window}ns")
    return results


async def _archive_frames(session, ticker: str, days: list[date]) -> tuple[DataFrame, DataFrame, DataFrame]:
    parts = [(await bybit.get_dom_history(session, ticker, day, DEPTH), await htx.get_dom_history(session, ticker, day, DEPTH),
              await htx.get_trades_history(session, ticker, day)) for day in days]
    return tuple(concat([part[i] for part in parts]).sort_index() for i in range(3))


# RECONCILIATION: the paper fills against the backtest fills of the same window; a fill is matched when the other side
# has one on the same side at the same price within `tolerance`
def reconcile(run: Run, results: dict[str, Xemm], tolerance: Timedelta) -> DataFrame:
    rows = []
    last_pnl = run.snapshots.sort_values("t").groupby("ticker")["pnl"].last()
    ticks = {coin.ticker: coin for coin in run.config.coins}
    for ticker, result in results.items():
        paper = run.fills[run.fills["ticker"] == ticker] if not run.fills.empty else DataFrame(columns=["ts", "buys", "price", "qty", "fee", "markout"])
        paper = paper.assign(usdt=paper["price"] * paper["qty"]).sort_values("ts")
        model = result.fills.assign(buys=result.fills["side"] == "buy").sort_values("ts")
        rows.append({
            "ticker": ticker, "edge": ticks[ticker].edge,
            "paper buys": int(paper["buys"].sum()), "paper sells": int((~paper["buys"].astype(bool)).sum()),
            "model buys": int(model["buys"].sum()), "model sells": int((~model["buys"]).sum()),
            "paper matched": _matched(paper, model, tolerance), "model matched": _matched(model, paper, tolerance),
            "paper volume": paper["usdt"].sum(), "model volume": model["usdt"].sum(),
            "paper fees": paper["fee"].sum(), "model fees": result.fees,
            "paper pnl": last_pnl.get(ticker, np.nan), "model pnl": result.curve["pnl"].dropna().iloc[-1] if result.curve["pnl"].notna().any() else np.nan,
            "paper markout": paper["markout"].median(), "model markout": model["markout 1m %"].median(),
        })
    return DataFrame(rows)


def _matched(fills: DataFrame, others: DataFrame, tolerance: Timedelta) -> float:
    if fills.empty:
        return np.nan
    if others.empty:
        return 0.0
    left = fills[["ts", "buys", "price"]].astype({"buys": bool}).sort_values("ts")
    right = others[["ts", "buys", "price"]].astype({"buys": bool}).rename(columns={"price": "other"}).sort_values("ts")
    pairs = merge_asof(left, right, on="ts", by="buys", tolerance=tolerance, direction="nearest")
    return float(np.isclose(pairs["price"], pairs["other"], rtol=1e-9).mean())


def print_reconciliation(run: Run, results: dict[str, Xemm], tolerance: Timedelta, source: str) -> None:
    rows = reconcile(run, results, tolerance)
    table = Table(title=f"Paper vs backtest on {source}, same window and config; matched: same side and price within {tolerance.total_seconds():g} s")
    for column in ("ticker", "edge %", "fills paper", "fills model", "paper matched", "model matched", "volume paper / model",
                   "fees paper / model", "pnl paper / model", "markout paper / model"):
        table.add_column(column, justify="right")
    for _, row in rows.iterrows():
        table.add_row(
            row["ticker"], f"{row['edge']:g}",
            f"{row['paper buys']} / {row['paper sells']}", f"{row['model buys']} / {row['model sells']}",
            _number(row["paper matched"], "{:.0%}"), _number(row["model matched"], "{:.0%}"),
            f"{row['paper volume']:,.0f} / {row['model volume']:,.0f}",
            f"{row['paper fees']:.2f} / {row['model fees']:.2f}",
            f"{_number(row['paper pnl'], '{:+.2f}')} / {_number(row['model pnl'], '{:+.2f}')}",
            f"{_number(row['paper markout'], '{:+.3f}')} / {_number(row['model markout'], '{:+.3f}')}",
        )
    table.add_section()
    table.add_row("total", "", f"{rows['paper buys'].sum()} / {rows['paper sells'].sum()}", f"{rows['model buys'].sum()} / {rows['model sells'].sum()}",
                  "", "", f"{rows['paper volume'].sum():,.0f} / {rows['model volume'].sum():,.0f}",
                  f"{rows['paper fees'].sum():.2f} / {rows['model fees'].sum():.2f}", f"{rows['paper pnl'].sum():+.2f} / {rows['model pnl'].sum():+.2f}", "")
    console.print(table)


def _lines(path: Path) -> DataFrame:
    if not path.exists():
        return DataFrame()
    with open(path, encoding="utf-8") as file:
        return DataFrame([json.loads(line) for line in file if line.strip()])


def _number(value: float, template: str) -> str:
    return "-" if isna(value) else template.format(value)
