FILL_RULES = ("through", "touch")


# FILLED: how much of our resting order a taker print fills, `buys`: ours is a bid hit by a taker sell
# alone at a better price than the book (`best`, the maker book without us): the taker trades with us first, up to
# its size; level with or behind the book: a print past our price ate the level, the whole order is filled;
# a print at our price fills up to its size only with the optimistic "touch" rule (first in the queue)
def filled(price: float, size: float, quote: float, best: float, left: float, buys: bool, rule: str, tick: float) -> float:
    sign = 1 if buys else -1
    ahead = sign * (quote - best) > tick / 2
    through = sign * (quote - price) > tick / 2
    at = abs(quote - price) <= tick / 2
    if ahead:
        return min(size, left) if through or at else 0.0
    if through:
        return left
    return min(size, left) if at and rule == "touch" else 0.0


# MARKOUT: value of a fill and its hedge some time later at the mids, % of the fill price, before fees
# sign 1 is long on the maker exchange and short on the hedge one; deviation is htx vs bybit in % at that time:
# with the maker on htx (hedge / quote - 1) + deviation, on bybit the deviation counts the other way
def markout(sign: int, quote: float, hedge: float, deviation: float, maker: str) -> float:
    maker_premium = deviation if maker == "htx" else -deviation
    return sign * ((hedge / quote - 1) * 100 + maker_premium)
