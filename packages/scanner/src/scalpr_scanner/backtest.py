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


# uv run --package scalpr-scanner backtest --top 10 --days 10 --edges 0.1,0.2,0.3,0.5 --latencies 0ms,200ms --walk-forward 3
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
    parser.add_argument("--latencies", default="0ms", help="delays from a fill to the hedge, comma separated (default 0ms)")
    parser.add_argument("--depth", type=int, default=20, help="book levels to load (default 20)")
    parser.add_argument("--freq", default="1s", help="quote refresh step (default 1s)")
    parser.add_argument("--basis-window", default="1h", help="rolling window of the basis (default 1h)")
    parser.add_argument("--train-until", type=date.fromisoformat,
                        help="out of sample check: pick each coin's edge on the days up to this one, trade it on the later days")
    parser.add_argument("--walk-forward", type=int, metavar="DAYS",
                        help="walk forward check: trade every day with the edge picked on the DAYS days before it")
    parser.add_argument("--results", type=Path, help="print a saved results parquet instead of simulating again")
    args = parser.parse_args()

    if args.results:
        results = read_parquet(args.results)
    else:
        tickers = args.tickers.split(",") if args.tickers else top_candidates(args.screen, args.top)
        days = [args.last_day - timedelta(days=back) for back in reversed(range(args.days))]
        edges = [float(edge) for edge in args.edges.split(",")]
        latencies = args.latencies.split(",")
        logger.info(f"{len(tickers)} coins x {len(days)} days ({days[0]} .. {days[-1]}) x edges {edges} x hedge latencies {latencies}")
        results = asyncio.run(run(tickers, days, edges, latencies, args))
        path = BACKTESTS_DIR / f"{datetime.now(UTC):%Y-%m-%d_%H%M}.parquet"
        write_parquet(results, path)
        logger.info(f"Results saved to {path}")
    if "latency" not in results:
        # runs saved before latencies were a column all used 0ms
        results["latency"] = "0ms"

    totals = []
    # latency is NaN on skipped coin-days, they are reported once at the end
    for latency in results["latency"].dropna().unique():
        part = results[results["latency"] == latency]
        totals.append({"latency": latency, **print_results(part, latency)})
        if args.train_until:
            print_out_of_sample(part, args.train_until)
        if args.walk_forward:
            totals[-1].update(print_walk_forward(part, args.walk_forward, latency))
    if len(totals) > 1:
        print_totals(DataFrame(totals))
    print_skipped(results)


def top_candidates(screen_path: Path | None, top: int) -> list[str]:
    path = screen_path or max(SCREENS_DIR.glob("*.parquet"), default=None)
    if path is None:
        raise SystemExit("no screen yet: run `screener` first or pass --tickers")
    screen = read_parquet(path)
    logger.info(f"Top {top} candidates of {path.name}")
    return list(screen.index[screen["candidate"]][:top])


# one coin-day at a time: a day of books takes hundreds of MB, they are dropped before the next one
async def run(tickers: list[str], days: list[date], edges: list[float], latencies: list[str], args: argparse.Namespace) -> DataFrame:
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
            for latency, edge in product(latencies, edges):
                result = xemm(spreads, basis_line, native, grid, tape, "htx", args.notional, edge, args.max_position, args.fill, latency)
                rows.append({"latency": latency, **summary(ticker, day, edge, coverage, result)})
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


# per coin and edge over all days; returns the pnl per day of every coin at its best edge, summed
def print_results(results: DataFrame, latency: str) -> dict:
    done = results.dropna(subset=["pnl"]) if "pnl" in results else results.iloc[0:0]
    if done.empty:
        console.print("[red]nothing simulated[/]")
        return {}
    per = done.groupby(["ticker", "edge"]).agg(
        days=("day", "nunique"), pnl=("pnl", "sum"), win_days=("pnl", lambda pnl: (pnl > 0).sum()),
        volume=("volume", "sum"), fills=("fills", "sum"), markout=("markout 1m %", "median"),
        max_inventory=("max inventory", "max"), coverage=("coverage", "mean"),
    )
    grid = per["pnl"].unstack("edge")
    # the edge with the most pnl for every coin, best coins first
    best = per.loc[per.groupby(level="ticker")["pnl"].idxmax()].reset_index(level="edge").sort_values("pnl", ascending=False)

    table = Table(title=f"XEMM backtest, hedge latency {latency}: P&L in USDT closed every day, sum over up to {done['day'].nunique()} days")
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
    return {"best in sample/day": (best["pnl"] / best["days"]).clip(lower=0).sum()}


