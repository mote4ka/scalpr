import numpy as np
import plotly.graph_objects as go
from pandas import DataFrame, Series
from plotly.subplots import make_subplots

from scalpr_scanner.xemm import Xemm

BYBIT_COLOR = "#f5a623"
HTX_COLOR = "#3b82f6"
DEVIATION_COLOR = "#a78bfa"
BASIS_COLOR = "#e5e7eb"
BAND_COLOR = "#ef4444"
BUY_COLOR = "#22c55e"
SELL_COLOR = "#ef4444"
QUOTE_COLOR = "#94a3b8"


# SPREAD CHART: mid prices on top; below htx deviation from bybit, its basis with a band around it,
# basis trades or, for market making, our quotes and fills on the prices and a third row with pnl and inventory
# Scattergl: a day on a 1s grid is 86k points per line, svg traces get slow on that
def spread_chart(spreads: DataFrame, title: str, basis: Series, band: float,
                 trades: DataFrame | None = None, market_making: Xemm | None = None) -> go.Figure:
    rows = 3 if market_making is not None else 2
    figure = make_subplots(
        rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.05,
        row_heights=[0.35, 0.35, 0.3] if rows == 3 else [0.4, 0.6],
        subplot_titles=("Mid price", "HTX deviation from Bybit, % of mid", "Market making: pnl and inventory, USDT")[:rows],
        specs=[[{}], [{}], [{"secondary_y": True}]][:rows],
    )
    for column, name, color in (("mid_bybit", "Bybit mid", BYBIT_COLOR), ("mid_htx", "HTX mid", HTX_COLOR)):
        figure.add_trace(go.Scattergl(x=spreads.index, y=spreads[column], name=name, line={"width": 1, "color": color}), row=1, col=1)

    deviation = spreads["deviation"]
    figure.add_trace(go.Scattergl(x=spreads.index, y=deviation, name="HTX vs Bybit", line={"width": 1, "color": DEVIATION_COLOR}), row=2, col=1)
    figure.add_trace(go.Scattergl(x=basis.index, y=basis, name="basis", line={"width": 1.5, "color": BASIS_COLOR}), row=2, col=1)
    for line, name in ((basis + band, f"basis ± {band:.3f}%"), (basis - band, None)):
        figure.add_trace(go.Scattergl(
            x=line.index, y=line, name=name, showlegend=name is not None, legendgroup="band",
            line={"width": 1, "dash": "dot", "color": BAND_COLOR},
        ), row=2, col=1)
    figure.add_hline(y=0, line={"color": "#6b7280", "width": 1}, row=2, col=1)
    if trades is not None:
        _add_trades(figure, deviation, trades)
    if market_making is not None:
        _add_market_making(figure, market_making)

    # thin-book mid spikes squash the zone around the basis: start from the 0.5-99.5 percentiles and the band,
    # the full range is one click on autoscale away
    low, high = np.nanquantile(deviation, [0.005, 0.995])
    low, high = min(low, np.nanmin(basis - band)), max(high, np.nanmax(basis + band))
    pad = (high - low) * 0.1
    figure.update_yaxes(ticksuffix="%", range=[low - pad, high + pad], row=2, col=1)

    height = 1100 if rows == 3 else 850
    figure.update_layout(title=title, template="plotly_dark", hovermode="x unified", height=height, legend={"orientation": "h", "y": -0.06})
    return figure


# entries in the color of the exchange we buy on, exits as white crosses, pnl in the hover
def _add_trades(figure: go.Figure, deviation: Series, trades: DataFrame) -> None:
    for buy_on, color in (("Bybit", BYBIT_COLOR), ("HTX", HTX_COLOR)):
        side = trades[trades["buy on"] == buy_on]
        figure.add_trace(go.Scatter(
            x=side["entry"], y=deviation.reindex(side["entry"]), mode="markers", name=f"enter: buy {buy_on}",
            marker={"symbol": "triangle-up" if buy_on == "HTX" else "triangle-down", "size": 9, "color": color},
            text=[f"pnl {pnl:+.3f}%" for pnl in side["pnl %"]], hoverinfo="text",
        ), row=2, col=1)
    closed = trades.dropna(subset=["exit"])
    figure.add_trace(go.Scatter(
        x=closed["exit"], y=deviation.reindex(closed["exit"]), mode="markers", name="exit",
        marker={"symbol": "x", "size": 7, "color": BASIS_COLOR},
        text=[f"held {held}, pnl {pnl:+.3f}%" for held, pnl in zip(closed["held"], closed["pnl %"], strict=True)], hoverinfo="text",
    ), row=2, col=1)


# our quotes and fills over the mid prices, pnl and inventory in the third row
def _add_market_making(figure: go.Figure, result: Xemm) -> None:
    curve, fills = result.curve, result.fills
    for column in ("bid", "ask"):
        figure.add_trace(go.Scattergl(
            x=curve.index, y=curve[column], name=f"our {column}", legendgroup="quotes",
            line={"width": 1, "dash": "dot", "color": QUOTE_COLOR},
        ), row=1, col=1)
    for side, color, symbol in (("buy", BUY_COLOR, "triangle-up"), ("sell", SELL_COLOR, "triangle-down")):
        part = fills[fills["side"] == side]
        figure.add_trace(go.Scatter(
            x=part["ts"], y=part["price"], mode="markers", name=f"we {side}",
            marker={"symbol": symbol, "size": 8, "color": color},
            text=[f"{side} {usdt:.0f} USDT, edge {now:+.3f}%, markout 1m {later:+.3f}%"
                  for usdt, now, later in zip(part["usdt"], part["edge %"], part["markout 1m %"], strict=True)],
            hoverinfo="text",
        ), row=1, col=1)
    figure.add_trace(go.Scattergl(x=curve.index, y=curve["pnl"], name="pnl, USDT", line={"width": 1.5, "color": BUY_COLOR}), row=3, col=1)
    figure.add_trace(go.Scattergl(
        x=curve.index, y=curve["inventory"], name="inventory, USDT", line={"width": 1, "color": DEVIATION_COLOR},
    ), row=3, col=1, secondary_y=True)
