import asyncio
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import aiohttp
from pandas import DataFrame
from scalpr_collector.http_client import retry_download
from scalpr_collector.orderbook import Message, book_frame
from scalpr_core.logger import logger
from scalpr_core.paths import DATA_DIR

# daily archives of the orderbook.200 ws feed (snapshot + 100ms deltas), one UTC day per file
# https://www.bybit.com/derivatives/en/history-data
ARCHIVE_URL = "https://quote-saver.bycsi.com/orderbook/linear/{ticker}/{name}"
FEED_LEVELS = 200
HISTORY_DIR = DATA_DIR / "history" / "bybit"


# backtest DOM history
async def get_dom_history(session: aiohttp.ClientSession, ticker: str, day: date, depth: int = 20) -> DataFrame:
    """Top `depth` levels of the USDT perp book after every update during `day` (UTC), see orderbook.book_frame."""
    name = f"{day}_{ticker}_ob{FEED_LEVELS}.data.zip"
    path = HISTORY_DIR / ticker / name
    if not path.exists():
        logger.info(f"Downloading bybit {ticker} order book for {day}")
        await retry_download(session, ARCHIVE_URL.format(ticker=ticker, name=name), path)

    start = datetime.combine(day, datetime.min.time(), UTC)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((start + timedelta(days=1)).timestamp() * 1000)
    # replay is cpu bound, keep it off the event loop
    return await asyncio.to_thread(book_frame, _messages(path), depth, FEED_LEVELS, start_ms, end_ms)


def _messages(path: Path) -> Iterator[Message]:
    with zipfile.ZipFile(path) as archive, archive.open(archive.namelist()[0]) as file:
        for line in file:
            message = json.loads(line)
            data = message["data"]
            yield message["ts"], message["type"] == "snapshot", data["b"], data["a"]


# i know how make real time dom capture with wss, but how scanner will be collecting this info in future?
# call a function every tick?
