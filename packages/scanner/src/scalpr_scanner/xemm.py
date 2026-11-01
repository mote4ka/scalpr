from dataclasses import dataclass

import numpy as np
from pandas import DataFrame, Series, Timedelta, Timestamp
from scalpr_strategy import (
    FEES,
    Ledger,
    fair_price,
    filled,
    inventory_shift,
    markout,
    quote_edge,
    quote_prices,
)

from scalpr_scanner.maker import Book, Tape

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
# a quote is filled by the trades that come while it stands (see scalpr_strategy.filled)
# dynamic quotes, as in the trader (scalpr_strategy.quote_prices): `anchor` "book" keeps a quote no more than a tick
# better than the maker book; `vol_edge` adds that many times the hedge mid volatility over `vol_window` to the edge;
# `skew` % leans both quotes against the inventory; with any of them a quote whose price changes is a new quote
def xemm(spreads: DataFrame, basis: Series, native: dict[str, DataFrame], grid: dict[str, DataFrame], trades: DataFrame,
         maker: str, notional: float, edge: float, max_position: float, rule: str, latency: str, quotes: str = "tick",
         hedge_latency: str | None = None, anchor: str = "fair", vol_edge: float = 0.0, skew: float = 0.0,
         vol_window: str = "5min", maker_fee: float | None = None, hedge_fee: float | None = None) -> Xemm:
    taker = "htx" if maker == "bybit" else "bybit"
    # % per trade: the account's when given, else the standard tier
    maker_fee = FEES[maker]["maker"] if maker_fee is None else maker_fee
    hedge_fee = FEES[taker]["taker"] if hedge_fee is None else hedge_fee
    fair = fair_price(grid[taker]["mid"], basis, maker).to_numpy()
    best_bid, best_ask = grid[maker]["bid"].to_numpy(), grid[maker]["ask"].to_numpy()
    maker_mid, taker_mid = grid[maker]["mid"].to_numpy(), grid[taker]["mid"].to_numpy()
    deviation = spreads["deviation"].to_numpy()
    tick = _tick(native[maker])
    books, tape = {exchange: Book(prices) for exchange, prices in native.items()}, Tape(trades)
    hedges = books[taker]
    ts = spreads.index.as_unit("ns").asi8
    step, delay, markout_after = ts[1] - ts[0], Timedelta(latency).value, MARKOUT.value
    # the hedge takes `latency` too unless it has its own
    hedge_delay = Timedelta(hedge_latency).value if hedge_latency is not None else delay
    n = len(ts)
    dynamic = anchor != "fair" or bool(vol_edge) or bool(skew)

    # edge of every step: the volatility is the std of the hedge mid's returns over `vol_window`, per second, %, the
    # same as scalpr_strategy.Volatility in the trader (unknown until half the window is there)
    edges = edge
    if vol_edge:
        window = max(2, round(Timedelta(vol_window).value / step))
        returns = np.log(Series(taker_mid)).diff() * 100
        vol = returns.rolling(window, min_periods=max(2, window // 2)).std().to_numpy() / np.sqrt(step / 1e9)
        edges = quote_edge(edge, vol_edge, vol)

    def edge_at(k: int) -> float:
        return edges[k] if isinstance(edges, np.ndarray) else edges

    def edge_part(start: int, end: int):
        return edges[start:end] if isinstance(edges, np.ndarray) else edges

    # quotes of every step, before the inventory skew
    quoting = ~np.isnan(fair + best_bid + best_ask)
    with np.errstate(invalid="ignore"):
        quote_bids, quote_asks = quote_prices(fair, edges, tick, best_bid, best_ask, anchor)
    # step k takes the trades in (ts[k], ts[k] + step]
    trade_steps = np.searchsorted(ts, tape.ts, side="left") - 1
    in_day = (trade_steps >= 0) & (tape.ts <= ts[-1] + step)

    ledger = Ledger()
    bids, asks, inventories, pnl = (np.full(n, np.nan) for _ in range(4))
    fills = []

    # between steps with trades nothing changes but the prices: those stretches are filled at once
    def carry(start: int, end: int) -> None:
        with np.errstate(invalid="ignore"):
            exposure = ledger.inventory * fair[start:end]
            if skew:
                shift = inventory_shift(exposure, max_position, skew)
                step_bids, step_asks = quote_prices(fair[start:end], edge_part(start, end), tick, best_bid[start:end], best_ask[start:end], anchor, shift)
            else:
                step_bids, step_asks = quote_bids[start:end], quote_asks[start:end]
            bids[start:end] = np.where(quoting[start:end] & (exposure < max_position), step_bids, np.nan)
            asks[start:end] = np.where(quoting[start:end] & (-exposure < max_position), step_asks, np.nan)
            inventories[start:end] = exposure
            pnl[start:end] = ledger.marked(maker_mid[start:end], taker_mid[start:end])

    def fill(j: int, buys: bool, quote: float, qty: float, fair_now: float, best: float) -> None:
        sign = 1 if buys else -1
        hedge = hedges.at(tape.ts[j] + hedge_delay, "vwap_sell" if buys else "vwap_buy")
        if np.isnan(hedge):
            # book thinner than the notional: the best price is the closest guess
            hedge = hedges.at(tape.ts[j] + hedge_delay, "bid" if buys else "ask")
        ledger.fill(buys, quote, qty, hedge, maker_fee, hedge_fee)
        later = np.searchsorted(ts, tape.ts[j] + markout_after)
        fills.append({
            "ts": Timestamp(int(tape.ts[j]), tz="UTC"),
            "side": "buy" if buys else "sell",
            "price": quote,
            "size": qty,
            "usdt": quote * qty,
            "fair": fair_now,
            # alone at the best price, the taker traded with us first
            "ahead": sign * (quote - best) > tick / 2,
            "hedge": hedge,
            # how much better than fair we traded
            "edge %": sign * (fair_now - quote) / quote * 100,
            # fill + hedge a minute later at the mids, before fees: what is locked between the two prices plus
            # where the htx vs bybit deviation went; the moves of the price itself are hedged away
            "markout 1m %": markout(sign, quote, hedge, deviation[later], maker) if later < n else np.nan,
            "inventory": ledger.inventory * fair_now,
        })

    # TICK QUOTES: the quote standing at `at`, made from the last hedge book update before it: (update, fair, bid, ask)
    source, maker_book, basis_values = books[taker], books[maker], basis.to_numpy()

    def live_quote(at: int) -> tuple[int, float, float, float] | None:
        update, step_of = np.searchsorted(source.ts, at, side="right") - 1, np.searchsorted(ts, at, side="right") - 1
        if update < 0 or step_of < 0:
            return None
        fair_then = fair_price(source.at(at, "mid"), basis_values[step_of], maker)
        bid_then, ask_then = maker_book.at(at, "bid"), maker_book.at(at, "ask")
        if np.isnan(fair_then + bid_then + ask_then):
            return None
        shift = inventory_shift(ledger.inventory * fair_then, max_position, skew)
        bid, ask = quote_prices(fair_then, edge_at(step_of), tick, bid_then, ask_then, anchor, shift)
        return update, fair_then, bid, ask

    # size left of the standing bid (True) and ask (False), and the hedge book update each quote was made from
    left_by_side, quote_update = {True: 0.0, False: 0.0}, {True: None, False: None}

    done = 0
    # the tape is sorted, so are its steps: the trades of a step are one slice of it
    steps = np.unique(trade_steps[in_day])
    for k, first, last in zip(steps, np.searchsorted(trade_steps, steps, "left"), np.searchsorted(trade_steps, steps, "right"), strict=True):
        carry(done, k + 1)  # step k is quoted with the inventory it starts with
        if quotes == "grid" and quoting[k]:
            if skew:
                shift = inventory_shift(ledger.inventory * fair[k], max_position, skew)
                bid, ask = quote_prices(fair[k], edge_at(k), tick, best_bid[k], best_ask[k], anchor, shift)
            else:
                bid, ask = quote_bids[k], quote_asks[k]
            bid_left = notional / bid if ledger.inventory * fair[k] < max_position else 0.0
            ask_left = notional / ask if -ledger.inventory * fair[k] < max_position else 0.0
            for j in range(first, last):
                if not in_day[j]:
                    continue
                buys = bool(tape.taker_sells[j])  # a taker sell hits our bid
                quote, left, best = (bid, bid_left, best_bid[k]) if buys else (ask, ask_left, best_ask[k])
                qty = filled(tape.price[j], tape.size[j], quote, best, left, buys, rule, tick) if left else 0.0
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
                # dynamic quotes can move without a hedge book update: a new price is a new quote too
                key = (update, quote) if dynamic else update
                if quote_update[buys] != key:
                    # a new quote: full size, unless that side is at the inventory limit
                    quote_update[buys] = key
                    exposure = ledger.inventory * fair_then if buys else -ledger.inventory * fair_then
                    left_by_side[buys] = notional / quote if exposure < max_position else 0.0
                # the maker book right before the print tells if our quote was the best one
                best = maker_book.at(tape.ts[j] - 1, "bid" if buys else "ask")
                left = left_by_side[buys]
                qty = filled(tape.price[j], tape.size[j], quote, best, left, buys, rule, tick) if left and not np.isnan(best) else 0.0
                if not qty:
                    continue
                fill(j, buys, quote, qty, fair_then, best)
                left_by_side[buys] -= qty
        inventories[k] = ledger.inventory * fair[k]
        pnl[k] = ledger.marked(maker_mid[k], taker_mid[k])
        done = k + 1
    carry(done, n)

    curve = DataFrame({"bid": bids, "ask": asks, "inventory": inventories, "pnl": pnl}, index=spreads.index)
    # carry the last value over gaps in the data
    curve[["inventory", "pnl"]] = curve[["inventory", "pnl"]].ffill()
    # close at the last moment both books are there (a partial htx day ends earlier than bybit)
    both = np.flatnonzero(~np.isnan(maker_mid + taker_mid))
    closed = _closed_pnl(ledger.cash, ledger.inventory, books, maker, taker, ts[both[-1]], hedge_fee) if len(both) else ledger.cash
    return Xemm(DataFrame(fills, columns=FILL_COLUMNS), curve, closed, ledger.fees)


# inventory closed with taker orders on both exchanges at `at`, fees included
def _closed_pnl(cash: float, inventory: float, books: dict[str, Book], maker: str, taker: str, at: int, hedge_fee: float) -> float:
    if inventory == 0:
        return cash
    long = inventory > 0
    maker_exit = books[maker].at(at, "vwap_sell" if long else "vwap_buy")
    taker_exit = books[taker].at(at, "vwap_buy" if long else "vwap_sell")
    fee = (maker_exit * FEES[maker]["taker"] + taker_exit * hedge_fee) * abs(inventory) / 100
    return cash + inventory * (maker_exit - taker_exit) - fee


# price step of the exchange: the smallest gap between best prices seen during the day
def _tick(prices: DataFrame) -> float:
    levels = np.unique(np.concatenate([prices["bid"].dropna().to_numpy(), prices["ask"].dropna().to_numpy()]))
    gaps = np.diff(levels)
    return float(np.round(gaps[gaps > 0].min(), 10))

