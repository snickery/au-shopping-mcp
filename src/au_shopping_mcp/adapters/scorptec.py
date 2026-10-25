"""Scorptec (scorptec.com.au) adapter - PC parts / computers.

Verified live 2026-09-06. Scorptec sits behind Cloudflare, which 403s plain HTTP AND the JSON
tier but lets headless Chromium through, so this adapter starts at the BROWSER tier. Search is
powered by SLI Systems, server-rendered into the page as a product grid (grid-product-title /
grid-product-price). Product pages carry schema.org BreadcrumbList JSON-LD + an <h1>.

  search:   /search/go?w=<q>           (SLI, browser-rendered grid)
  product:  /product/<cat>/<brand>/<id-slug>

Because Cloudflare clears via a browser challenge whose cookies don't transfer to httpx, every
request runs through Playwright - slower than the other adapters; keep result counts modest.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, unquote, urljoin, parse_qs, urlparse

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import Availability, Category, ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter

_ID_RE = re.compile(r"/product/[a-z0-9-]+/[a-z0-9-]+/(\d+)-", re.I)


class Adapter(SiteAdapter):
    name = "scorptec"
    domain = "scorptec.com.au"
    base_url = "https://www.scorptec.com.au"
    description = "Scorptec Computers: PC components, systems and peripherals - search, detail, category browse."
    policy = FetchPolicy(
        start_tier=FetchTier.BROWSER,   # Cloudflare 403s HTTP; Chromium clears it
        min_delay_s=2.0, jitter_s=1.0, timeout_s=40,
    )

    def parse_url(self, url: str) -> dict[str, str] | None:
        m = _ID_RE.search(url)
        if m:
            return {"kind": "item", "id": m.group(1)}
        if "/category/" in url:
            return {"kind": "category", "id": url}
        if "/search" in url:
            return {"kind": "search", "id": url}
        return None

    def sort_options(self) -> list[str]:
        return ["relevance"]  # SLI grid default; other sorts are JS-driven

    # ------------------------------------------------------------------ search
    async def search(self, query: str, *, page: int = 1, sort: str | None = None, filters: dict[str, str] | None = None) -> SearchPage:
        url = f"{self.base_url}/search/go?w={quote_plus(query)}"
        if page > 1:
            url += f"&page={page}"

        async def produce():
            res = await self.fetcher.fetch(url)
            sp = self._parse_grid(res.body, res.final_url)
            sp.query, sp.page = query, page
            return sp

        return await self.cached(("search", url), self.policy.cache_ttl_search_s, produce)

    async def browse(self, category: str, *, page: int = 1, sort: str | None = None) -> SearchPage:
        url = category if category.startswith("http") else urljoin(self.base_url, category)

        async def produce():
            res = await self.fetcher.fetch(url)
            sp = self._parse_grid(res.body, res.final_url)
            sp.category, sp.page = category, page
            return sp

        return await self.cached(("browse", url), self.policy.cache_ttl_search_s, produce)

    def _real_url(self, href: str) -> str:
        """SLI links wrap the product URL in a ?url= param; decode it back to /product/..."""
        q = parse_qs(urlparse(href).query)
        u = q.get("url", [None])[0]
        if u:
            u = unquote(u)
            return u if u.startswith("http") else urljoin(self.base_url, u)
        return urljoin(self.base_url, href)

    def _parse_grid(self, html: str, source_url: str) -> SearchPage:
        tree = ex.parse(html)
        sp = SearchPage(source_url=source_url)
        for title in tree.css(".grid-product-title"):
            a = title.css_first("a[href]")
            if not a:
                continue
            url = self._real_url(a.attributes.get("href") or "")
            name = a.text(strip=True)
            if not name or "/product/" not in url:
                continue
            # price + stock sit in the sibling grid-product-price-stock block of the same card
            card = title
            for _ in range(4):
                card = card.parent or card
            price_n = card.css_first(".grid-product-price")
            stock_n = card.css_first(".grid-product-stock")
            pid = (self.parse_url(url) or {}).get("id")
            sp.items.append(ItemRef(
                id=str(pid or url), name=name, url=url.split("?")[0],
                price=ex.parse_price(price_n.text(strip=True) if price_n else None), currency="AUD",
                availability=self._avail(stock_n.text(strip=True) if stock_n else ""),
            ))
        # de-dupe (SLI can repeat a card in mobile/desktop variants)
        seen, uniq = set(), []
        for it in sp.items:
            if it.url in seen:
                continue
            seen.add(it.url)
            uniq.append(it)
        sp.items = uniq
        m = re.search(r'sli_result_set_after_value[^>]*>\s*([\d,]+)', html)
        sp.total = int(m.group(1).replace(",", "")) if m else None
        sp.page_size = len(sp.items)
        sp.has_more = bool(sp.total and sp.page * max(1, len(sp.items)) < sp.total)
        if not sp.items:
            raise ExtractionError("No SLI product grid on Scorptec results page - Cloudflare block, zero results, "
                                  "or layout changed. Run probe_page on the URL.")
        return sp

    # ------------------------------------------------------------------ product
    async def get_item(self, id_or_url: str) -> Item:
        if not id_or_url.startswith("http"):
            if id_or_url.startswith("/"):
                id_or_url = urljoin(self.base_url, id_or_url)
            else:
                sp = await self.search(id_or_url, page=1)
                if not sp.items:
                    raise ExtractionError(f"No Scorptec product found for {id_or_url!r}.")
                id_or_url = sp.items[0].url
        url = id_or_url

        async def produce():
            res = await self.fetcher.fetch(url)
            tree = ex.parse(res.body)
            prod = ex.json_ld_of_type(tree, "Product")
            name = ex.text(tree, "h1") or (prod.get("name") if prod else None) or ex.meta(tree, "og:title")
            if not name:
                raise ExtractionError(f"No product title on {url} - Cloudflare block or layout changed.")
            price = None
            if prod:
                offer = prod.get("offers") or {}
                price = ex.parse_price(str(offer.get("price") or ""))
            if price is None:
                price = ex.parse_price(ex.text(tree, ".grid-product-price, .product-price, .price"))
            item = Item(
                id=str((self.parse_url(url) or {}).get("id") or url), name=name, url=res.final_url,
                brand=(prod.get("brand") or {}).get("name") if prod and isinstance(prod.get("brand"), dict) else None,
                price=price, currency="AUD", image=(prod.get("image") if prod and isinstance(prod.get("image"), str) else ex.meta(tree, "og:image")),
                description=(prod.get("description") if prod else None) or ex.meta(tree, "og:description"),
            )
            bc = ex.json_ld_of_type(tree, "BreadcrumbList")
            if bc:
                item.category_path = [e.get("name") or (e.get("item") or {}).get("name", "") for e in bc.get("itemListElement", [])][1:-1]
            for row in tree.css(".product-specs tr, table.specs tr, .specifications tr"):
                cells = [c.text(strip=True) for c in row.css("th, td")]
                if len(cells) == 2 and cells[0] and len(cells[0]) < 60:
                    item.specs[cells[0]] = cells[1]
            return item

        return await self.cached(("item", url), self.policy.cache_ttl_item_s, produce)

    @staticmethod
    def _avail(txt: str) -> Availability:
        t = txt.lower()
        if "in stock" in t or "available" in t:
            return Availability.IN_STOCK
        if "out of stock" in t or "sold out" in t or "unavailable" in t:
            return Availability.OUT_OF_STOCK
        if "pre" in t or "back" in t or "order" in t:
            return Availability.SPECIAL_ORDER
        if "low" in t:
            return Availability.LOW_STOCK
        return Availability.UNKNOWN