def print_skipped(results: DataFrame) -> None:
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


# WALK FORWARD: from the (window + 1)-th day of a coin on, every day is traded with the edge that made the most over
# the `window` days before it, and only if it made money there; hindsight is the best single edge over those
# same traded days, picked after the fact
def print_walk_forward(results: DataFrame, window: int, latency: str) -> dict:
    done = results.dropna(subset=["pnl"]) if "pnl" in results else results.iloc[0:0]
    daily = done.pivot_table(index=["ticker", "day"], columns="edge", values="pnl")
    rows = []
    for ticker, days in daily.groupby(level="ticker"):
        days = days.droplevel("ticker").sort_index()
        for i in range(window, len(days)):
            train = days.iloc[i - window:i].sum()
            edge = train.idxmax()
            traded = train[edge] > 0
            rows.append({"ticker": ticker, "day": days.index[i], "edge": edge, "traded": traded, "pnl": days.iloc[i][edge] if traded else 0.0})
    if not rows:
        console.print(f"[red]walk forward needs more than {window} days per coin[/]")
        return {}
    walk = DataFrame(rows)

    table = Table(title=f"Walk forward, hedge latency {latency}: edge picked on the {window} days before each day (pnl per day, USDT)")
    for column in ("ticker", "test days", "traded", "win days", "edges used", "pnl/day", "hindsight edge", "hindsight/day"):
        table.add_column(column, justify="right")
    summaries = []
    for ticker, part in walk.groupby("ticker"):
        tested = daily.loc[ticker].loc[part["day"]].sum()
        traded = part[part["traded"]]
        summaries.append({
            "ticker": ticker, "days": len(part), "traded": len(traded), "wins": int((traded["pnl"] > 0).sum()),
            "edges": ", ".join(f"{edge:g}" for edge in sorted(traded["edge"].unique())) or "-",
            "pnl/day": part["pnl"].sum() / len(part),
            "hindsight edge": tested.idxmax(), "hindsight/day": max(tested.max(), 0) / len(part),
        })
    summaries = DataFrame(summaries).sort_values("pnl/day", ascending=False)
    for _, row in summaries.iterrows():
        table.add_row(
            row["ticker"], str(row["days"]), str(row["traded"]), f"{row['wins']} / {row['traded']}", row["edges"],
            f"{row['pnl/day']:+.2f}", f"{row['hindsight edge']:g}%", f"{row['hindsight/day']:+.2f}",
        )
    table.add_section()
    table.add_row(
        "total", str(summaries["days"].sum()), str(summaries["traded"].sum()), f"{summaries['wins'].sum()} / {summaries['traded'].sum()}", "",
        f"{summaries['pnl/day'].sum():+.2f}", "", f"{summaries['hindsight/day'].sum():+.2f}",
    )
    console.print(table)
    return {"walk forward/day": summaries["pnl/day"].sum(), "hindsight/day": summaries["hindsight/day"].sum(),
            "coin-days won": f"{summaries['wins'].sum()} / {summaries['traded'].sum()}"}


def print_totals(totals: DataFrame) -> None:
    table = Table(title="By hedge latency: pnl per day of all coins, USDT")
    for column in totals.columns:
        table.add_column(column, justify="right")
    for _, row in totals.iterrows():
        table.add_row(*(f"{value:+.2f}" if isinstance(value, float) else str(value) for value in row))
    console.print(table)
