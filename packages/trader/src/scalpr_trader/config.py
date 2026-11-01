import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from scalpr_strategy import ANCHORS, FEES, FILL_RULES

UNITS = {"ms": 1_000_000, "s": 1_000_000_000, "min": 60_000_000_000, "h": 3_600_000_000_000}


# "50ms", "5s", "30min", "1h" -> ns
def duration(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|min|h)\s*", text)
    if match is None:
        raise ValueError(f"not a duration: {text!r}, use ms, s, min or h")
    return int(float(match[1]) * UNITS[match[2]])


@dataclass(frozen=True)
class Coin:
    ticker: str  # bybit-style, DOTUSDT
    edge: float  # % from fair
    notional: float  # USDT per quote
    max_position: float  # USDT, a side stops quoting at this inventory
    vol_edge: float = 0.0  # edge grows by this times the hedge mid volatility (std of 1 s returns, %)
    skew: float = 0.0  # % both quotes lean against the inventory at max_position (scalpr_strategy.inventory_shift)


# CONFIG: trader.toml; [trader] holds the defaults, [coins.<TICKER>] sets edge and may override notional,
# max_position, vol_edge and skew; durations are strings like "50ms" (see config.example.toml)
@dataclass(frozen=True)
class Config:
    coins: tuple[Coin, ...]
    maker: str = "htx"  # quoted exchange; the other one takes the hedges
    anchor: str = "fair"  # "book": quotes no more than a tick better than the maker book (scalpr_strategy.quote_prices)
    vol_window: int = duration("5min")  # volatility of the hedge mid over this long, for vol_edge
    quote_latency: int = duration("50ms")  # from seeing a book update to the new quote being live on the maker exchange
    hedge_latency: int = duration("50ms")  # from seeing our fill to the hedge order filling
    fill: str = "through"  # see scalpr_strategy.filled
    basis_window: int = duration("1h")
    warmup: int = duration("30min")  # no quotes until the basis has this much history, as in the backtest
    stale: int = duration("10s")  # quotes are pulled when nothing came from an exchange this long (heartbeats included)
    markout: int = duration("1min")
    maker_fee: float | None = None  # % per maker fill on the quoted exchange; None: the standard tier of scalpr_strategy.FEES
    hedge_fee: float | None = None  # % per taker hedge; None: scalpr_strategy.FEES

    @property
    def hedge(self) -> str:
        return "bybit" if self.maker == "htx" else "htx"

    # % of the maker fill and of its hedge: the account's when the config gives them
    @property
    def fees(self) -> tuple[float, float]:
        return (FEES[self.maker]["maker"] if self.maker_fee is None else self.maker_fee,
                FEES[self.hedge]["taker"] if self.hedge_fee is None else self.hedge_fee)


def load(path: Path) -> Config:
    with open(path, "rb") as file:
        raw = tomllib.load(file)
    defaults = raw.get("trader", {})
    def setting(coin: dict, name: str, default: float) -> float:
        return float(coin.get(name, defaults.get(name, default)))

    coins = tuple(
        Coin(ticker, float(coin["edge"]), setting(coin, "notional", 100), setting(coin, "max_position", 300),
             setting(coin, "vol_edge", 0), setting(coin, "skew", 0))
        for ticker, coin in raw.get("coins", {}).items()
    )
    if not coins:
        raise ValueError(f"{path}: no [coins.<TICKER>] sections")
    durations = {name: duration(defaults[name]) for name in ("quote_latency", "hedge_latency", "basis_window", "warmup", "stale", "markout", "vol_window")
                 if name in defaults}
    fees = {name: float(defaults[name]) for name in ("maker_fee", "hedge_fee") if name in defaults}
    config = Config(coins, maker=defaults.get("maker", "htx"), anchor=defaults.get("anchor", "fair"), fill=defaults.get("fill", "through"), **durations, **fees)
    if config.maker != "htx":
        raise ValueError("only maker = \"htx\" for now: quoting on bybit needs its price ticks")
    if config.fill not in FILL_RULES:
        raise ValueError(f"fill must be one of {FILL_RULES}")
    if config.anchor not in ANCHORS:
        raise ValueError(f"anchor must be one of {ANCHORS}")
    return config


# the config back from run.json of a journal (dataclasses.asdict of a Config)
def from_dict(raw: dict) -> Config:
    return Config(tuple(Coin(**coin) for coin in raw["coins"]), **{name: value for name, value in raw.items() if name != "coins"})
