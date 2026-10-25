import asyncio
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import aiohttp
from pandas import DataFrame, DatetimeIndex, read_csv, read_parquet, to_datetime
from scalpr_collector.http_client import retry_download
from scalpr_collector.orderbook import Message, book_frame, load_frame, save_frame
from scalpr_collector.progress import track_lines
from scalpr_collector.storage import write_parquet
from scalpr_core.logger import logger
from scalpr_core.paths import DATA_DIR

# daily archives of the orderbook.200 ws feed (snapshot + 100ms deltas), one UTC day per file
# https://www.bybit.com/derivatives/en/history-data
ARCHIVE_URL = "https://quote-saver.bycsi.com/orderbook/linear/{ticker}/{name}"
# daily trades, one UTC day per file: https://public.bybit.com/trading/
TRADES_URL = "https://public.bybit.com/trading/{ticker}/{name}"
FEED_LEVELS = 200
HISTORY_DIR = DATA_DIR / "history" / "bybit"


# backtest DOM history
async def get_dom_history(session: aiohttp.ClientSession, ticker: str, day: date, depth: int = 20) -> DataFrame:
    """Top `depth` levels of the USDT perp book after every update during `day` (UTC), see orderbook.book_frame."""
    directory = HISTORY_DIR / ticker
    stem = f"{day}_{ticker}"
    frame = await asyncio.to_thread(load_frame, directory, stem, depth)
    if frame is not None:
        logger.info(f"Loaded bybit {ticker} order book for {day} from parquet")
        return frame

    name = f"{stem}_ob{FEED_LEVELS}.data.zip"
    path = directory / "archives" / name
    # downloads land in .part and are renamed when complete, so an existing file is a full archive
    if path.exists():
        logger.info(f"Using cached bybit {ticker} order book for {day}")
    else:
        logger.info(f"Downloading bybit {ticker} order book for {day}")
        await retry_download(session, ARCHIVE_URL.format(ticker=ticker, name=name), path)

    start = datetime.combine(day, datetime.min.time(), UTC)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((start + timedelta(days=1)).timestamp() * 1000)
    # replay is cpu bound, keep it off the event loop
    frame = await asyncio.to_thread(book_frame, _messages(path), depth, FEED_LEVELS, start_ms, end_ms)
    await asyncio.to_thread(save_frame, frame, directory, stem, depth)
    return frame


# backtest trades
async def get_trades_history(session: aiohttp.ClientSession, ticker: str, day: date) -> DataFrame:
    """Trades of the USDT perp during `day` (UTC): index ts, price, size in coins, side of the taker (buy/sell)."""
    directory = HISTORY_DIR / ticker
    cached = directory / f"{day}_{ticker}_trades.parquet"
    if cached.exists():
        logger.info(f"Loaded bybit {ticker} trades for {day} from parquet")
        return await asyncio.to_thread(read_parquet, cached)

    name = f"{ticker}{day}.csv.gz"
    path = directory / "archives" / name
    if path.exists():
        logger.info(f"Using cached bybit {ticker} trades for {day}")
    else:
        logger.info(f"Downloading bybit {ticker} trades for {day}")
        await retry_download(session, TRADES_URL.format(ticker=ticker, name=name), path)

    frame = await asyncio.to_thread(_trades_frame, path)
    await asyncio.to_thread(write_parquet, frame, cached)
    return frame


def _trades_frame(path: Path) -> DataFrame:
    raw = read_csv(path)
    # RPI trades fill retail-only orders that aren't in the public book, a regular maker order never gets them
    if "RPI" in raw:
        raw = raw[raw["RPI"] == 0]
    # timestamp is in seconds with a fraction
    ts = to_datetime((raw["timestamp"] * 1000).round().astype("int64"), unit="ms", utc=True)
    return DataFrame(
        {"price": raw["price"].to_numpy(), "size": raw["size"].to_numpy(), "side": raw["side"].str.lower().to_numpy()},
        index=DatetimeIndex(ts, name="ts"),
    ).sort_index()


def _messages(path: Path) -> Iterator[Message]:
    with zipfile.ZipFile(path) as archive:
        info = archive.infolist()[0]
        with archive.open(info) as file:
            for line in track_lines(file, path.name, info.file_size):
                message = json.loads(line)
                data = message["data"]
                yield message["ts"], message["type"] == "snapshot", data["b"], data["a"]


# i know how make real time dom capture with wss, but how scanner will be collecting this info in future?
# call a function every tick?

async def test():
    async with aiohttp.ClientSession(trust_env=True) as session:
        df = await get_dom_history(session, "BTCUSDT", date(2026, 10, 20), depth=20)
        
        print(df)
        
if __name__ == "__main__":
    asyncio.run(test())