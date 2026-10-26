from dataclasses import dataclass


# LEDGER: a quoted (maker) leg and its hedge; inventory in coins on the maker exchange, the hedge holds the opposite,
# cash and fees in USDT
@dataclass
class Ledger:
    inventory: float = 0.0
    cash: float = 0.0
    fees: float = 0.0

    # a maker fill of `qty` at `quote` hedged at `hedge`, fees in % of each leg; returns the fee paid
    def fill(self, buys: bool, quote: float, qty: float, hedge: float, maker_fee: float, hedge_fee: float) -> float:
        sign = 1 if buys else -1
        fee = (quote * maker_fee + hedge * hedge_fee) * qty / 100
        self.cash += sign * (hedge - quote) * qty - fee
        self.fees += fee
        self.inventory += sign * qty
        return fee

    # pnl with the maker leg at its mid and the hedge at the other mid (numbers or numpy arrays)
    def marked(self, maker_mid, hedge_mid):
        return self.cash + self.inventory * (maker_mid - hedge_mid)
