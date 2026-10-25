"""Adapter contract. server.py only talks to this interface."""
from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse

from au_shopping_mcp.core.extract import links, parse, readable_text
from au_shopping_mcp.core.fetch import Fetcher
from au_shopping_mcp.core.models import Category, FetchPolicy, Item, SearchPage
from au_shopping_mcp.core.politeness import Cache

log = logging.getLogger("au_shopping_mcp.adapter")


class SiteAdapter(ABC):
    """Subclass per site. Keep selectors/endpoints as class attrs so a redesign is a one-file fix."""

    name: str                      # "bunnings" - used for SITE env and tool prefix
    domain: str                    # "bunnings.com.au"
    base_url: str                  # "https://www.bunnings.com.au"
    policy: FetchPolicy = FetchPolicy()
    description: str = ""          # one-liner surfaced in site_status and the server instructions

    def __init__(self) -> None:
        self.fetcher = Fetcher(self.policy, self.domain)
        self.cache = Cache()

    # ---- required ----
    @abstractmethod
    async def search(self, query: str, *, page: int = 1, sort: str | None = None, filters: dict[str, str] | None = None) -> SearchPage: ...

    @abstractmethod
    async def get_item(self, id_or_url: str) -> Item: ...

    @abstractmethod
    def parse_url(self, url: str) -> dict[str, str] | None:
        """Classify a site URL: {"kind": "item"|"category"|"search", "id": ...} or None."""

    # ---- optional ----
    async def browse(self, category: str, *, page: int = 1, sort: str | None = None) -> SearchPage:
        raise NotImplementedError(f"{self.name} adapter does not support category browsing")

    async def categories(self, parent: str | None = None) -> list[Category]:
        raise NotImplementedError(f"{self.name} adapter does not expose a category tree")

    def sort_options(self) -> list[str]:
        return []

    def site_specific_status(self) -> dict[str, Any]:
        return {}

    # ---- generic, works for any adapter ----
    def offsite_reason(self, url: str) -> str | None:
        """Why a caller-supplied URL is refused, or None if it is on this site.

        Tools that take a URL from the model must never become a generic fetcher:
        an absolute URL to another host is an exfiltration channel (data smuggled
        into the query string by a prompt-injected agent) and an SSRF path into
        the LAN. Relative paths and ids are always fine - they resolve against
        base_url. Only absolute URLs on `domain` or its subdomains pass.
        """
        if "://" not in url and not url.startswith("//"):
            return None
        parsed = urlparse(url if "://" in url else "https:" + url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in ("http", "https"):
            return f"only http(s) URLs are allowed, got scheme {parsed.scheme!r}"
        if parsed.username or parsed.password:
            return "URLs with embedded credentials are not allowed"
        if host == self.domain or host.endswith("." + self.domain):
            return None
        return f"{host or url!r} is not on {self.domain}; this server only fetches {self.domain} pages"

    async def fetch_page(self, url: str, mode: str = "text") -> dict[str, Any]:
        if reason := self.offsite_reason(url):
            raise ValueError(reason)
        if not url.startswith(("http://", "https://")):
            url = self.base_url.rstrip("/") + "/" + url.lstrip("/")
        res = await self.fetcher.fetch(url)
        tree = parse(res.body)
        out: dict[str, Any] = {"url": res.final_url, "status": res.status, "tier": res.tier.name}
        title = tree.css_first("title")
        out["title"] = title.text(strip=True) if title else None
        if mode == "raw":
            out["html"] = res.body
        elif mode == "links":
            out["links"] = links(tree, res.final_url)
        else:
            out["text"] = readable_text(tree)
            out["links"] = links(tree, res.final_url)[:40]
        return out

    async def close(self) -> None:
        await self.fetcher.close()

    # ---- cache helpers ----
    async def cached(self, key_parts: tuple, ttl_s: int, producer):
        key = self.cache.key(self.name, *key_parts)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        val = await producer()
        self.cache.set(key, val, ttl_s)
        return val


def load_adapter(name: str) -> SiteAdapter:
    """SITE=bunnings -> au_shopping_mcp.adapters.bunnings.Adapter()"""
    mod = importlib.import_module(f"au_shopping_mcp.adapters.{name}")
    return mod.Adapter()


def available_adapters() -> list[str]:
    """Every adapter module name (skips base and _-prefixed helpers)."""
    import pkgutil

    import au_shopping_mcp.adapters as pkg
    return sorted(m.name for m in pkgutil.iter_modules(pkg.__path__)
                  if m.name != "base" and not m.name.startswith("_"))


def load_adapters(sites: str, site: str) -> list[SiteAdapter]:
    """SITES="all" or "bunnings,costco" -> those adapters; empty SITES falls back to SITE."""
    if not sites.strip():
        return [load_adapter(site)]
    names = available_adapters() if sites.strip() == "all" else [s.strip() for s in sites.split(",") if s.strip()]
    unknown = sorted(set(names) - set(available_adapters()))
    if unknown:
        raise SystemExit(f"SITES names unknown adapters: {', '.join(unknown)}")
    return [load_adapter(n) for n in dict.fromkeys(names)]
