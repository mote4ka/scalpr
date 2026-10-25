import asyncio
import json
import tarfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from itertools import chain
from pathlib import Path

import aiohttp
from pandas import DataFrame
from scalpr_collector.http_client import retry_download, retry_fetch
from scalpr_collector.orderbook import Message, book_frame
from scalpr_core.logger import logger
from scalpr_core.paths import DATA_DIR

# daily archives of the 150 level book (snapshot + updates), sizes in contracts
# archive day is UTC+8: file for D covers [D-1 16:00, D 16:00) UTC
ARCHIVE_URL = "https://futures.htx.com/data/historical_data/futures/daily/orderbook/lv150/{inst}/{name}"
CONTRACT_INFO_URL = "https://api.hbdm.com/linear-swap-api/v1/swap_contract_info"
FEED_LEVELS = 150
HISTORY_DIR = DATA_DIR / "history" / "htx"


# backtest DOM history
async def get_dom_history(session: aiohttp.ClientSession, ticker: str, day: date, depth: int = 20) -> DataFrame:
    """Top `depth` levels of the USDT perp book after every update during `day` (UTC), see orderbook.book_frame.

    ticker is bybit-style (ACEUSDT), sizes are converted from contracts to coins.
    """
    contract = f"{ticker.removesuffix('USDT')}-USDT"
    # UTC day spans two UTC+8 archives
    paths = [await _archive(session, f"{contract}-PERP", d) for d in (day, day + timedelta(days=1))]

    info, _ = await retry_fetch(session, CONTRACT_INFO_URL, params={"contract_code": contract})
    contract_size = float(info["data"][0]["contract_size"])

    start = datetime.combine(day, datetime.min.time(), UTC)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((start + timedelta(days=1)).timestamp() * 1000)
    messages = chain.from_iterable(_messages(path) for path in paths)
    # replay is cpu bound, keep it off the event loop
    return await asyncio.to_thread(book_frame, messages, depth, FEED_LEVELS, start_ms, end_ms, contract_size)


async def _archive(session: aiohttp.ClientSession, inst: str, day: date) -> Path:
    name = f"{inst}-l2orderbook-{FEED_LEVELS}lv-{day}.tar.gz"
    path = HISTORY_DIR / inst / name
    if not path.exists():
        logger.info(f"Downloading htx {inst} order book for {day} (UTC+8)")
        await retry_download(session, ARCHIVE_URL.format(inst=inst, name=name), path)
    return path


def _messages(path: Path) -> Iterator[Message]:
    with tarfile.open(path) as archive, archive.extractfile(archive.next()) as file:
        for line in file:
            message = json.loads(line)
            # ts is in microseconds
            yield int(message["ts"]) // 1000, message["action"] == "snapshot", message["bids"], message["asks"]
