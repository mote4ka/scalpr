import math
from dataclasses import dataclass

import numpy as np
from pandas import DataFrame, Series, Timedelta, Timestamp

from scalpr_scanner.maker import Book, Tape
from scalpr_scanner.spread import FEES

FILL_COLUMNS = ["ts", "side", "price", "size", "usdt", "fair", "ahead", "hedge", "edge %", "edge 1m %", "inventory"]
# how far after a fill the edge is measured again: the gap shows adverse selection
MARKOUT = Timedelta("1min")


@dataclass
class Xemm:
    fills: DataFrame
    # on the grid: our bid and ask (NaN: not quoting that side), inventory in USDT, pnl in USDT marked to mids
    curve: DataFrame
    # pnl if the inventory were closed with taker orders on both exchanges at the end
    closed_pnl: float
    fees: float


# CROSS-EXCHANGE MARKET MAKING: `maker` exchange is quoted all the time, every fill is hedged with a taker order
# on the other exchange `latency` later
# fair price on the maker exchange = mid of the other one moved by the basis; quotes sit `edge` % away from it on
# the tick grid and never cross the maker book; a side stops quoting when inventory reaches `max_position` USDT
# quotes are refreshed every grid step and filled by the trades that come during it (see _filled)
def xemm(spreads: DataFrame, basis: Series, native: dict[str, DataFrame], grid: dict[str, DataFrame], trades: DataFrame,
         maker: str, notional: float, edge: float, max_position: float, rule: str, latency: str) -> Xemm:
    taker = "htx" if maker == "bybit" else "bybit"
    # deviation is htx vs bybit, so the basis shifts bybit up to htx or htx down to bybit
    fair = grid["bybit"]["mid"] * (1 + basis / 100) if maker == "htx" else grid["htx"]["mid"] / (1 + basis / 100)
    fair = fair.to_numpy()
    best_bid, best_ask = grid[maker]["bid"].to_numpy(), grid[maker]["ask"].to_numpy()
    maker_mid, taker_mid = grid[maker]["mid"].to_numpy(), grid[taker]["mid"].to_numpy()
    tick = _tick(native[maker])
    books, tape = {exchange: Book(prices) for exchange, prices in native.items()}, Tape(trades)
    hedges = books[taker]
    ts = spreads.index.as_unit("ns").asi8
    step, delay, markout = ts[1] - ts[0], Timedelta(latency).value, MARKOUT.value
    n = len(ts)

    inventory = cash = fees = 0.0  # inventory in coins on the maker exchange, the hedge holds the opposite
    bids, asks, inventories, pnl = (np.full(n, np.nan) for _ in range(4))
    fills = []
    for k in range(n):
        if not np.isnan(fair[k] + best_bid[k] + best_ask[k]):
            bid = math.floor(fair[k] * (1 - edge / 100) / tick) * tick
            ask = math.ceil(fair[k] * (1 + edge / 100) / tick) * tick
            # stay a maker: a quote over the other side of the book would be a taker order
            bid, ask = min(bid, best_ask[k] - tick), max(ask, best_bid[k] + tick)
            bid_left = notional / bid if inventory * fair[k] < max_position else 0.0
            ask_left = notional / ask if -inventory * fair[k] < max_position else 0.0
            bids[k], asks[k] = (bid if bid_left else np.nan), (ask if ask_left else np.nan)

            first, last = np.searchsorted(tape.ts, ts[k], side="right"), np.searchsorted(tape.ts, ts[k] + step, side="right")
            for j in range(first, last):
                buys = bool(tape.taker_sells[j])  # a taker sell hits our bid
                quote, left = (bid, bid_left) if buys else (ask, ask_left)
                qty = _filled(tape.price[j], tape.size[j], quote, best_bid[k] if buys else best_ask[k], left, buys, rule, tick) if left else 0.0
                if not qty:
                    continue
                sign = 1 if buys else -1
                hedge = hedges.at(tape.ts[j] + delay, "vwap_sell" if buys else "vwap_buy")
                if np.isnan(hedge):
                    # book thinner than the notional: the best price is the closest guess
                    hedge = hedges.at(tape.ts[j] + delay, "bid" if buys else "ask")
                fee = (quote * FEES[maker]["maker"] + hedge * FEES[taker]["taker"]) * qty / 100
                cash += sign * (hedge - quote) * qty - fee
                fees += fee
                inventory += sign * qty
                if buys:
                    bid_left -= qty
                else:
                    ask_left -= qty
                later = np.searchsorted(ts, tape.ts[j] + markout)
                fills.append({
                    "ts": Timestamp(int(tape.ts[j]), tz="UTC"),
                    "side": "buy" if buys else "sell",
                    "price": quote,
                    "size": qty,
                    "usdt": quote * qty,
                    "fair": fair[k],
                    # alone at the best price, the taker traded with us first
                    "ahead": quote > best_bid[k] + tick / 2 if buys else quote < best_ask[k] - tick / 2,
                    "hedge": hedge,
                    # how much better than fair we traded, now and a minute later
                    "edge %": sign * (fair[k] - quote) / quote * 100,
                    "edge 1m %": sign * (fair[later] - quote) / quote * 100 if later < n else np.nan,
                    "inventory": inventory * fair[k],
                })
        inventories[k] = inventory * fair[k] if not np.isnan(fair[k]) else np.nan
        # maker leg at its mid, the hedge at the other mid
        pnl[k] = cash + inventory * (maker_mid[k] - taker_mid[k])

    curve = DataFrame({"bid": bids, "ask": asks, "inventory": inventories, "pnl": pnl}, index=spreads.index)
    # carry the last value over gaps in the data
    curve[["inventory", "pnl"]] = curve[["inventory", "pnl"]].ffill()
    # close at the last moment both books are there (a partial htx day ends earlier than bybit)
    both = np.flatnonzero(~np.isnan(maker_mid + taker_mid))
    closed = _closed_pnl(cash, inventory, books, maker, taker, ts[both[-1]]) if len(both) else cash
    return Xemm(DataFrame(fills, columns=FILL_COLUMNS), curve, closed, fees)


