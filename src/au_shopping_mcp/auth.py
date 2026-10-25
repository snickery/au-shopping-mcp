"""Interactive login for adapters that expose more when authenticated (e.g. Costco member prices).

`au-shopping-mcp login <site>` (or SITE=<site> au-shopping-mcp login) opens a REAL browser window
on the site's login page. You sign in by hand - including any 2FA - and when you press Enter the
session cookies are copied into the same cookie jar the HTTP fetch tier reads
(DATA_DIR/cookies-<domain>.txt), so subsequent scrapes run authenticated. The persistent browser
profile (DATA_DIR/browser-profile) also keeps the session for the Playwright tiers.

No credentials are stored by the server - only the resulting session cookies, in DATA_DIR.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import os
import sys
import time

from au_shopping_mcp.adapters.base import load_adapter
from au_shopping_mcp.core import config


async def _login(site: str) -> int:
    adapter = load_adapter(site)
    login_url = getattr(adapter, "login_url", None) or adapter.base_url
    domain = adapter.domain
    profile_dir = config.BROWSER_PROFILE_DIR
    os.makedirs(profile_dir, exist_ok=True)
    cookie_path = os.path.join(config.DATA_DIR, f"cookies-{domain}.txt")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright is required for interactive login: uv run playwright install chromium", file=sys.stderr)
        return 2

    print(f"Opening a browser for {site} ({login_url}).")
    print("Log in in the window (including any 2FA). When the page shows you as signed in,")
    print("come back here and press Enter to capture the session.\n")

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            profile_dir, headless=False, channel="chrome",
            user_agent=adapter.policy.user_agent, locale="en-AU", timezone_id="Australia/Sydney",
            viewport={"width": 1366, "height": 900}, args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"(couldn't preload {login_url}: {e}; navigate there yourself)")

        await asyncio.get_event_loop().run_in_executor(None, input, "Press Enter once you're logged in... ")

        cookies = await ctx.cookies()
        jar = http.cookiejar.MozillaCookieJar(cookie_path)
        n = 0
        for c in cookies:
            cd = c.get("domain", "")
            if domain not in cd:
                continue
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=c["name"], value=c["value"], port=None, port_specified=False,
                domain=cd, domain_specified=True, domain_initial_dot=cd.startswith("."),
                path=c.get("path", "/"), path_specified=True, secure=c.get("secure", False),
                expires=int(c["expires"]) if c.get("expires", -1) and c["expires"] > 0 else None,
                discard=False, comment=None, comment_url=None, rest={}, rfc2109=False,
            ))
            n += 1
        os.makedirs(os.path.dirname(cookie_path), exist_ok=True)
        jar.save(ignore_discard=True, ignore_expires=True)
        await ctx.close()

    print(f"\nSaved {n} {domain} cookies to {cookie_path}.")
    print(f"The session is now used by the {site} adapter's HTTP tier and the persistent browser profile.")
    return 0


def main(argv: list[str]) -> int:
    site = argv[0] if argv else config.SITE
    if not site:
        print("Usage: au-shopping-mcp login <site>  (or set SITE=<site>)", file=sys.stderr)
        return 2
    return asyncio.run(_login(site))
