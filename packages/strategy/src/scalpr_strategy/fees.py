MODES = ("maker", "taker")
# % per trade, standard (non-VIP) tier: check your account and update
FEES = {
    "bybit": {"maker": 0.02, "taker": 0.055},
    "htx": {"maker": 0.02, "taker": 0.05},
}


# ROUND TRIP FEES: every exchange trades twice, on entry and on exit
def round_trip_fees(modes: dict[str, str]) -> float:
    return 2 * sum(FEES[exchange][mode] for exchange, mode in modes.items())
