import argparse
import asyncio
from datetime import UTC, date, datetime, timedelta
from itertools import product
from pathlib import Path

from pandas import DataFrame, read_parquet
from rich.table import Table
from scalpr_collector.http_client import SessionManager
from scalpr_collector.storage import write_parquet
from scalpr_core.logger import console, logger
from scalpr_core.paths import DATA_DIR

from scalpr_scanner.history import load_day
from scalpr_scanner.maker import FILL_RULES
from scalpr_scanner.screener import SCREENS_DIR
from scalpr_scanner.spread import basis, exchange_prices, spread_frame
from scalpr_scanner.xemm import Xemm, xemm

BACKTESTS_DIR = DATA_DIR / "backtests"
# htx publishes day D after 16:00 UTC of D+1: the day before yesterday is the last one surely complete
PUBLISH_LAG = timedelta(days=2)


# uv run --package scalpr-scanner backtest --top 10 --days 3 --edges 0.1,0.15,0.2,0.25
def main() -> None:
    parser = argparse.ArgumentParser(description="XEMM on HTX with a Bybit hedge over several coins, days and edges")
    parser.add_argument("--tickers", help="comma separated bybit-style tickers, default: top candidates of the last screen")
    parser.add_argument("--screen", type=Path, help="screen parquet to take candidates from (default: the latest in data/screener)")
    parser.add_argument("--top", type=int, default=10, help="candidates to take from the screen (default 10)")
    parser.add_argument("--days", type=int, default=3, help="UTC days back from --last-day (default 3)")
    parser.add_argument("--last-day", type=date.fromisoformat, default=datetime.now(UTC).date() - PUBLISH_LAG,
                        help="last UTC day, YYYY-MM-DD (default: the day before yesterday)")
    parser.add_argument("--edges", default="0.1,0.15,0.2,0.25", help="quote distances from fair in %%, comma separated (default 0.1,0.15,0.2,0.25)")
    parser.add_argument("--notional", type=float, default=100, help="quote size in USDT (default 100)")
    parser.add_argument("--max-position", type=float, default=300, help="a side stops quoting at this inventory in USDT (default 300)")
    parser.add_argument("--fill", choices=FILL_RULES, default="through", help="fill rule when our quote is level with the book (default through)")
    parser.add_argument("--latency", default="0ms", help="delay from a fill to the hedge (default 0ms)")
    parser.add_argument("--depth", type=int, default=20, help="book levels to load (default 20)")
    parser.add_argument("--freq", default="1s", help="quote refresh step (default 1s)")
    parser.add_argument("--basis-window", default="1h", help="rolling window of the basis (default 1h)")
    parser.add_argument("--train-until", type=date.fromisoformat,
                        help="out of sample check: pick each coin's edge on the days up to this one, trade it on the later days")
    parser.add_argument("--results", type=Path, help="print a saved results parquet instead of simulating again")
    args = parser.parse_args()

    if args.results:
        results = read_parquet(args.results)
    else:
        tickers = args.tickers.split(",") if args.tickers else top_candidates(args.screen, args.top)
        days = [args.last_day - timedelta(days=back) for back in reversed(range(args.days))]
        edges = [float(edge) for edge in args.edges.split(",")]
        logger.info(f"{len(tickers)} coins x {len(days)} days ({days[0]} .. {days[-1]}) x edges {edges}")
        results = asyncio.run(run(tickers, days, edges, args))
        path = BACKTESTS_DIR / f"{datetime.now(UTC):%Y-%m-%d_%H%M}.parquet"
        write_parquet(results, path)
        logger.info(f"Results saved to {path}")
    print_results(results, results["day"].nunique())
    if args.train_until:
        print_out_of_sample(results, args.train_until)


def top_candidates(screen_path: Path | None, top: int) -> list[str]:
    path = screen_path or max(SCREENS_DIR.glob("*.parquet"), default=None)
    if path is None:
        raise SystemExit("no screen yet: run `screener` first or pass --tickers")
    screen = read_parquet(path)
    logger.info(f"Top {top} candidates of {path.name}")
    return list(screen.index[screen["candidate"]][:top])


# one coin-day at a time: a day of books takes hundreds of MB, they are dropped before the next one
async def run(tickers: list[str], days: list[date], edges: list[float], args: argparse.Namespace) -> DataFrame:
    manager = SessionManager()
    session = await manager.getSession()
    rows = []
    try:
        for n, (ticker, day) in enumerate(product(tickers, days), 1):
            logger.info(f"[{n}/{len(tickers) * len(days)}] {ticker} {day}")
            try:
                bybit_book, htx_book, tape = await load_day(session, ticker, day, args.depth, "htx")
            except Exception as error:  # noqa: BLE001 - a missing archive or a delisted coin must not stop the batch
                logger.warning(f"{ticker} {day}: skipped, {error!r}")
                rows.append({"ticker": ticker, "day": str(day), "error": repr(error)})
                continue
            native, grid = exchange_prices(bybit_book, htx_book, day, args.notional, args.freq)
            spreads = spread_frame(grid)
            basis_line = basis(spreads, args.basis_window)
            # share of the day both books are there (a partial htx day is shorter)
            coverage = (grid["bybit"]["mid"].notna() & grid["htx"]["mid"].notna()).mean()
            for edge in edges:
                result = xemm(spreads, basis_line, native, grid, tape, "htx", args.notional, edge, args.max_position, args.fill, args.latency)
                rows.append(summary(ticker, day, edge, coverage, result))
    finally:
        await manager.closeSession()
    return DataFrame(rows)


