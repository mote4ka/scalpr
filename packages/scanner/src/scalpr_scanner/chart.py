import numpy as np
import plotly.graph_objects as go
from pandas import DataFrame, Series
from plotly.subplots import make_subplots

BYBIT_COLOR = "#f5a623"
HTX_COLOR = "#3b82f6"
DEVIATION_COLOR = "#a78bfa"
BASIS_COLOR = "#e5e7eb"
BAND_COLOR = "#ef4444"


# SPREAD CHART: mid prices on top; below htx deviation from bybit, its basis with entry bands and basis trades
# Scattergl: a day on a 1s grid is 86k points per line, svg traces get slow on that
def spread_chart(spreads: DataFrame, title: str, basis: Series, entry: float, trades: DataFrame) -> go.Figure:
    figure = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.4, 0.6], vertical_spacing=0.06,
        subplot_titles=("Mid price", "HTX deviation from Bybit, % of mid"),
    )
    for column, name, color in (("mid_bybit", "Bybit mid", BYBIT_COLOR), ("mid_htx", "HTX mid", HTX_COLOR)):
        figure.add_trace(go.Scattergl(x=spreads.index, y=spreads[column], name=name, line={"width": 1, "color": color}), row=1, col=1)

    deviation = spreads["deviation"]
    figure.add_trace(go.Scattergl(x=spreads.index, y=deviation, name="HTX vs Bybit", line={"width": 1, "color": DEVIATION_COLOR}), row=2, col=1)
    figure.add_trace(go.Scattergl(x=basis.index, y=basis, name="basis", line={"width": 1.5, "color": BASIS_COLOR}), row=2, col=1)
    for band, name in ((basis + entry, f"basis ± {entry:.3f}%"), (basis - entry, None)):
        figure.add_trace(go.Scattergl(
            x=band.index, y=band, name=name, showlegend=name is not None, legendgroup="band",
            line={"width": 1, "dash": "dot", "color": BAND_COLOR},
        ), row=2, col=1)
    figure.add_hline(y=0, line={"color": "#6b7280", "width": 1}, row=2, col=1)
    _add_trades(figure, deviation, trades)

    # thin-book mid spikes squash the zone around the basis: start from the 0.5-99.5 percentiles and the bands,
    # the full range is one click on autoscale away
    low, high = np.nanquantile(deviation, [0.005, 0.995])
    low, high = min(low, np.nanmin(basis - entry)), max(high, np.nanmax(basis + entry))
    pad = (high - low) * 0.1
    figure.update_yaxes(ticksuffix="%", range=[low - pad, high + pad], row=2, col=1)

    figure.update_layout(title=title, template="plotly_dark", hovermode="x unified", height=850, legend={"orientation": "h", "y": -0.08})
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
