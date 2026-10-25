"""Copy to adapters/<site>.py, fill the PROBE items, set SITE=<site>.

Onboarding a new site, in order:
  1. probe_page on a search page and a detail page -> see which extraction sources exist
  2. If the site's frontend calls a JSON endpoint (DevTools > Network > Fetch/XHR), implement
     search() with self.fetcher.fetch_json() and skip HTML entirely (tier 0).
  3. Otherwise JSON-LD -> hydration state -> selectors, same as bunnings.py.
  4. Save one real search page + one detail page into tests/fixtures/<site>/ and write
     tests against them so a redesign shows up as a red test, not a confused agent.
"""
from __future__ import annotations

import re

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter


class Adapter(SiteAdapter):
    name = "example"                                  # PROBE
    domain = "example.com"                            # PROBE
    base_url = "https://www.example.com"              # PROBE
    description = "One line about what this site is and what the tools return."
    policy = FetchPolicy(
        start_tier=FetchTier.HTTP,                    # bump to BROWSER if the site is a pure SPA
        min_delay_s=1.5,
        browser_wait_selector=None,                   # PROBE: e.g. "[data-testid=product-card]"
    )

    ITEM_URL_RE = re.compile(r"/product/([A-Za-z0-9-]+)")   # PROBE

    def parse_url(self, url: str):
        m = self.ITEM_URL_RE.search(url)
        return {"kind": "item", "id": m.group(1)} if m else None

    async def search(self, query, *, page=1, sort=None, filters=None) -> SearchPage:
        url = f"{self.base_url}/search?q={query}&page={page}"          # PROBE
        res = await self.fetcher.fetch(url)
        tree = ex.parse(res.body)
        sp = SearchPage(query=query, page=page, source_url=res.final_url)
        for node in (ex.json_ld_of_type(tree, "ItemList") or {}).get("itemListElement", []):
            item = node.get("item", node)
            sp.items.append(ItemRef(id=item.get("sku", item.get("url")), name=item.get("name", ""), url=item.get("url")))
        if not sp.items:
            raise ExtractionError("No results parsed - run probe_page on the search URL.")
        return sp

    async def get_item(self, id_or_url: str) -> Item:
        url = id_or_url if id_or_url.startswith("http") else f"{self.base_url}/product/{id_or_url}"   # PROBE
        res = await self.fetcher.fetch(url)
        tree = ex.parse(res.body)
        prod = ex.json_ld_of_type(tree, "Product")
        if not prod:
            raise ExtractionError("No JSON-LD Product - add selectors for this site.")
        return Item(id=str(prod.get("sku") or id_or_url), name=prod.get("name", ""), url=url)
