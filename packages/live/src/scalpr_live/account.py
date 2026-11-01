from dataclasses import dataclass


# ACCOUNT: the two legs of one coin as the exchanges hold them. Unlike the paper Ledger (a fill and its hedge booked
# together) the legs move apart: a maker fill first, its hedge later and maybe in other sizes. Positions in coins,
# cash and fees in USDT.
@dataclass
class Account:
    maker: float = 0.0  # position on the maker exchange, coins, + long
    hedge: float = 0.0  # on the hedge exchange
    cash: float = 0.0
    fees: float = 0.0

    # `coins` bought (+) or sold (-) at `price` on one leg, the fee in % of the notional
    def trade(self, maker_leg: bool, coins: float, price: float, fee: float) -> float:
        paid = abs(coins) * price * fee / 100
        if maker_leg:
            self.maker = round(self.maker + coins, 10)
        else:
            self.hedge = round(self.hedge + coins, 10)
        self.cash -= coins * price + paid
        self.fees += paid
        return paid

    # what both legs are worth at the mids, net of everything paid
    def marked(self, maker_mid: float, hedge_mid: float) -> float:
        return self.cash + self.maker * maker_mid + self.hedge * hedge_mid

    # coins not offset by the other leg
    @property
    def net(self) -> float:
        return round(self.maker + self.hedge, 10)
