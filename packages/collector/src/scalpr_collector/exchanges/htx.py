import asyncio
import json
import tarfile
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from itertools import chain
from pathlib import Path

import aiohttp
from pandas import DataFrame, DatetimeIndex, concat, read_csv, read_parquet, to_datetime
from scalpr_collector.http_client import retry_download, retry_fetch
from scalpr_collector.orderbook import Message, book_frame, load_frame, save_frame
from scalpr_collector.progress import track_lines
from scalpr_collector.storage import write_parquet
from scalpr_core.logger import logger
from scalpr_core.paths import DATA_DIR

# daily archives of the 150 level book (snapshot + updates) and of trades, sizes in contracts
# archive day is UTC+8: file for D covers [D-1 16:00, D 16:00) UTC
FEED_LEVELS = 150
ARCHIVES = {
    "order book": ("https://futures.htx.com/data/historical_data/futures/daily/orderbook/lv150/{inst}/{name}", "{inst}-l2orderbook-150lv-{day}.tar.gz"),
    "trades": ("https://futures.htx.com/data/historical_data/futures/daily/trades/{inst}/{name}", "{inst}-trades-{day}.zip"),
}
CONTRACT_INFO_URL = "https://api.hbdm.com/linear-swap-api/v1/swap_contract_info"
HISTORY_DIR = DATA_DIR / "history" / "htx"


# backtest DOM history
async def get_dom_history(session: aiohttp.ClientSession, ticker: str, day: date, depth: int = 20) -> DataFrame:
    """Top `depth` levels of the USDT perp book after every update during `day` (UTC), see orderbook.book_frame.

    ticker is bybit-style (ACEUSDT), sizes are converted from contracts to coins.
    """
    contract = f"{ticker.removesuffix('USDT')}-USDT"
    inst = f"{contract}-PERP"
    # parquet holds the UTC day, unlike the UTC+8 archives
    directory = HISTORY_DIR / inst
    stem = f"{inst}-{day}"
    frame = await asyncio.to_thread(load_frame, directory, stem, depth)
    if frame is not None:
        logger.info(f"Loaded htx {inst} order book for {day} from parquet")
        return frame

    paths, is_full_day = await _utc_day_archives(session, inst, day, "order book")
    contract_size = await _contract_size(session, contract)
    start_ms, end_ms = _utc_day_ms(day)
    messages = chain.from_iterable(_messages(path) for path in paths)
    # replay is cpu bound, keep it off the event loop
    frame = await asyncio.to_thread(book_frame, messages, depth, FEED_LEVELS, start_ms, end_ms, contract_size)
    # partial day isn't cached, so a later run picks up the full one
    if is_full_day:
        await asyncio.to_thread(save_frame, frame, directory, stem, depth)
    return frame


# backtest trades
async def get_trades_history(session: aiohttp.ClientSession, ticker: str, day: date) -> DataFrame:
    """Trades of the USDT perp during `day` (UTC): index ts, price, size in coins, side of the taker (buy/sell)."""
    contract = f"{ticker.removesuffix('USDT')}-USDT"
    inst = f"{contract}-PERP"
    cached = HISTORY_DIR / inst / f"{inst}-{day}_trades.parquet"
    if cached.exists():
        logger.info(f"Loaded htx {inst} trades for {day} from parquet")
        return await asyncio.to_thread(read_parquet, cached)

    paths, is_full_day = await _utc_day_archives(session, inst, day, "trades")
    contract_size = await _contract_size(session, contract)
    frame = await asyncio.to_thread(_trades_frame, paths, contract_size, *_utc_day_ms(day))
    if is_full_day:
        await asyncio.to_thread(write_parquet, frame, cached)
    return frame


# UTC day spans two UTC+8 archives, the second one appears only after 16:00 UTC of the next day
async def _utc_day_archives(session: aiohttp.ClientSession, inst: str, day: date, kind: str) -> tuple[list[Path], bool]:
    paths = [await _archive(session, inst, day, kind)]
    try:
        paths.append(await _archive(session, inst, day + timedelta(days=1), kind))
    except FileNotFoundError:
        logger.warning(f"htx {inst} {kind} archive for {day + timedelta(days=1)} is not published yet, data for {day} ends at 16:00 UTC")
    return paths, len(paths) == 2


async def _archive(session: aiohttp.ClientSession, inst: str, day: date, kind: str) -> Path:
    url, name = ARCHIVES[kind]
    name = name.format(inst=inst, day=day)
    path = HISTORY_DIR / inst / "archives" / name
    # downloads land in .part and are renamed when complete, so an existing file is a full archive
    if path.exists():
        logger.info(f"Using cached htx {inst} {kind} for {day} (UTC+8)")
    else:
        logger.info(f"Downloading htx {inst} {kind} for {day} (UTC+8)")
        await retry_download(session, url.format(inst=inst, name=name), path)
    return path


async def _contract_size(session: aiohttp.ClientSession, contract: str) -> float:
    info, _ = await retry_fetch(session, CONTRACT_INFO_URL, params={"contract_code": contract})
    return float(info["data"][0]["contract_size"])


def _utc_day_ms(day: date) -> tuple[int, int]:
    start = datetime.combine(day, datetime.min.time(), UTC)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def _messages(path: Path) -> Iterator[Message]:
    with tarfile.open(path) as archive:
        member = archive.next()
        with archive.extractfile(member) as file:
            for line in track_lines(file, path.name, member.size):
                message = json.loads(line)
                # ts is in microseconds
                yield int(message["ts"]) // 1000, message["action"] == "snapshot", message["bids"], message["asks"]


def _trades_frame(paths: list[Path], contract_size: float, start_ms: int, end_ms: int) -> DataFrame:
    parts = []
    for path in paths:
        with zipfile.ZipFile(path) as archive, archive.open(archive.infolist()[0]) as file:
            parts.append(read_csv(file, usecols=["px", "side", "size", "ts"]))
    raw = concat(parts)
    # ts is in milliseconds
    raw = raw[(raw["ts"] >= start_ms) & (raw["ts"] < end_ms)]
    return DataFrame(
        {"price": raw["px"].to_numpy(), "size": raw["size"].to_numpy() * contract_size, "side": raw["side"].to_numpy()},
        index=DatetimeIndex(to_datetime(raw["ts"], unit="ms", utc=True), name="ts"),
    ).sort_index()
