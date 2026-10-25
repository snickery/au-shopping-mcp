"""staticICE (staticice.com.au) adapter - Australian price-comparison aggregator.

Verified live 2026-09-06. Plain server-rendered HTML, no bot protection (tier 1). Unlike the
retail adapters, staticICE returns the SAME product offered by MANY stores, cheapest first, so
`search_products` is the whole point: each result is one store's offer (price, store name,
state, and a link straight to that store's product page). There are no staticICE product pages,
so `get_product` just echoes back an offer you pass it.

  search:  /cgi-bin/search.cgi?q=<q>&spos=3
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, unquote, urljoin, parse_qs, urlparse

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import Availability, ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter


class Adapter(SiteAdapter):
    name = "staticice"
    domain = "staticice.com.au"
    base_url = "https://www.staticice.com.au"
    description = ("staticICE Australian price comparison: one search returns the same product priced across "
                   "many stores, cheapest first, each with a direct link to that store. Aggregator - get_product "
                   "just re-parses an offer, there are no staticICE product pages.")
    policy = FetchPolicy(start_tier=FetchTier.HTTP, min_delay_s=1.5, jitter_s=0.8)

    _PRICE_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")

    def parse_url(self, url: str) -> dict[str, str] | None:
        if "search.cgi" in url:
            return {"kind": "search", "id": url}
        return None

    def sort_options(self) -> list[str]:
        return ["price_asc"]  # staticICE always returns cheapest-first

    def site_specific_status(self) -> dict[str, Any]:
        return {"aggregator": True, "note": "Results are per-store offers of the same product, cheapest first."}

    async def search(self, query: str, *, page: int = 1, sort: str | None = None, filters: dict[str, str] | None = None) -> SearchPage:
        # staticICE has no real paging; spos just shifts the sponsored block.
        url = f"{self.base_url}/cgi-bin/search.cgi?q={quote_plus(query)}&spos=3"

        async def produce():
            res = await self.fetcher.fetch(url)
            sp = self._parse(res.body, res.final_url)
            sp.query, sp.page = query, page
            return sp

        return await self.cached(("search", url), self.policy.cache_ttl_search_s, produce)

    def _parse(self, html: str, source_url: str) -> SearchPage:
        tree = ex.parse(html)
        sp = SearchPage(source_url=source_url)
        for row in tree.css('tr[valign="top"]'):
            texts = [t for t in (row.text(deep=True, separator="\n").split("\n")) if t.strip()]
            texts = [t.strip() for t in texts if t.strip()]
            if len(texts) < 2:
                continue
            price = None
            for t in texts:
                m = self._PRICE_RE.match(t)
                if m:
                    price = ex.parse_price(m.group(0))
                    break
            if price is None:
                continue
            # product link = first redirect with newurl=
            real_url, store = None, None
            for a in row.css("a[href*='redirect.cgi']"):
                href = a.attributes.get("href") or ""
                q = parse_qs(urlparse(href).query)
                nu = q.get("newurl", [None])[0]
                if nu and "/product" in nu.lower() or (nu and len(nu) > 40):
                    real_url = unquote(nu)
                    store = (q.get("name", [None])[0] or "").replace("%20", " ")
                    break
                if nu and not real_url:
                    real_url = unquote(nu)
                    store = (q.get("name", [None])[0] or "").replace("%20", " ")
            # description = the longest non-price text node
            desc = max((t for t in texts if not self._PRICE_RE.match(t) and "updated" not in t.lower()
                        and t not in ("|", "•", "&bullet;")), key=len, default="")
            desc = re.sub(r"\s*\|\s*", " ", desc).strip()
            if not desc:
                continue
            state_m = re.search(r"\(([A-Z]{2,3})\)", " ".join(texts))
            store_name = store or (next((t for t in texts if "www." in t or ".com" in t), None) or "").strip()
            sp.items.append(ItemRef(
                id=f"{(store_name or 'store')}:{abs(hash(desc)) % 10**8}",
                name=desc[:200], url=real_url or source_url, brand=store_name or None,
                price=price, currency="AUD",
            ))
        if not sp.items:
            raise ExtractionError("No priced rows on staticICE results page - zero results or layout changed. "
                                  "Run probe_page on the search URL.")
        sp.total = len(sp.items)
        sp.page_size = len(sp.items)
        sp.has_more = False
        return sp

    async def get_item(self, id_or_url: str) -> Item:
        raise ExtractionError(
            "staticICE is a price-comparison aggregator with no product pages. Use staticice_search_products; "
            "each result's `url` links straight to the cheapest store, and `brand` is the store name. "
            f"(You passed {id_or_url!r}.)"
        )
