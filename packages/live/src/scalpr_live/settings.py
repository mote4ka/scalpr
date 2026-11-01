import tomllib
from dataclasses import dataclass
from pathlib import Path

from scalpr_trader.config import duration


# LIVE SETTINGS: the [live] table of trader.toml, all optional; the strategy itself comes from [trader] and [coins]
@dataclass(frozen=True)
class Settings:
    stale: int = duration("3s")  # quotes come off when an exchange is this quiet (the paper trader waits 10 s)
    answer_timeout: int = duration("5s")  # a place or cancel the exchange hasn't answered this long halts quoting
    hedge_timeout: int = duration("5s")  # a hedgeable amount still owed this long halts quoting
    max_loss: float = 10.0  # USDT, quoting halts when the run's marked pnl falls below -max_loss
    hedge_slippage: float = 0.3  # %, an IOC hedge fills no further than this past the best price
    # an order stays while it is within these of the wanted one (orders.Tolerance): following every tick of the fair
    # price took ~3.5 requests/s per coin on a calm day and peaks of 41/s in the 29.10 crash, htx allows ~24/s for
    # the whole account. On the crash recording these gave 5.6x fewer requests and the best pnl (see .claude/rules/live.md)
    keep_inside: float = 0.03  # % closer to fair than wanted
    keep_outside: float = 0.1  # % further from fair
    keep_size: float = 0.3  # share of the wanted contracts missing
    max_requests: float = 10.0  # per second to the maker exchange, 0: no limit (trader.Budget)


NUMBERS = ("max_loss", "hedge_slippage", "keep_inside", "keep_outside", "keep_size", "max_requests")


def load_settings(path: Path) -> Settings:
    with open(path, "rb") as file:
        raw = tomllib.load(file).get("live", {})
    durations = {name: duration(raw[name]) for name in ("stale", "answer_timeout", "hedge_timeout") if name in raw}
    numbers = {name: float(raw[name]) for name in NUMBERS if name in raw}
    unknown = set(raw) - set(durations) - set(numbers)
    if unknown:
        raise ValueError(f"{path}: unknown [live] settings {sorted(unknown)}")
    return Settings(**durations, **numbers)
