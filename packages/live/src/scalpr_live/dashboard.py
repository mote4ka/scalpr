from rich.table import Table

from scalpr_live.orders import Order
from scalpr_live.trader import LiveTrader


def _order(order: Order | None) -> str:
    if order is None:
        return "-"
    state = "" if order.state == "live" else f" {order.state}"
    return f"{order.price:g} x {order.left}{state}"


# DASHBOARD: one row per coin, our orders as the trader sees them, both legs, and the paper twin's pnl next to ours
def dashboard(trader: LiveTrader, title: str) -> Table:
    requests = ", ".join(f"{exchange} {trader.requests[exchange]} (peak {trader.peak[exchange]}/s)" for exchange in (trader.maker, trader.hedge))
    mode = "[bold red]REAL ORDERS[/]" if trader.real else "dry run, nothing is sent"
    caption = f"{mode} | requests: {requests} | budget refusals {trader.deferred}"
    if trader.halted:
        caption += f" | [red]HALTED: {trader.halted}[/]"
    table = Table(title=title, caption=caption)
    for column in ("ticker", "status", "our bid", "our ask", "position htx / bybit", "owed hedge", "fills buy / sell", "volume", "fees", "pnl", "paper pnl"):
        table.add_column(column, justify="right")
    total = paper_total = 0.0
    for live in trader.coins.values():
        pnl, paper = trader.coin_pnl(live), trader.engine.pnl(trader.engine.coins[live.coin.ticker])
        total, paper_total = total + pnl, paper_total + paper
        table.add_row(
            live.coin.ticker, live.status, _order(live.orders.sides[True]), _order(live.orders.sides[False]),
            f"{live.account.maker:+g} / {live.account.hedge:+g}", f"{live.hedger.owed:+g}",
            f"{live.fills[True]} / {live.fills[False]}", f"{live.volume:,.0f}", f"{live.account.fees:.2f}", f"{pnl:+.2f}", f"{paper:+.2f}",
        )
    table.add_section()
    table.add_row("total", "", "", "", "", "", "", "", "", f"{total:+.2f}", f"{paper_total:+.2f}")
    return table
