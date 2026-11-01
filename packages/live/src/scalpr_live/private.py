import asyncio
import gzip
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp
from scalpr_core.logger import logger
from yarl import URL

from scalpr_live.htx import canonical
from scalpr_live.htx import sign as htx_sign

# private websockets: what happens to our orders, as the exchanges push it
HTX_PRIVATE = "wss://api.hbdm.com/ws/v5/notification"  # multi-asset accounts (V5)
BYBIT_PRIVATE = "wss://stream.bybit.com/v5/private"
RECEIVE_TIMEOUT = 30  # s without any message: the connection is dead
BYBIT_PING = 20  # s, bybit drops a private connection quiet for 10 min and asks for a ping every 20 s
MAX_BACKOFF = 30


# one order's state on an exchange, cumulative: the fills since the last update are the difference
@dataclass(frozen=True)
class OrderUpdate:
    exchange: str
    client_id: str  # ours: htx client_order_id, bybit orderLinkId
    ticker: str  # bybit-style
    state: str  # new / partially_filled / filled / canceled / rejected
    filled: float  # htx contracts, bybit coins
    value: float  # USDT of those fills
    final: bool  # nothing more will happen to the order
    received: int  # local time, ns


HTX_STATES = {"new": "new", "partially_filled": "partially_filled", "filled": "filled", "canceled": "canceled", "partially_canceled": "canceled"}
BYBIT_STATES = {"New": "new", "PartiallyFilled": "partially_filled", "Filled": "filled", "Cancelled": "canceled",
                "PartiallyFilledCanceled": "canceled", "Rejected": "rejected", "Deactivated": "canceled"}


# {"op": "notify", "topic": "orders", "data": {...}}: final once trade_volume + cancel_volume == volume (several fills
# of one match all say "filled" before the volume adds up)
def htx_update(message: dict, received: int) -> OrderUpdate | None:
    if message.get("op") != "notify" or message.get("topic") != "orders":
        return None
    data = message["data"]
    if not data.get("client_order_id"):
        return None
    filled, volume = float(data.get("trade_volume") or 0), float(data.get("volume") or 0)
    state = HTX_STATES.get(data["state"], data["state"])
    if "cancel_volume" in data and volume:
        complete = filled + float(data["cancel_volume"] or 0) >= volume - 1e-9
    else:
        complete = state == "canceled" or filled >= volume - 1e-9
    return OrderUpdate("htx", str(data["client_order_id"]), data["contract_code"].replace("-", ""), state, filled,
                       float(data.get("trade_turnover") or 0), complete and state in ("filled", "canceled"), received)


# {"topic": "order", "data": [{...}, ...]}
def bybit_updates(message: dict, received: int) -> list[OrderUpdate]:
    if message.get("topic") != "order":
        return []
    updates = []
    for data in message.get("data", []):
        if data.get("category") != "linear" or not data.get("orderLinkId"):
            continue
        state = BYBIT_STATES.get(data["orderStatus"], data["orderStatus"])
        updates.append(OrderUpdate("bybit", data["orderLinkId"], data["symbol"], state, float(data.get("cumExecQty") or 0),
                                   float(data.get("cumExecValue") or 0), state in ("filled", "canceled", "rejected"), received))
    return updates


