import numpy as np


# FAIR PRICE on the maker exchange: mid of the hedge exchange moved by the basis
# basis is the htx vs bybit deviation in %, so it shifts bybit up to htx or htx down to bybit
# works on numbers and on numpy arrays alike
def fair_price(hedge_mid, basis, maker: str):
    shift = 1 + basis / 100
    return hedge_mid * shift if maker == "htx" else hedge_mid / shift


# QUOTES: bid and ask `edge` % away from fair on the tick grid; stay a maker: a quote over the other side of the
# book would be a taker order, so it stops a tick short of it
# works on numbers and on numpy arrays alike (NaN in, NaN out)
def quote_prices(fair, edge: float, tick: float, best_bid, best_ask):
    bid = np.minimum(np.floor(fair * (1 - edge / 100) / tick) * tick, best_ask - tick)
    ask = np.maximum(np.ceil(fair * (1 + edge / 100) / tick) * tick, best_bid + tick)
    return bid, ask
