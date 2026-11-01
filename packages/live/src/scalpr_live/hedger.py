import math

from scalpr_live.specs import EPSILON, Spec


# HEDGER: what one coin's maker fills still owe the hedge exchange. Owed coins go out as soon as they make a whole
# bybit order (quantity steps, the smallest quantity and notional); a smaller remainder waits for the next fills
# (a DOT fill under ~$5 can't be hedged alone). Hedges are IOC: the part that didn't fill is owed again.
class Hedger:
    def __init__(self, spec: Spec):
        self.spec = spec
        self.owed = 0.0  # coins to buy (+) or sell (-) on the hedge exchange
        self.sending = 0.0  # signed coins in hedge orders without an answer yet

    # a maker fill: bought on the maker exchange, so the hedge sells
    def on_fill(self, bought: bool, coins: float) -> None:
        self.owed = round(self.owed - coins if bought else self.owed + coins, 10)

    # signed coins of the hedge order to send now at about `price`, 0 when what's free is under the smallest order
    def order(self, price: float) -> float:
        free = self.owed - self.sending
        qty = self.spec.hedge_qty(abs(free))
        if qty <= 0 or qty < self.spec.min_hedge(price) - EPSILON:
            return 0.0
        signed = math.copysign(qty, free)
        self.sending = round(self.sending + signed, 10)
        return signed

    # a hedge order answered: `filled` of the `sent` coins traded (signed alike)
    def answered(self, sent: float, filled: float) -> None:
        self.sending = round(self.sending - sent, 10)
        self.owed = round(self.owed - filled, 10)

    # an owed amount that a hedge order should already take: not just a remainder under the smallest order
    def overdue(self, price: float) -> bool:
        return self.spec.hedge_qty(abs(self.owed)) >= self.spec.min_hedge(price) - EPSILON
