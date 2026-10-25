"""Fetch ladder: HTTP -> BROWSER -> STEALTH, escalating on detected blocks.

The fetcher remembers, per domain, the lowest tier that last succeeded and starts
there next time (decaying back down after a quiet period), so a site that needs
Playwright doesn't waste an httpx round-trip on every call, and a site that
stopped blocking doesn't stay on the expensive path forever.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from au_shopping_mcp.core import config
from au_shopping_mcp.core.config import log_event
from au_shopping_mcp.core.models import BlockedError, FetchPolicy, FetchResult, FetchTier
from au_shopping_mcp.core.politeness import DomainLimiter

log = logging.getLogger("au_shopping_mcp.fetch")

# Signals that a "200 OK" is really a challenge page.
_CHALLENGE_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"<title>\s*just a moment",           # Cloudflare
        r"cf-browser-verification",
        r"challenge-platform",
        r"<title>\s*access denied",
        r"<title>\s*attention required",       # Cloudflare block
        r"perimeterx|px-captcha|_pxhd",        # PerimeterX
        r"akamai.*bot manager|ak_bmsc",        # Akamai (weak signal, header check better)
        r"datadome",
        r"are you a (human|robot)",
    )
]
_BLOCK_STATUSES = {403, 429, 503, 401}
_TIER_DECAY_S = 30 * 60  # after 30 min quiet, try one tier lower again


def looks_blocked(status: int, body: str, headers: dict[str, str]) -> str | None:
    """Return a reason string if this response is a block/challenge, else None."""
    if status in _BLOCK_STATUSES:
        return f"http {status}"
    if headers.get("cf-mitigated") == "challenge":
        return "cf-mitigated header"
    head = body[:8000]
    for pat in _CHALLENGE_PATTERNS:
        if pat.search(head):
            return f"challenge marker: {pat.pattern}"
    return None


class _Browsers:
    """One Playwright driver, one headless Chromium and one persistent stealth context per
    process, shared by every site's Fetcher. A Chromium profile directory can only be open
    once, so per-site stealth contexts would collide when one process serves several sites."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.pw = None
        self.browser = None
        self.stealth_ctx = None


_BROWSERS = _Browsers()


async def close_browsers() -> None:
    b = _BROWSERS
    if b.stealth_ctx:
        await b.stealth_ctx.close()
    if b.browser:
        await b.browser.close()
    if b.pw:
        await b.pw.stop()
    b.pw = b.browser = b.stealth_ctx = None


