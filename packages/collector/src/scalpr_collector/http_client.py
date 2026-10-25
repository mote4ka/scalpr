import json
from pathlib import Path

import aiohttp
from scalpr_core.logger import logger
from scalpr_core.retry import retry

from scalpr_collector.progress import track


# SESSION MANAGER CLASS
class SessionManager:
    def __init__(self, quote_cookie: bool = True):
        self.isSectionAlive = False
        self._session: aiohttp.ClientSession | None = None
        # quote_cookie=False sends cookie values as is (aiohttp wraps values with special chars in quotes by default)
        self._quoteCookie = quote_cookie
        logger.debug('SessionManager inited!')

    def _createSession(self) -> aiohttp.ClientSession:
        # cookie jar needs running event loop, so it's created together with session
        # trust_env: use HTTP(S)_PROXY from environment like curl does, aiohttp ignores it by default
        return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(quote_cookie=self._quoteCookie), trust_env=True)

    async def getSession(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = self._createSession()
            logger.debug(f'New session created: {self._session}')
            self.isSectionAlive = True
        return self._session

    async def updateSession(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = self._createSession()
        logger.debug(f'Session updated, new session: {self._session}')
        return self._session
    
    async def closeSession(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self.isSectionAlive = False
            logger.debug(f'ClientSession {self._session} closed')
            
            
            
# RETRY FETCH: async_fetch with retries
@retry(max_retries=3)
async def retry_fetch(session: aiohttp.ClientSession, url: str, params: dict = None, headers: dict = None, cookies: dict = None, proxies = None, timeout: int = 10, method: str = "GET", json_body: dict = None):
    return await async_fetch(session=session, url=url, params=params, headers=headers, cookies=cookies, proxies=proxies, timeout=timeout, method=method, json_body=json_body)


# ASYNC FETCH: one request without retries, for workers with own retry policy (buff and csm pools)
# method + json_body: POST with json body (bybit p2p api), GET without body by default
async def async_fetch(session: aiohttp.ClientSession, url: str, params: dict = None, headers: dict = None, cookies: dict = None, proxies = None, timeout: int = 10, method: str = "GET", json_body: dict = None):
    # try:

    logger.debug(f'Requesting {url}...')
    async with session.request(
        method,
        url,
        params=params,
        json=json_body,
        headers=headers,
        cookies=cookies, 
        proxy=proxies,
        timeout=aiohttp.ClientTimeout(total=timeout)
    ) as response:
        response.raise_for_status()
        
        logger.debug(f'Status: {response.status}. Request completed')
        
        # content_type=None: some apis (htx) send json as text/plain
        data = await response.json(content_type=None)
        headers = json.dumps(dict(response.headers))

        return data, headers

# RETRY DOWNLOAD: async_download with retries on network and server errors
# missing file (FileNotFoundError) is not retried: the archive is just not published yet
@retry(max_retries=3, exceptions=(aiohttp.ClientError, TimeoutError))
async def retry_download(session: aiohttp.ClientSession, url: str, path: Path, timeout: int = 60) -> Path:
    return await async_download(session=session, url=url, path=path, timeout=timeout)


# ASYNC DOWNLOAD: streams response body into file (history archives are up to hundreds of MB)
# timeout is per socket read, not total, so big files don't hit it
async def async_download(session: aiohttp.ClientSession, url: str, path: Path, timeout: int = 60) -> Path:
    logger.debug(f'Downloading {url}...')
    path.parent.mkdir(parents=True, exist_ok=True)
    # .part first: interrupted download never looks like a finished file
    part = path.with_name(path.name + '.part')
    async with session.get(url, timeout=aiohttp.ClientTimeout(sock_read=timeout)) as response:
        if response.status == 404:
            raise FileNotFoundError(f'Not found: {url}')
        response.raise_for_status()
        # 1 MB chunk writes are short enough to block the loop
        with open(part, 'wb') as file, track('download', path.name, response.content_length) as advance:  # noqa: ASYNC230
            async for chunk in response.content.iter_chunked(1 << 20):
                file.write(chunk)
                advance(len(chunk))
    part.replace(path)

    logger.debug(f'Saved {path}')
    return path