# how much of our resting order a taker print fills, `buys`: ours is a bid hit by a taker sell
# alone at a better price than the book: the taker trades with us first, up to its size
# level with or behind the book: a print past our price ate the level, the whole order is filled;
# a print at our price fills up to its size only with the optimistic "touch" rule (first in the queue)
def _filled(price: float, size: float, quote: float, best: float, left: float, buys: bool, rule: str, tick: float) -> float:
    sign = 1 if buys else -1
    ahead = sign * (quote - best) > tick / 2
    through = sign * (quote - price) > tick / 2
    at = abs(quote - price) <= tick / 2
    if ahead:
        return min(size, left) if through or at else 0.0
    if through:
        return left
    return min(size, left) if at and rule == "touch" else 0.0


# inventory closed with taker orders on both exchanges at `at`, fees included
def _closed_pnl(cash: float, inventory: float, books: dict[str, Book], maker: str, taker: str, at: int) -> float:
    if inventory == 0:
        return cash
    long = inventory > 0
    maker_exit = books[maker].at(at, "vwap_sell" if long else "vwap_buy")
    taker_exit = books[taker].at(at, "vwap_buy" if long else "vwap_sell")
    fee = (maker_exit * FEES[maker]["taker"] + taker_exit * FEES[taker]["taker"]) * abs(inventory) / 100
    return cash + inventory * (maker_exit - taker_exit) - fee


# price step of the exchange: the smallest gap between best prices seen during the day
def _tick(prices: DataFrame) -> float:
    levels = np.unique(np.concatenate([prices["bid"].dropna().to_numpy(), prices["ask"].dropna().to_numpy()]))
    gaps = np.diff(levels)
    return float(np.round(gaps[gaps > 0].min(), 10))

