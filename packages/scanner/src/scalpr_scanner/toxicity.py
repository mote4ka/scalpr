import numpy as np
from pandas import DataFrame, Timedelta

# windows around every htx trade: a quote and its hedge live about this long between a move and our reaction
TOXICITY_WINDOWS = ("50ms", "200ms")
# a side of the bybit tape quiet for longer than this gives no price
STALE = Timedelta("5s").value


# TAPE PRICE: bybit price at any moment from its trades: middle of the last taker buy (near the ask) and the last
# taker sell (near the bid), so the bounce between bid and ask of single trades cancels out
class TapePrice:
    def __init__(self, trades: DataFrame):
        buys, sells = trades[trades["side"] == "buy"], trades[trades["side"] == "sell"]
        self.buy_ts, self.buy_price = buys.index.as_unit("ns").asi8, buys["price"].to_numpy()
        self.sell_ts, self.sell_price = sells.index.as_unit("ns").asi8, sells["price"].to_numpy()

    def at(self, ts: np.ndarray) -> np.ndarray:
        buy = np.searchsorted(self.buy_ts, ts, side="right") - 1
        sell = np.searchsorted(self.sell_ts, ts, side="right") - 1
        fresh = (buy >= 0) & (sell >= 0)
        buy, sell = buy.clip(0), sell.clip(0)
        fresh &= (ts - self.buy_ts[buy] <= STALE) & (ts - self.sell_ts[sell] <= STALE)
        return np.where(fresh, (self.buy_price[buy] + self.sell_price[sell]) / 2, np.nan)


# TOXICITY: mean move of the bybit price from `window` before to `window` after every htx trade, % in the direction
# of the htx taker; a maker filled by that taker loses about this much when it takes about `window` to replace the
# quote and to hedge: positive means htx takers trade on bybit moves (latency arbitrage), ~0 means organic flow
def toxicity(htx_trades: DataFrame, bybit_trades: DataFrame) -> dict[str, float]:
    price = TapePrice(bybit_trades)
    ts = htx_trades.index.as_unit("ns").asi8
    direction = np.where(htx_trades["side"] == "buy", 1, -1)
    result = {}
    for window in TOXICITY_WINDOWS:
        span = Timedelta(window).value
        with np.errstate(invalid="ignore"):
            moves = direction * (price.at(ts + span) / price.at(ts - span) - 1) * 100
        result[f"toxicity {window} %"] = np.nanmean(moves) if np.isfinite(moves).any() else np.nan
    result["toxicity trades"] = int(np.isfinite(moves).sum())
    return result
