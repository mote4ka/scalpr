import asyncio
from datetime import UTC, date, datetime, timedelta

import aiohttp
from pandas import DataFrame
from scalpr_collector.exchanges import bybit, htx

EXCHANGES = {"bybit": bybit, "htx": htx}
# htx publishes day D after 16:00 UTC of D+1: the day before yesterday is the last one surely complete
PUBLISH_LAG = timedelta(days=2)


def last_complete_day() -> date:
    return datetime.now(UTC).date() - PUBLISH_LAG


# LOAD DAY: books of both exchanges and, with a maker leg, the trades tape of the maker exchange
async def load_day(session: aiohttp.ClientSession, ticker: str, day: date, depth: int, maker: str | None) -> tuple[DataFrame, DataFrame, DataFrame | None]:
    # all at once: downloads overlap, each gets its own progress row
    jobs = [bybit.get_dom_history(session, ticker, day, depth), htx.get_dom_history(session, ticker, day, depth)]
    if maker is not None:
        jobs.append(EXCHANGES[maker].get_trades_history(session, ticker, day))
    bybit_book, htx_book, *tape = await asyncio.gather(*jobs)
    return bybit_book, htx_book, tape[0] if tape else None
