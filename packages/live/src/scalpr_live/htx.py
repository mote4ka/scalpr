import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import aiohttp
from yarl import URL

# HTX REST: USDT perps on api.hbdm.com, the api key itself on the spot host
# https://www.htx.com/en-us/opend/newApiPages/
REST = "https://api.hbdm.com"
SPOT_REST = "https://api.huobi.pro"
TIMEOUT = aiohttp.ClientTimeout(total=10)


class HtxError(Exception):
    def __init__(self, path: str, code: object, message: str):
        super().__init__(f"htx {path}: {code} {message}")
        self.code = code


# parameters sorted by name, values percent-encoded (the timestamp's ":" too)
def canonical(params: dict) -> str:
    return urlencode(sorted(params.items()), safe="", quote_via=quote)


# the signature covers method, host, path and the canonical query, base64 of HMAC-SHA256
def sign(secret: str, method: str, host: str, path: str, query: str) -> str:
    payload = f"{method}\n{host}\n{path}\n{query}"
    return base64.b64encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()).decode()


# the query of a signed request: auth parameters (+ the request's own for GET; a POST sends its json body apart)
def signed_query(key: str, secret: str, method: str, host: str, path: str, params: dict | None = None, now: datetime | None = None) -> str:
    auth = {
        "AccessKeyId": key, "SignatureMethod": "HmacSHA256", "SignatureVersion": "2",
        "Timestamp": (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S"),
    }
    query = canonical(auth | (params or {}))
    return f"{query}&Signature={quote(sign(secret, method, host, path, query), safe='')}"


# answers come in three shapes: {"status": "ok", "data"} (v1 perps and spot), {"code": 200, "data"} (v2, v3),
# errors as err_code/err_msg, err-code/err-msg or code/msg|message
def result(path: str, data: dict) -> object:
    if data.get("status") == "ok" or data.get("code") == 200:
        return data.get("data")
    code = data.get("err_code") or data.get("err-code") or data.get("code")
    message = data.get("err_msg") or data.get("err-msg") or data.get("msg") or data.get("message") or ""
    raise HtxError(path, code, message)


# HTX REST: one account's signed requests on one host; `latency` of the last request
class HtxRest:
    def __init__(self, session: aiohttp.ClientSession, key: str, secret: str, url: str = REST):
        self.session, self.key, self.secret, self.url = session, key, secret, url
        self.host = URL(url).host
        self.latency = 0.0  # s, round trip of the last request

    # raw: the whole answer, for public endpoints that put their payload next to "status" (ts, tick)
    async def get(self, path: str, params: dict | None = None, signed: bool = True, raw: bool = False) -> object:
        query = signed_query(self.key, self.secret, "GET", self.host, path, params) if signed else canonical(params or {})
        return await self._request("GET", path, query, None, raw)

    async def post(self, path: str, body: dict | None = None) -> object:
        query = signed_query(self.key, self.secret, "POST", self.host, path)
        return await self._request("POST", path, query, json.dumps(body or {}, separators=(",", ":")), False)

    async def _request(self, method: str, path: str, query: str, payload: str | None, raw: bool) -> object:
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        # encoded=True: the query goes out exactly as signed
        url = URL(f"{self.url}{path}" + (f"?{query}" if query else ""), encoded=True)
        start = time.perf_counter()
        async with self.session.request(method, url, data=payload, headers=headers, timeout=TIMEOUT) as response:
            text = await response.text()
        self.latency = time.perf_counter() - start
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise HtxError(path, response.status, text[:200]) from None
        return data if raw else result(path, data)