class Fetcher:
    def __init__(self, policy: FetchPolicy, domain: str):
        self.policy = policy
        self.domain = domain
        self.limiter = DomainLimiter(policy)
        self._tier = policy.start_tier
        self._tier_set_at = time.monotonic()
        self._client: httpx.AsyncClient | None = None
        self.last_block: dict[str, Any] | None = None
        self.stats = {"fetches": 0, "escalations": 0, "blocks": 0, "by_tier": {t.name: 0 for t in FetchTier}}
        self._cookie_path = os.path.join(config.DATA_DIR, f"cookies-{domain}.txt")

    # ---------- state ----------
    @property
    def current_tier(self) -> FetchTier:
        # decay back toward start_tier after a quiet period
        if self._tier > self.policy.start_tier and time.monotonic() - self._tier_set_at > _TIER_DECAY_S:
            self._tier = FetchTier(self._tier - 1)
            self._tier_set_at = time.monotonic()
        return self._tier

    def _set_tier(self, tier: FetchTier) -> None:
        self._tier = tier
        self._tier_set_at = time.monotonic()

    @property
    def ceiling(self) -> FetchTier:
        return min(self.policy.max_tier, config.MAX_TIER)

    # ---------- public ----------
    async def fetch(self, url: str, *, tier: FetchTier | None = None, headers: dict[str, str] | None = None,
                    cookies: dict[str, str] | None = None) -> FetchResult:
        """Fetch a page, escalating tiers on block. Raises BlockedError if the ceiling is hit.

        `cookies` are site-scoping cookies the adapter wants sent on every tier (e.g. a selected
        store). They go into the cookie jar / browser context rather than a raw `Cookie:` header,
        because httpx replaces a hand-built Cookie header with the jar's contents once the jar
        holds anything for the domain."""
        t = tier or max(self.current_tier, FetchTier.HTTP)
        last_err: BlockedError | None = None
        while t <= self.ceiling:
            try:
                res = await self._fetch_at(t, url, headers or {}, cookies or {})
                self.stats["fetches"] += 1
                self.stats["by_tier"][t.name] += 1
                if t != self.current_tier:
                    self._set_tier(t)
                log_event(log, "fetch", domain=self.domain, url=url, tier=t.name, status=res.status, elapsed_ms=res.elapsed_ms)
                return res
            except BlockedError as e:
                last_err = e
                self.stats["blocks"] += 1
                self.last_block = {"at": time.time(), "tier": t.name, "reason": e.reason, "url": url}
                log_event(log, "blocked", logging.WARNING, domain=self.domain, url=url, tier=t.name, reason=e.reason)
                if t >= self.ceiling:
                    break
                self.stats["escalations"] += 1
                t = FetchTier(t + 1)
        raise last_err or BlockedError("no tier succeeded", t)

    async def fetch_json(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None, json_body: Any = None) -> FetchResult:
        """Tier 0: hit a site's own JSON endpoint directly. No escalation - if this is blocked,
        the adapter should fall back to fetch() on the HTML page instead."""
        client = await self._http_client()
        start = time.monotonic()
        async with self.limiter:
            r = await client.request(method, url, headers=headers, json=json_body)
        body = r.text
        reason = looks_blocked(r.status_code, body, dict(r.headers))
        if reason:
            raise BlockedError(reason, FetchTier.API, r.status_code)
        data = None
        try:
            data = r.json()
        except json.JSONDecodeError:
            pass
        self.stats["fetches"] += 1
        self.stats["by_tier"]["API"] += 1
        return FetchResult(url=url, final_url=str(r.url), status=r.status_code, tier=FetchTier.API, body=body,
                           headers=dict(r.headers), elapsed_ms=int((time.monotonic() - start) * 1000), json_data=data)

    async def fetch_capture(self, page_url: str, match: str, *, timeout_s: float | None = None) -> FetchResult:
        """Browser tier, but return a JSON response the PAGE fetches (not the page HTML).

        Loads page_url in Chromium and returns the body of the first same-load XHR/fetch whose
        URL contains `match` and is JSON. For SPA storefronts whose product data comes from a
        third-party search API with a session-locked key (e.g. an Algolia key that 403s when
        replayed directly), letting the real page make the call is the reliable path. Runs
        through the persistent-block detector on the PAGE (not the captured call), so a challenge
        page still raises BlockedError.
        """
        ctx = await self._ensure_browser(stealth=False)
        page = await ctx.new_page()
        captured: list[Any] = []
        to = (timeout_s or self.policy.timeout_s)

        async def on_resp(resp):
            if match not in resp.url or "json" not in resp.headers.get("content-type", ""):
                return
            try:
                captured.append(await resp.json())
            except Exception:
                pass

        page.on("response", on_resp)
        start = time.monotonic()
        try:
            async with self.limiter:
                resp = await page.goto(page_url, wait_until="domcontentloaded", timeout=to * 1000)
                status = resp.status if resp else 0
                # wait for the first match, then a short settle window to collect siblings
                waited = 0.0
                while not captured and waited < to:
                    await page.wait_for_timeout(300)
                    waited += 0.3
                await page.wait_for_timeout(1500)
                body = await page.content()
            reason = looks_blocked(status, body, dict(resp.headers) if resp else {})
            if reason and not captured:
                raise BlockedError(reason, FetchTier.BROWSER, status)
            if not captured:
                raise BlockedError(f"no {match!r} JSON response captured within {to:.0f}s", FetchTier.BROWSER, status)
            return FetchResult(url=page_url, final_url=page.url, status=200,
                               tier=FetchTier.BROWSER, body="", json_data=captured,
                               elapsed_ms=int((time.monotonic() - start) * 1000))
        finally:
            await page.close()
            await ctx.close()

    async def fetch_dom(self, url: str, extract_js: str, *, wait_selector: str | None = None,
                        timeout_s: float | None = None, cookies: dict[str, str] | None = None) -> FetchResult:
        """Browser tier: render the page, run `extract_js` in it, return its result as json_data.

        For SPA storefronts whose useful data is only in the rendered DOM (no stable JSON call to
        intercept). `extract_js` is a JS expression evaluated with page.evaluate - typically an
        arrow function (may be async) returning a list/dict. Waits for `wait_selector` (never
        networkidle - many storefronts poll analytics forever). `cookies` are site-scoping cookies
        (store, currency/region) added to the browser context, same contract as fetch()."""
        ctx = await self._ensure_browser(stealth=False)
        if cookies:
            # Jar/context-level, never a raw Cookie: header - site-scoping cookies (store,
            # currency/region) must survive the page's own Set-Cookie churn.
            await ctx.add_cookies([{"name": k, "value": v, "domain": f".{self.domain}", "path": "/"}
                                   for k, v in cookies.items()])
        page = await ctx.new_page()
        to = (timeout_s or self.policy.timeout_s)
        start = time.monotonic()
        try:
            async with self.limiter:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=to * 1000)
                status = resp.status if resp else 0
                if wait_selector:
                    try:
                        await page.wait_for_selector(wait_selector, timeout=to * 1000)
                    except Exception:
                        pass
                for _ in range(3):  # nudge lazy-loaded product grids into rendering
                    await page.mouse.wheel(0, 2400)
                    await page.wait_for_timeout(700)
                await page.wait_for_timeout(1200)
                data = await page.evaluate(extract_js)
                body = await page.content()
            reason = looks_blocked(status, body, dict(resp.headers) if resp else {})
            if reason:
                raise BlockedError(reason, FetchTier.BROWSER, status)
            return FetchResult(url=url, final_url=page.url, status=status, tier=FetchTier.BROWSER,
                               body=body, json_data=data, elapsed_ms=int((time.monotonic() - start) * 1000))
        finally:
            await page.close()
            await ctx.close()

    async def close(self) -> None:
        """Close this site's HTTP client. The shared browsers belong to the process (close_browsers)."""
        if self._client:
            await self._client.aclose()
            self._save_cookies()

    def status(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "current_tier": self.current_tier.name,
            "ceiling": self.ceiling.name,
            "last_block": self.last_block,
            "requests_issued": self.limiter.total_requests,
            **self.stats,
        }

    # ---------- tiers ----------
    async def _fetch_at(self, tier: FetchTier, url: str, headers: dict[str, str], cookies: dict[str, str]) -> FetchResult:
        if tier == FetchTier.HTTP:
            return await self._tier_http(url, headers, cookies)
        if tier == FetchTier.BROWSER:
            return await self._tier_browser(url, stealth=False, cookies=cookies)
        if tier == FetchTier.STEALTH:
            return await self._tier_browser(url, stealth=True, cookies=cookies)
        raise ValueError(f"unsupported tier {tier}")

    async def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            jar = http.cookiejar.MozillaCookieJar(self._cookie_path)
            if os.path.exists(self._cookie_path):
                try:
                    jar.load(ignore_discard=True, ignore_expires=True)
                except Exception:  # corrupt file - start fresh
                    pass
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": self.policy.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-AU,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate",
                    "Upgrade-Insecure-Requests": "1",
                },
                cookies=jar,
                follow_redirects=True,
                timeout=self.policy.timeout_s,
                http2=True,
            )
        return self._client

    def _save_cookies(self) -> None:
        try:
            jar = self._client.cookies.jar  # type: ignore[union-attr]
            if isinstance(jar, http.cookiejar.MozillaCookieJar):
                os.makedirs(os.path.dirname(self._cookie_path), exist_ok=True)
                jar.save(ignore_discard=True, ignore_expires=True)
        except Exception:
            log.debug("cookie save failed", exc_info=True)

    async def _tier_http(self, url: str, headers: dict[str, str], cookies: dict[str, str] | None = None) -> FetchResult:
        client = await self._http_client()
        for name, value in (cookies or {}).items():
            # Jar-level set: overrides whatever the site previously set under that name.
            client.cookies.set(name, value, domain=f".{self.domain}", path="/")
        start = time.monotonic()
        async with self.limiter:
            r = await client.get(url, headers=headers)
        body = r.text
        reason = looks_blocked(r.status_code, body, dict(r.headers))
        if reason:
            raise BlockedError(reason, FetchTier.HTTP, r.status_code)
        if self.policy.browser_wait_selector and self.policy.browser_wait_selector.lstrip("#.") not in body:
            # Server returned a JS shell without the content we need: not a block, but HTTP can't do it.
            raise BlockedError("content requires JS render", FetchTier.HTTP, r.status_code)
        self._save_cookies()
        return FetchResult(url=url, final_url=str(r.url), status=r.status_code, tier=FetchTier.HTTP, body=body,
                           headers=dict(r.headers), elapsed_ms=int((time.monotonic() - start) * 1000))

    async def _ensure_browser(self, stealth: bool):
        b = _BROWSERS
        async with b.lock:
            if b.pw is None:
                from playwright.async_api import async_playwright
                b.pw = await async_playwright().start()
            if stealth:
                if b.stealth_ctx is None:
                    os.makedirs(config.BROWSER_PROFILE_DIR, exist_ok=True)
                    b.stealth_ctx = await b.pw.chromium.launch_persistent_context(
                        config.BROWSER_PROFILE_DIR,
                        headless=True,
                        user_agent=self.policy.user_agent,
                        locale="en-AU",
                        timezone_id="Australia/Sydney",
                        viewport={"width": 1366, "height": 860},
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    await b.stealth_ctx.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    )
                return b.stealth_ctx
            if b.browser is None:
                b.browser = await b.pw.chromium.launch(headless=True)
            return await b.browser.new_context(user_agent=self.policy.user_agent, locale="en-AU")

    async def _tier_browser(self, url: str, *, stealth: bool, cookies: dict[str, str] | None = None) -> FetchResult:
        ctx = await self._ensure_browser(stealth)
        tier = FetchTier.STEALTH if stealth else FetchTier.BROWSER
        if cookies:
            await ctx.add_cookies([{"name": k, "value": v, "domain": f".{self.domain}", "path": "/"} for k, v in cookies.items()])
        page = await ctx.new_page()
        start = time.monotonic()
        try:
            async with self.limiter:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.policy.timeout_s * 1000)
                status = resp.status if resp else 0
                if self.policy.browser_wait_selector:
                    try:
                        await page.wait_for_selector(self.policy.browser_wait_selector, timeout=self.policy.timeout_s * 1000)
                    except Exception:
                        pass  # fall through; block detection below decides
                else:
                    await page.wait_for_load_state("networkidle", timeout=self.policy.timeout_s * 1000)
                body = await page.content()
                headers = dict(resp.headers) if resp else {}
            reason = looks_blocked(status, body, headers)
            if reason:
                raise BlockedError(reason, tier, status)
            return FetchResult(url=url, final_url=page.url, status=status, tier=tier, body=body, headers=headers,
                               elapsed_ms=int((time.monotonic() - start) * 1000))
        finally:
            await page.close()
            if not stealth:
                await ctx.close()


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")