def summary(ticker: str, day: date, edge: float, coverage: float, result: Xemm) -> dict:
    fills, curve = result.fills, result.curve
    return {
        "ticker": ticker,
        "day": str(day),
        "edge": edge,
        "coverage": coverage,
        "fills": len(fills),
        "volume": fills["usdt"].sum(),
        "markout 1m %": fills["markout 1m %"].median(),
        "fees": result.fees,
        "pnl marked": curve["pnl"].iloc[-1],
        # every day ends flat: inventory closed with taker orders
        "pnl": result.closed_pnl,
        "max inventory": curve["inventory"].abs().max(),
    }


def print_results(results: DataFrame, days: int) -> None:
    done = results.dropna(subset=["pnl"]) if "pnl" in results else results.iloc[0:0]
    if done.empty:
        console.print("[red]nothing simulated[/]")
    else:
        per = done.groupby(["ticker", "edge"]).agg(
            days=("day", "nunique"), pnl=("pnl", "sum"), win_days=("pnl", lambda pnl: (pnl > 0).sum()),
            volume=("volume", "sum"), fills=("fills", "sum"), markout=("markout 1m %", "median"),
            max_inventory=("max inventory", "max"), coverage=("coverage", "mean"),
        )
        grid = per["pnl"].unstack("edge")
        # the edge with the most pnl for every coin, best coins first
        best = per.loc[per.groupby(level="ticker")["pnl"].idxmax()].reset_index(level="edge").sort_values("pnl", ascending=False)

        table = Table(title=f"XEMM backtest: P&L in USDT closed every day, sum over up to {days} days")
        for column in ("ticker", *(f"edge {edge:g}%" for edge in grid.columns), "best edge", "days", "win days", "pnl/day",
                       "volume/day", "fills/day", "markout 1m", "max inventory", "coverage"):
            table.add_column(column, justify="right")
        for ticker, row in best.iterrows():
            # iterrows makes the row float, counts go back to int
            days_done, win_days = int(row["days"]), int(row["win_days"])
            table.add_row(
                ticker,
                *(f"{pnl:+.2f}" for pnl in grid.loc[ticker]),
                f"{row['edge']:g}%",
                str(days_done),
                f"{win_days} / {days_done}",
                f"{row['pnl'] / row['days']:+.2f}",
                f"{row['volume'] / row['days']:,.0f}",
                f"{row['fills'] / row['days']:.0f}",
                f"{row['markout']:+.3f}",
                f"{row['max_inventory']:.0f}",
                f"{row['coverage']:.0%}",
            )
        console.print(table)
    if "error" in results:
        for _, row in results.dropna(subset=["error"]).iterrows():
            console.print(f"[yellow]skipped {row['ticker']} {row['day']}: {row['error']}[/]")


# OUT OF SAMPLE: each coin's edge is the best one on the train days (up to `train_until`), a coin is traded only
# if that edge made money there; the test days show what this choice would have earned, next to the best edge
# picked with hindsight on the test days themselves
def print_out_of_sample(results: DataFrame, train_until: date) -> None:
    done = results.dropna(subset=["pnl"]) if "pnl" in results else results.iloc[0:0]
    train, test = done[done["day"] <= str(train_until)], done[done["day"] > str(train_until)]
    if train.empty or test.empty:
        console.print(f"[red]no days on one side of {train_until}[/]")
        return
    train_pnl = train.groupby(["ticker", "edge"])["pnl"].sum()
    test_pnl = test.groupby(["ticker", "edge"])["pnl"].sum()
    train_days, test_days = train.groupby("ticker")["day"].nunique(), test.groupby("ticker")["day"].nunique()
    test_wins = test.groupby(["ticker", "edge"])["pnl"].agg(lambda pnl: (pnl > 0).sum())

    rows = []
    for ticker, (_, edge) in train_pnl.groupby(level="ticker").idxmax().items():
        if ticker not in test_days:
            continue
        hindsight = test_pnl.loc[ticker].idxmax()
        rows.append({
            "ticker": ticker,
            "edge": edge,
            "train/day": train_pnl[ticker, edge] / train_days[ticker],
            "traded": train_pnl[ticker, edge] > 0,
            "test/day": test_pnl.get((ticker, edge), float("nan")) / test_days[ticker],
            "test wins": f"{test_wins.get((ticker, edge), 0)} / {test_days[ticker]}",
            "hindsight edge": hindsight,
            "hindsight/day": test_pnl[ticker, hindsight] / test_days[ticker],
        })
    table_rows = DataFrame(rows).sort_values("train/day", ascending=False)

    table = Table(title=f"Out of sample: edge picked on days up to {train_until}, traded on the days after (pnl per day, USDT)")
    for column in ("ticker", "train edge", "train", "traded", "test", "test win days", "hindsight edge", "hindsight"):
        table.add_column(column, justify="right")
    for _, row in table_rows.iterrows():
        table.add_row(
            row["ticker"], f"{row['edge']:g}%", f"{row['train/day']:+.2f}", "yes" if row["traded"] else "no",
            f"{row['test/day']:+.2f}", row["test wins"], f"{row['hindsight edge']:g}%", f"{row['hindsight/day']:+.2f}",
        )
    traded = table_rows[table_rows["traded"]]
    table.add_section()
    table.add_row(
        f"{len(traded)} traded", "", f"{traded['train/day'].sum():+.2f}", "", f"{traded['test/day'].sum():+.2f}", "", "",
        f"{table_rows['hindsight/day'].clip(lower=0).sum():+.2f}",
    )
    console.print(table)
