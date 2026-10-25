import argparse
import asyncio
import webbrowser
from datetime import date

from pandas import DataFrame, Timedelta, isna
from rich.table import Table
from scalpr_collector.exchanges import bybit, htx
from scalpr_collector.http_client import SessionManager
from scalpr_core.logger import console, logger
from scalpr_core.paths import DATA_DIR

from scalpr_scanner.chart import spread_chart
from scalpr_scanner.maker import FILL_RULES, LABELS, maker_trades
from scalpr_scanner.spread import (
    MODES,
    basis,
    basis_trades,
    crossing_cost,
    exchange_prices,
    round_trip_fees,
    spread_frame,
    windows,
)

CHARTS_DIR = DATA_DIR / "charts"
EXCHANGES = {"bybit": bybit, "htx": htx}


# uv run --package scalpr-scanner scanner ACEUSDT 2026-10-20 --htx maker
def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit vs HTX perp arbitrage spread for one UTC day")
    parser.add_argument("ticker", help="bybit-style ticker, e.g. ACEUSDT")
    parser.add_argument("day", type=date.fromisoformat, help="UTC day, YYYY-MM-DD")
    parser.add_argument("--bybit", choices=MODES, default="taker", help="how orders are executed on Bybit (default taker)")
    parser.add_argument("--htx", choices=MODES, default="taker", help="how orders are executed on HTX (default taker)")
    parser.add_argument("--notional", type=float, default=100, help="order size in USDT, taker legs use VWAP prices for it (default 100)")
    parser.add_argument("--depth", type=int, default=20, help="book levels to load (default 20)")
    parser.add_argument("--freq", default="1s", help="time grid step (default 1s)")
    parser.add_argument("--fees", type=float, help="round trip fees in %% for the entry level and taker trades, default: from the fee table and the modes")
    parser.add_argument("--basis-window", default="1h", help="rolling window of the basis (default 1h)")
    parser.add_argument("--entry", type=float, help="enter when the deviation is this %% away from the basis, default: fees + crossing cost of taker legs")
    parser.add_argument("--fill", choices=FILL_RULES, default="through", help="maker order is filled by a trade past its price (through, default) or at it (touch)")
    parser.add_argument("--exit-timeout", default="5min", help="maker exit order not filled in this time is closed with a taker order (default 5min)")
    parser.add_argument("--latency", default="0ms", help="delay from a maker fill to the hedge order (default 0ms)")
    parser.add_argument("--top", type=int, default=10, help="rows in the windows and trades lists (default 10)")
    parser.add_argument("--no-open", action="store_true", help="only save the chart, don't open it in the browser")
    args = parser.parse_args()

    modes = {"bybit": args.bybit, "htx": args.htx}
    makers = [exchange for exchange, mode in modes.items() if mode == "maker"]
    if len(makers) > 1:
        parser.error("maker on both exchanges leaves legs unhedged: quote on one exchange, hedge with taker on the other")
    maker = makers[0] if makers else None

    fees = args.fees if args.fees is not None else round_trip_fees(modes)
    bybit_book, htx_book, tape = asyncio.run(load_history(args.ticker, args.day, args.depth, maker))
    native, grid = exchange_prices(bybit_book, htx_book, args.day, args.notional, args.freq)
    spreads = spread_frame(grid)
    # taker legs pay their exchange's spread on entry and exit, a maker leg doesn't cross it
    cost = sum(crossing_cost(grid[exchange]) for exchange, mode in modes.items() if mode == "taker")
    # the deviation has to move at least as much as a round trip costs
    entry = args.entry if args.entry is not None else fees + cost
    logger.info(f"Bybit {args.bybit}, HTX {args.htx}: round trip fees {fees:.3f}%, crossing cost {cost:.3f}%, entry {entry:.3f}% from basis")

    taker_fees = round_trip_fees(dict.fromkeys(modes, "taker"))
    print_windows(spreads, windows(spreads, taker_fees), taker_fees, args.top)
    basis_line = basis(spreads, args.basis_window)
    if maker is None:
        trades = basis_trades(spreads, basis_line, entry, fees)
        print_trades(trades, entry, args.basis_window, args.top)
    else:
        trades = maker_trades(spreads, basis_line, entry, native, tape, maker, args.fill, args.exit_timeout, args.latency)
        print_maker_trades(trades, entry, args.basis_window, maker, args.fill, args.top)
        trades = trades.dropna(subset=["entry"])

    title = f"{args.ticker} {args.day} UTC: HTX vs Bybit, Bybit {args.bybit} / HTX {args.htx}"
    path = CHARTS_DIR / f"{args.ticker}_{args.day}_{args.bybit}-{args.htx}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    spread_chart(spreads, title, basis_line, entry, trades).write_html(path)
    logger.info(f"Chart saved to {path}")
    if not args.no_open:
        webbrowser.open(path.as_uri())


# books of both exchanges and, with a maker leg, the trades tape of the maker exchange
async def load_history(ticker: str, day: date, depth: int, maker: str | None) -> tuple[DataFrame, DataFrame, DataFrame | None]:
    manager = SessionManager()
    session = await manager.getSession()
    try:
        # all at once: downloads overlap, each gets its own progress row
        jobs = [bybit.get_dom_history(session, ticker, day, depth), htx.get_dom_history(session, ticker, day, depth)]
        if maker is not None:
            jobs.append(EXCHANGES[maker].get_trades_history(session, ticker, day))
        bybit_book, htx_book, *tape = await asyncio.gather(*jobs)
        return bybit_book, htx_book, tape[0] if tape else None
    finally:
        await manager.closeSession()


