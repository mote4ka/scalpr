import io
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from scalpr_trader.engine import Engine

FILE_WIDTH = 200  # wider than the table, it keeps its own width


# DASHBOARD: one row per coin and a total, what the paper trader is doing right now
def dashboard(engine: Engine, title: str) -> Table:
    heard = ", ".join(f"{exchange} {(engine.now - seen) / 1e9:.1f} s ago" if seen else f"{exchange} not yet" for exchange, seen in engine.seen.items())
    table = Table(title=f"{title} | last heard: {heard}")
    columns = ("ticker", "status", "edge %", "basis %", "fair", "our bid", "our ask", "htx bid", "htx ask",
               "fills buy / sell", "volume", "inventory", "fees", "pnl", "markout")
    for column in columns:
        table.add_column(column, justify="right")
    totals = {"fills": [0, 0], "volume": 0.0, "fees": 0.0, "pnl": 0.0}
    for ticker, state in engine.coins.items():
        maker_book = state.books[engine.maker]
        inventory = state.ledger.inventory * state.fair if not math.isnan(state.fair) else math.nan
        pnl = engine.pnl(state)
        table.add_row(
            ticker,
            f"warmup {_clock(state.basis.left(engine.now))}" if state.status == "warmup" else state.status,
            f"{state.coin.edge:g}",
            _number(state.basis.value, "{:+.3f}"),
            _number(state.fair, "{:.6g}"),
            _number(state.sent[True], "{:.6g}"),
            _number(state.sent[False], "{:.6g}"),
            _number(maker_book.best_bid() if maker_book else None, "{:.6g}"),
            _number(maker_book.best_ask() if maker_book else None, "{:.6g}"),
            f"{state.fills[True]} / {state.fills[False]}",
            f"{state.volume:,.0f}",
            _number(inventory, "{:+.0f}"),
            f"{state.ledger.fees:.2f}",
            _number(pnl, "{:+.2f}"),
            _number(statistics.median(state.recent) if state.recent else None, "{:+.3f}"),
        )
        totals["fills"][0] += state.fills[True]
        totals["fills"][1] += state.fills[False]
        totals["volume"] += state.volume
        totals["fees"] += state.ledger.fees
        totals["pnl"] += 0.0 if math.isnan(pnl) else pnl
    table.add_section()
    table.add_row("total", *[""] * 8, f"{totals['fills'][0]} / {totals['fills'][1]}", f"{totals['volume']:,.0f}", "",
                  f"{totals['fees']:.2f}", f"{totals['pnl']:+.2f}", "")
    return table


# the dashboard with its colors, replaced whole at every write: a trader without a terminal (the systemd service) is
# watched with `watch -c cat <path>`, see deploy/watch.sh; the time on top shows the file is still being written
def write_file(table: Table, path: Path) -> None:
    text = io.StringIO()
    console = Console(file=text, width=FILE_WIDTH, force_terminal=True, color_system="standard")
    console.print(f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC")
    console.print(table)
    part = path.with_suffix(".part")
    part.write_text(text.getvalue(), encoding="utf-8")
    part.replace(path)


# ns as m:ss
def _clock(ns: int) -> str:
    seconds = round(ns / 1e9)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _number(value: float | None, template: str) -> str:
    return "-" if value is None or math.isnan(value) else template.format(value)

