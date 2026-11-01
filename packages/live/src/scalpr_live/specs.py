import math
from dataclasses import dataclass

import aiohttp
from scalpr_collector.exchanges import htx as htx_rest

from scalpr_live.bybit import BybitRest

EPSILON = 1e-9


# whole `step`s in `value`, robust to float noise (0.3 / 0.1 is 2.9999999999999996)
def steps(value: float, step: float) -> int:
    return math.floor(value / step + EPSILON)


# SPEC: what one coin's orders must look like on the two exchanges
@dataclass(frozen=True)
class Spec:
    ticker: str  # bybit-style, DOTUSDT
    tick: float  # htx price step
    contract: float  # coins per htx contract
    qty_step: float  # bybit order quantity step, coins
    min_qty: float  # bybit smallest order, coins
    min_notional: float  # bybit smallest order, USDT

    # whole htx contracts of a quote worth `notional` USDT at `price`
    def contracts(self, notional: float, price: float) -> int:
        return max(0, steps(notional / price, self.contract))

    # the smallest bybit order at `price`, coins
    def min_hedge(self, price: float) -> float:
        return max(self.min_qty, math.ceil(self.min_notional / price / self.qty_step - EPSILON) * self.qty_step)

    # `coins` cut down to whole bybit steps
    def hedge_qty(self, coins: float) -> float:
        return round(steps(coins, self.qty_step) * self.qty_step, 10)

    # an htx contract is a whole number of bybit steps, so every fill can be hedged to the coin
    @property
    def steps_fit(self) -> bool:
        return abs(self.contract / self.qty_step - round(self.contract / self.qty_step)) < EPSILON


# public contract details of both exchanges, nothing signed
async def load_specs(session: aiohttp.ClientSession, tickers: list[str]) -> dict[str, Spec]:
    htx = await htx_rest.get_contract_specs(session)
    bybit = BybitRest(session, "", "")
    specs = {}
    for ticker in tickers:
        if ticker not in htx:
            raise SystemExit(f"not traded on htx: {ticker}")
        found = (await bybit.get("/v5/market/instruments-info", {"category": "linear", "symbol": ticker}, signed=False))["list"]
        if not found:
            raise SystemExit(f"not traded on bybit: {ticker}")
        lots = found[0]["lotSizeFilter"]
        specs[ticker] = Spec(ticker, htx[ticker]["tick"], htx[ticker]["size"], float(lots["qtyStep"]), float(lots["minOrderQty"]),
                             float(lots.get("minNotionalValue") or 0))
    return specs
