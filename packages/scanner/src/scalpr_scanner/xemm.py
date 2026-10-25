import math
from dataclasses import dataclass

import numpy as np
from pandas import DataFrame, Series, Timedelta, Timestamp

from scalpr_scanner.maker import Book, Tape
from scalpr_scanner.spread import FEES

# grid: quotes are set once per grid step from that step's prices
# tick: quotes follow every update of the hedge exchange's book and reach the maker exchange `latency` later
QUOTE_MODES = ("tick", "grid")
FILL_COLUMNS = ["ts", "side", "price", "size", "usdt", "fair", "ahead", "hedge", "edge %", "markout 1m %", "inventory"]
# how far after a fill it is marked again: a drop from the edge shows adverse selection
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
# `quotes` (see QUOTE_MODES): with "tick" a trade meets the quote made from the books `latency` before it, so a move
# of the hedge exchange can catch a quote that is not replaced yet; the curve still shows the grid step quotes
# a quote is filled by the trades that come while it stands (see _filled)
def xemm(spreads: DataFrame, basis: Series, native: dict[str, DataFrame], grid: dict[str, DataFrame], trades: DataFrame,
         maker: str, notional: float, edge: float, max_position: float, rule: str, latency: str, quotes: str = "tick") -> Xemm:
    taker = "htx" if maker == "bybit" else "bybit"
    # deviation is htx vs bybit, so the basis shifts bybit up to htx or htx down to bybit
    fair = grid["bybit"]["mid"] * (1 + basis / 100) if maker == "htx" else grid["htx"]["mid"] / (1 + basis / 100)
    fair = fair.to_numpy()
    best_bid, best_ask = grid[maker]["bid"].to_numpy(), grid[maker]["ask"].to_numpy()
    maker_mid, taker_mid = grid[maker]["mid"].to_numpy(), grid[taker]["mid"].to_numpy()
    deviation = spreads["deviation"].to_numpy()
    tick = _tick(native[maker])
    books, tape = {exchange: Book(prices) for exchange, prices in native.items()}, Tape(trades)
    hedges = books[taker]
    ts = spreads.index.as_unit("ns").asi8
    step, delay, markout = ts[1] - ts[0], Timedelta(latency).value, MARKOUT.value
    n = len(ts)

    # quotes of every step on the tick grid; stay a maker: a quote over the other side of the book would be a taker order
    quoting = ~np.isnan(fair + best_bid + best_ask)
    with np.errstate(invalid="ignore"):
        quote_bids = np.minimum(np.floor(fair * (1 - edge / 100) / tick) * tick, best_ask - tick)
        quote_asks = np.maximum(np.ceil(fair * (1 + edge / 100) / tick) * tick, best_bid + tick)
    # step k takes the trades in (ts[k], ts[k] + step]
    trade_steps = np.searchsorted(ts, tape.ts, side="left") - 1
    in_day = (trade_steps >= 0) & (tape.ts <= ts[-1] + step)

    inventory = cash = fees = 0.0  # inventory in coins on the maker exchange, the hedge holds the opposite
    bids, asks, inventories, pnl = (np.full(n, np.nan) for _ in range(4))
    fills = []

    # between steps with trades nothing changes but the prices: those stretches are filled at once
    def carry(start: int, end: int) -> None:
        with np.errstate(invalid="ignore"):
            exposure = inventory * fair[start:end]
            bids[start:end] = np.where(quoting[start:end] & (exposure < max_position), quote_bids[start:end], np.nan)
            asks[start:end] = np.where(quoting[start:end] & (-exposure < max_position), quote_asks[start:end], np.nan)
            inventories[start:end] = exposure
            # maker leg at its mid, the hedge at the other mid
            pnl[start:end] = cash + inventory * (maker_mid[start:end] - taker_mid[start:end])

    def fill(j: int, buys: bool, quote: float, qty: float, fair_price: float, best: float) -> None:
        nonlocal inventory, cash, fees
        sign = 1 if buys else -1
        hedge = hedges.at(tape.ts[j] + delay, "vwap_sell" if buys else "vwap_buy")
        if np.isnan(hedge):
            # book thinner than the notional: the best price is the closest guess
            hedge = hedges.at(tape.ts[j] + delay, "bid" if buys else "ask")
        fee = (quote * FEES[maker]["maker"] + hedge * FEES[taker]["taker"]) * qty / 100
        cash += sign * (hedge - quote) * qty - fee
        fees += fee
        inventory += sign * qty
        later = np.searchsorted(ts, tape.ts[j] + markout)
        fills.append({
            "ts": Timestamp(int(tape.ts[j]), tz="UTC"),
            "side": "buy" if buys else "sell",
            "price": quote,
            "size": qty,
            "usdt": quote * qty,
            "fair": fair_price,
            # alone at the best price, the taker traded with us first
            "ahead": sign * (quote - best) > tick / 2,
            "hedge": hedge,
            # how much better than fair we traded
            "edge %": sign * (fair_price - quote) / quote * 100,
            # fill + hedge a minute later at the mids, before fees: what is locked between the two prices plus
            # where the htx vs bybit deviation went; the moves of the price itself are hedged away
            "markout 1m %": _markout(sign, quote, hedge, deviation[later], maker) if later < n else np.nan,
            "inventory": inventory * fair_price,
        })

    # TICK QUOTES: the quote standing at `at`, made from the last hedge book update before it: (update, fair, bid, ask)
    source, maker_book, basis_values = books[taker], books[maker], basis.to_numpy()

    def live_quote(at: int) -> tuple[int, float, float, float] | None:
        update, step_of = np.searchsorted(source.ts, at, side="right") - 1, np.searchsorted(ts, at, side="right") - 1
        if update < 0 or step_of < 0:
            return None
        shift = 1 + basis_values[step_of] / 100
        fair_then = source.at(at, "mid") * shift if maker == "htx" else source.at(at, "mid") / shift
        bid_then, ask_then = maker_book.at(at, "bid"), maker_book.at(at, "ask")
        if np.isnan(fair_then + bid_then + ask_then):
            return None
        bid = min(math.floor(fair_then * (1 - edge / 100) / tick) * tick, ask_then - tick)
        ask = max(math.ceil(fair_then * (1 + edge / 100) / tick) * tick, bid_then + tick)
        return update, fair_then, bid, ask

    # size left of the standing bid (True) and ask (False), and the hedge book update each quote was made from
    left_by_side, quote_update = {True: 0.0, False: 0.0}, {True: None, False: None}

    done = 0
    # the tape is sorted, so are its steps: the trades of a step are one slice of it
    steps = np.unique(trade_steps[in_day])
    for k, first, last in zip(steps, np.searchsorted(trade_steps, steps, "left"), np.searchsorted(trade_steps, steps, "right"), strict=True):
        carry(done, k + 1)  # step k is quoted with the inventory it starts with
        if quotes == "grid" and quoting[k]:
            bid, ask = quote_bids[k], quote_asks[k]
            bid_left = notional / bid if inventory * fair[k] < max_position else 0.0
            ask_left = notional / ask if -inventory * fair[k] < max_position else 0.0
            for j in range(first, last):
                if not in_day[j]:
                    continue
                buys = bool(tape.taker_sells[j])  # a taker sell hits our bid
                quote, left, best = (bid, bid_left, best_bid[k]) if buys else (ask, ask_left, best_ask[k])
                qty = _filled(tape.price[j], tape.size[j], quote, best, left, buys, rule, tick) if left else 0.0
                if not qty:
                    continue
                fill(j, buys, quote, qty, fair[k], best)
                if buys:
                    bid_left -= qty
                else:
                    ask_left -= qty
        elif quotes == "tick":
            for j in range(first, last):
                if not in_day[j]:
                    continue
                buys = bool(tape.taker_sells[j])
                standing = live_quote(tape.ts[j] - delay)
                if standing is None:
                    continue
                update, fair_then, bid, ask = standing
                quote = bid if buys else ask
                if quote_update[buys] != update:
                    # a new quote: full size, unless that side is at the inventory limit
                    quote_update[buys] = update
                    exposure = inventory * fair_then if buys else -inventory * fair_then
                    left_by_side[buys] = notional / quote if exposure < max_position else 0.0
                # the maker book right before the print tells if our quote was the best one
                best = maker_book.at(tape.ts[j] - 1, "bid" if buys else "ask")
                left = left_by_side[buys]
                qty = _filled(tape.price[j], tape.size[j], quote, best, left, buys, rule, tick) if left and not np.isnan(best) else 0.0
                if not qty:
                    continue
                fill(j, buys, quote, qty, fair_then, best)
                left_by_side[buys] -= qty
        inventories[k] = inventory * fair[k]
        pnl[k] = cash + inventory * (maker_mid[k] - taker_mid[k])
        done = k + 1
    carry(done, n)

    curve = DataFrame({"bid": bids, "ask": asks, "inventory": inventories, "pnl": pnl}, index=spreads.index)
    # carry the last value over gaps in the data
    curve[["inventory", "pnl"]] = curve[["inventory", "pnl"]].ffill()
    # close at the last moment both books are there (a partial htx day ends earlier than bybit)
    both = np.flatnonzero(~np.isnan(maker_mid + taker_mid))
    closed = _closed_pnl(cash, inventory, books, maker, taker, ts[both[-1]]) if len(both) else cash
    return Xemm(DataFrame(fills, columns=FILL_COLUMNS), curve, closed, fees)


# value of a fill and its hedge, % of the fill price: sign 1 is long on the maker exchange, short on the other
# with the maker on htx: (hedge / quote - 1) + htx vs bybit deviation; on bybit the deviation counts the other way
def _markout(sign: int, quote: float, hedge: float, deviation: float, maker: str) -> float:
    maker_premium = deviation if maker == "htx" else -deviation
    return sign * ((hedge / quote - 1) * 100 + maker_premium)


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

