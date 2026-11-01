import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import aiohttp
from yarl import URL

# Bybit v5 REST: https://bybit-exchange.github.io/docs/v5/guide
REST = "https://api.bybit.com"
RECV_WINDOW = 5000  # ms the server still takes a request after its timestamp
TIMEOUT = aiohttp.ClientTimeout(total=10)


class BybitError(Exception):
    def __init__(self, path: str, code: int | None, message: str):
        super().__init__(f"bybit {path}: {code} {message}")
        self.code = code


# the signature covers timestamp + key + recv window + the query string (GET) or the json body (POST), hex
def sign(secret: str, timestamp: int, key: str, payload: str, recv_window: int = RECV_WINDOW) -> str:
    return hmac.new(secret.encode(), f"{timestamp}{key}{recv_window}{payload}".encode(), hashlib.sha256).hexdigest()


# BYBIT REST: one account's signed requests; `result` of the answer or BybitError, `latency` of the last request
class BybitRest:
    def __init__(self, session: aiohttp.ClientSession, key: str, secret: str, url: str = REST):
        self.session, self.key, self.secret, self.url = session, key, secret, url
        self.latency = 0.0  # s, round trip of the last request

    async def get(self, path: str, params: dict | None = None, signed: bool = True) -> dict:
        query = urlencode(params or {})
        return await self._request("GET", path, query, query, signed)

    async def post(self, path: str, body: dict) -> dict:
        payload = json.dumps(body, separators=(",", ":"))
        return await self._request("POST", path, "", payload, True)

    async def _request(self, method: str, path: str, query: str, payload: str, signed: bool) -> dict:
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        if signed:
            timestamp = time.time_ns() // 1_000_000
            headers |= {
                "X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": str(timestamp), "X-BAPI-RECV-WINDOW": str(RECV_WINDOW),
                "X-BAPI-SIGN": sign(self.secret, timestamp, self.key, payload),
            }
        # encoded=True: the query goes out exactly as signed
        url = URL(f"{self.url}{path}" + (f"?{query}" if query else ""), encoded=True)
        start = time.perf_counter()
        async with self.session.request(method, url, data=payload if method == "POST" else None, headers=headers, timeout=TIMEOUT) as response:
            text = await response.text()
        self.latency = time.perf_counter() - start
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise BybitError(path, response.status, text[:200]) from None
        if data.get("retCode") != 0:
            raise BybitError(path, data.get("retCode"), data.get("retMsg", ""))
        return data["result"]
