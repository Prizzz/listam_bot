"""Page fetching through a real (headed) Chromium that passes the Cloudflare challenge.

On a VPS this runs under Xvfb (see Dockerfile). The browser profile is persistent so the
Cloudflare clearance cookie survives restarts.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # playwright is optional: the light image runs without it
    from playwright.async_api import BrowserContext, Playwright

log = logging.getLogger("listam.fetcher")


class Blocked(Exception):
    """Cloudflare did not let us through."""


class Fetcher:
    def __init__(self, profile_dir: str, proxy: Optional[str] = None, challenge_timeout: int = 40):
        self.profile_dir = profile_dir
        self.proxy = proxy
        self.challenge_timeout = challenge_timeout
        self._pw: Optional[Playwright] = None
        self._ctx: Optional[BrowserContext] = None
        self._lock = asyncio.Lock()

    async def _start(self) -> None:
        from playwright.async_api import async_playwright  # imported lazily on purpose

        self._pw = await async_playwright().start()
        kwargs = dict(
            user_data_dir=self.profile_dir,
            headless=False,  # headless is rejected by Cloudflare; Xvfb provides a display
            locale="en-US",
            viewport={"width": 1366, "height": 850},
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        if self.proxy:
            kwargs["proxy"] = {"server": self.proxy}
        self._ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
        log.info("browser started")

    async def close(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self._ctx = self._pw = None

    async def _restart(self) -> None:
        await self.close()
        await self._start()

    async def get(self, url: str) -> str:
        """Return page HTML. Raises Blocked if the challenge isn't passed."""
        async with self._lock:
            if self._ctx is None:
                await self._start()
            try:
                return await self._get(url)
            except Blocked:
                raise
            except Exception:
                log.exception("browser error, restarting")
                await self._restart()
                return await self._get(url)

    async def _get(self, url: str) -> str:
        page = await self._ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            waited = 0.0
            while waited < self.challenge_timeout:
                title = (await page.title()).lower()
                if "just a moment" not in title and "attention required" not in title:
                    break
                await asyncio.sleep(2)
                waited += 2
            else:
                raise Blocked("Cloudflare challenge not passed (status %s)" % (resp.status if resp else "?"))
            if resp is not None and resp.status >= 400 and waited == 0:
                raise Blocked("HTTP %s" % resp.status)
            await asyncio.sleep(random.uniform(0.8, 2.0))  # let lazy content settle
            return await page.content()
        finally:
            await page.close()


class HttpFetcher:
    """Light fetcher (~40 MB): a plain HTTP client with a Chrome TLS fingerprint, no browser at all.

    Cloudflare still answers some requests with a challenge (about every third one in tests);
    those pass on a retry a few seconds later, so each request is retried before giving up.
    """

    RETRY_STATUS = {403, 429, 503}

    def __init__(self, proxy: Optional[str] = None, attempts: int = 6, impersonate: str = "chrome131"):
        from curl_cffi import requests as cr  # imported lazily: optional dependency

        self._cr = cr
        self.proxy = proxy
        self.attempts = attempts
        self.impersonate = impersonate
        self._session = None
        self._lock = asyncio.Lock()

    def _new_session(self):
        kw = {"proxies": {"http": self.proxy, "https": self.proxy}} if self.proxy else {}
        return self._cr.AsyncSession(impersonate=self.impersonate, **kw)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get(self, url: str) -> str:
        async with self._lock:
            last = "no response"
            for attempt in range(1, self.attempts + 1):
                if self._session is None:
                    self._session = self._new_session()
                try:
                    r = await self._session.get(url, timeout=30, headers={"Accept-Language": "en-US,en;q=0.9"})
                    if r.status_code not in self.RETRY_STATUS:
                        return r.text  # includes 404 for removed ads: the parser copes with it
                    last = "HTTP %d (%s)" % (r.status_code, r.headers.get("cf-mitigated") or "denied")
                except Exception as e:  # network hiccup, timeout, TLS reset
                    last = "%s: %s" % (type(e).__name__, e)
                    await self.close()
                if attempt < self.attempts:
                    await asyncio.sleep(random.uniform(2, 6))
            raise Blocked("list.am: blocked after %d attempts (%s)" % (self.attempts, last))
