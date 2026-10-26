from collections import deque
from dataclasses import dataclass

from scalpr_strategy import filled


# a quote as the exchange holds it
@dataclass
class Quote:
    price: float
    left: float  # coins still to fill


# PAPER QUOTES: our bid and ask of one coin on the maker exchange, the way the exchange would hold them
# a sent quote (or a cancel) goes live `latency` after it was sent, so a fast move can still hit the old one;
# a trade meets the quote live when the trade arrives (see scalpr_strategy.filled); nothing goes to the exchange
class PaperQuotes:
    def __init__(self, latency: int):
        self.latency = latency
        self.live: dict[bool, Quote | None] = {True: None, False: None}  # True: bid
        self.pending: dict[bool, deque[tuple[int, Quote | None]]] = {True: deque(), False: deque()}

    # `price` None cancels that side
    def send(self, now: int, bid: bool, price: float | None, size: float) -> None:
        self.pending[bid].append((now + self.latency, Quote(price, size) if price is not None else None))

    def standing(self, now: int, bid: bool) -> Quote | None:
        pending = self.pending[bid]
        while pending and pending[0][0] <= now:
            self.live[bid] = pending.popleft()[1]
        return self.live[bid]

    # a taker print; `sells`: the taker sold, so it can hit our bid; `best`: the maker book on that side right before
    # the print; returns (price, qty) of our fill
    def match(self, now: int, sells: bool, price: float, size: float, best: float, rule: str, tick: float) -> tuple[float, float] | None:
        quote = self.standing(now, sells)
        if quote is None or quote.left <= 0:
            return None
        qty = filled(price, size, quote.price, best, quote.left, sells, rule, tick)
        if not qty:
            return None
        quote.left -= qty
        return quote.price, qty