# PRIVATE FEED: one authenticated connection kept up (reconnects with backoff); `ready` while authenticated and
# subscribed, `last` is when anything last came
class PrivateFeed:
    exchange: str
    url: str

    def __init__(self, key: str, secret: str):
        self.key, self.secret = key, secret
        self.ready = False
        self.last = 0  # ns
        self.reconnects = 0
        self.error: str | None = None  # why the last connection ended

    async def run(self, session: aiohttp.ClientSession, on_update: Callable[[OrderUpdate], None]) -> None:
        backoff = 1
        while True:
            try:
                async with session.ws_connect(self.url, heartbeat=None, receive_timeout=RECEIVE_TIMEOUT, max_msg_size=0) as ws:
                    await self.login(ws)
                    backoff = 1
                    keepalive = asyncio.create_task(self.keepalive(ws))
                    try:
                        async for raw in ws:
                            if raw.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                                break
                            self.last = time.time_ns()
                            for update in await self.handle(ws, _decode(raw.data), self.last):
                                on_update(update)
                    finally:
                        keepalive.cancel()
            except (aiohttp.ClientError, TimeoutError, ConnectionError, PermissionError) as error:
                self.error = repr(error)
                logger.warning(f"{self.exchange} private: connection lost, {error!r}")
            self.ready = False
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def login(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        raise NotImplementedError

    async def handle(self, ws: aiohttp.ClientWebSocketResponse, message: dict, received: int) -> list[OrderUpdate]:
        raise NotImplementedError

    async def keepalive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await asyncio.Event().wait()


# HTX V5: auth signed like a GET of the notification path, then the orders of every contract; the server pings
class HtxPrivate(PrivateFeed):
    exchange, url = "htx", HTX_PRIVATE

    def auth_message(self, now: float | None = None) -> dict:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now or time.time()))
        params = {"AccessKeyId": self.key, "SignatureMethod": "HmacSHA256", "SignatureVersion": "2", "Timestamp": timestamp}
        url = URL(self.url)
        signature = htx_sign(self.secret, "GET", url.host, url.path, canonical(params))
        return {"op": "auth", "type": "api", **params, "Signature": signature}

    async def login(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.send_json(self.auth_message())

    async def handle(self, ws: aiohttp.ClientWebSocketResponse, message: dict, received: int) -> list[OrderUpdate]:
        op = message.get("op")
        if op == "ping":
            await ws.send_json({"op": "pong", "ts": message.get("ts")})
        elif op == "auth":
            if message.get("err-code", 0) != 0:
                raise PermissionError(f"htx private auth: {message.get('err-msg') or message}")
            await ws.send_json({"op": "sub", "cid": "orders", "topic": "orders", "contract_code": "*"})
        elif op == "sub":
            if message.get("err-code", 0) != 0:
                raise PermissionError(f"htx private subscribe: {message.get('err-msg') or message}")
            self.ready = True
        elif op == "close" or op == "error":
            raise ConnectionError(f"htx private: {message}")
        else:
            update = htx_update(message, received)
            return [update] if update is not None else []
        return []


# BYBIT v5: auth with an expiring signature of "GET/realtime<expires>", then the order topic; we ping every 20 s
class BybitPrivate(PrivateFeed):
    exchange, url = "bybit", BYBIT_PRIVATE

    def auth_message(self, now: float | None = None) -> dict:
        expires = int(((now or time.time()) + 10) * 1000)
        signature = hmac.new(self.secret.encode(), f"GET/realtime{expires}".encode(), hashlib.sha256).hexdigest()
        return {"op": "auth", "args": [self.key, expires, signature]}

    async def login(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.send_json(self.auth_message())

    async def handle(self, ws: aiohttp.ClientWebSocketResponse, message: dict, received: int) -> list[OrderUpdate]:
        op = message.get("op")
        if op == "auth":
            if not message.get("success"):
                raise PermissionError(f"bybit private auth: {message.get('ret_msg') or message}")
            await ws.send_json({"op": "subscribe", "args": ["order"]})
        elif op == "subscribe":
            if not message.get("success"):
                raise PermissionError(f"bybit private subscribe: {message.get('ret_msg') or message}")
            self.ready = True
        elif op in ("pong", "ping"):
            pass
        else:
            return bybit_updates(message, received)
        return []

    async def keepalive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(BYBIT_PING)
            await ws.send_json({"op": "ping"})


# htx sends gzipped binary frames, bybit plain text
def _decode(data: str | bytes) -> dict:
    if isinstance(data, bytes):
        data = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    return json.loads(data)