def print_windows(spreads: DataFrame, found: DataFrame, fees: float, top_rows: int) -> None:
    spread = spreads["spread"]
    summary = Table(title=f"Taker on both, buy on the cheaper exchange, sell on the other: windows above {fees:.3f}% fees")
    columns = ("median %", "max %", "time above fees", "windows", "buy on Bybit / HTX", "aligned after, median", "exit after, median", "no exit", "no data")
    for column in columns:
        summary.add_column(column, justify="right")
    summary.add_row(
        f"{spread.median():.3f}",
        f"{spread.max():.3f}",
        f"{(spread > fees).mean():.2%}",
        str(len(found)),
        f"{(found['buy on'] == 'Bybit').sum()} / {(found['buy on'] == 'HTX').sum()}",
        _duration(found["aligned after"].median()),
        _duration(found["exit after"].median()),
        f"{found['exit after'].isna().mean():.0%}" if len(found) else "-",
        f"{spread.isna().mean():.2%}",
    )
    console.print(summary)
    if found.empty:
        return

    top = Table(title=f"Top {top_rows} windows by peak spread")
    for column in found.columns:
        top.add_column(column, justify="right")
    for _, row in found.nlargest(top_rows, "peak %").iterrows():
        top.add_row(
            row["start"].strftime("%H:%M:%S"),
            row["buy on"],
            f"{row['entry %']:.3f}",
            f"{row['peak %']:.3f}",
            _duration(row["above fees"]),
            _duration(row["aligned after"]),
            _duration(row["exit after"]),
        )
    console.print(top)


def print_trades(trades: DataFrame, entry: float, window: str, top_rows: int) -> None:
    closed = trades.dropna(subset=["exit"])
    pnl = closed["pnl %"]
    summary = Table(title=f"Basis reversion, taker on both: enter {entry:.3f}% away from the {window} basis, exit back at it")
    columns = ("trades", "buy on Bybit / HTX", "back to basis", "held, median", "win rate", "pnl median %", "pnl total %", "best %", "worst %")
    for column in columns:
        summary.add_column(column, justify="right")
    summary.add_row(
        str(len(trades)),
        f"{(trades['buy on'] == 'Bybit').sum()} / {(trades['buy on'] == 'HTX').sum()}",
        f"{len(closed)} / {len(trades)}",
        _duration(closed["held"].median()),
        f"{(pnl > 0).mean():.0%}" if len(closed) else "-",
        _percent(pnl.median()),
        _percent(pnl.sum()),
        _percent(pnl.max()),
        _percent(pnl.min()),
    )
    console.print(summary)
    if closed.empty:
        return

    top = Table(title=f"Top {top_rows} trades by pnl")
    for column in ("entry", "buy on", "excess %", "entry %", "held", "closing %", "pnl %"):
        top.add_column(column, justify="right")
    for _, row in closed.nlargest(top_rows, "pnl %").iterrows():
        top.add_row(
            row["entry"].strftime("%H:%M:%S"),
            row["buy on"],
            _percent(row["excess %"]),
            _percent(row["entry %"]),
            _duration(row["held"]),
            _percent(row["closing %"]),
            _percent(row["pnl %"]),
        )
    console.print(top)


def print_maker_trades(trades: DataFrame, entry: float, window: str, maker: str, rule: str, top_rows: int) -> None:
    closed = trades[trades["status"] == "closed"]
    pnl = closed["pnl %"]
    filled = trades["entry"].notna().sum()
    summary = Table(title=f"Basis reversion, maker on {LABELS[maker]} ({rule} fills) + taker hedge: enter {entry:.3f}% away from the {window} basis")
    columns = ("signals", "filled", "fill wait, median", "closed", "exit maker / taker", "held, median", "win rate", "pnl median %", "pnl total %", "best %", "worst %")
    for column in columns:
        summary.add_column(column, justify="right")
    summary.add_row(
        str(len(trades)),
        f"{filled} ({filled / len(trades):.0%})" if len(trades) else "0",
        _duration(trades["wait"].median()),
        str(len(closed)),
        f"{(closed['exit by'] == 'maker').sum()} / {(closed['exit by'] == 'taker').sum()}",
        _duration(closed["held"].median()),
        f"{(pnl > 0).mean():.0%}" if len(closed) else "-",
        _percent(pnl.median()),
        _percent(pnl.sum()),
        _percent(pnl.max()),
        _percent(pnl.min()),
    )
    console.print(summary)
    if closed.empty:
        return

    top = Table(title=f"Top {top_rows} trades by pnl")
    for column in ("signal", "buy on", "excess %", "wait", "entry %", "held", "exit by", "closing %", "pnl %"):
        top.add_column(column, justify="right")
    for _, row in closed.nlargest(top_rows, "pnl %").iterrows():
        top.add_row(
            row["signal"].strftime("%H:%M:%S"),
            row["buy on"],
            _percent(row["excess %"]),
            _duration(row["wait"]),
            _percent(row["entry %"]),
            _duration(row["held"]),
            row["exit by"],
            _percent(row["closing %"]),
            _percent(row["pnl %"]),
        )
    console.print(top)


# "00:03:42", "-" when it didn't happen before the data ends
def _duration(value: Timedelta) -> str:
    if isna(value):
        return "-"
    # medians can land between grid points: 00:56:30.500000 -> 00:56:30.5
    text = str(value).removeprefix("0 days ")
    return text.rstrip("0") if "." in text else text


def _percent(value: float) -> str:
    return "-" if isna(value) else f"{value:+.3f}"
