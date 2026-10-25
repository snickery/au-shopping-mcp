"""Altronics (altronics.com.au) adapter.

Verified live 2026-09-06. Altronics runs nopCommerce - fully server-rendered HTML. Search and
category pages return a product grid; product pages carry schema.org Product + BreadcrumbList
JSON-LD. No JSON API, no bot protection (tier 1, selectors + JSON-LD).

  search:   /search/?q=<q>&orderby=<n>&pagesize=25&pagenumber=<page>
  product:  /product/<slug>   (item id = Altronics SKU, e.g. Z6320, from the JSON-LD)
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import Availability, Category, ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter


class Adapter(SiteAdapter):
    name = "altronics"
    domain = "altronics.com.au"
    base_url = "https://www.altronics.com.au"
    description = "Altronics electronics retailer: components, kits, tools, test gear - product search, detail, category browse."
    policy = FetchPolicy(start_tier=FetchTier.HTTP, min_delay_s=1.2, jitter_s=0.6)

    # nopCommerce orderby codes (from the sort <select> on the search page).
    SORT_MAP = {"relevance": None, "name": "5", "name_desc": "6", "sku": "8", "sku_desc": "9",
                "price_asc": "10", "price_desc": "11", "newest": "15"}
    PAGE_SIZE = 25
    _PRODUCT_RE = re.compile(r"/product/([a-z0-9][a-z0-9-]+)", re.I)
    SELECTORS = {
        "card": ".product-item, .item-box .product-item",
        "link": "a[href^='/product/']",
        "title": ".product-title a, h2.product-title",
        "price": ".new-price, .actual-price, .price-value",
        "sku": ".sku-number, .sku",
    }

    def parse_url(self, url: str) -> dict[str, str] | None:
        m = self._PRODUCT_RE.search(url)
        if m:
            return {"kind": "item", "id": m.group(1)}
        if "/category/" in url:
            return {"kind": "category", "id": url}
        if "/search" in url:
            return {"kind": "search", "id": url}
        return None

    def sort_options(self) -> list[str]:
        return list(self.SORT_MAP)

    def _orderby(self, sort: str | None) -> str:
        if sort and sort not in self.SORT_MAP:
            raise ValueError(f"unknown sort {sort!r}; one of {', '.join(self.SORT_MAP)}")
        v = self.SORT_MAP.get(sort or "relevance")
        return f"&orderby={v}" if v else ""

    # ------------------------------------------------------------------ search / browse
    async def search(self, query: str, *, page: int = 1, sort: str | None = None, filters: dict[str, str] | None = None) -> SearchPage:
        url = f"{self.base_url}/search/?q={quote_plus(query)}&pagesize={self.PAGE_SIZE}&pagenumber={page}{self._orderby(sort)}"

        async def produce():
            res = await self.fetcher.fetch(url)
            sp = self._listing(res.body, res.final_url)
            sp.query, sp.page = query, page
            return sp

        return await self.cached(("search", url), self.policy.cache_ttl_search_s, produce)

    async def browse(self, category: str, *, page: int = 1, sort: str | None = None) -> SearchPage:
        url = category if category.startswith("http") else urljoin(self.base_url, category)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}pagesize={self.PAGE_SIZE}&pagenumber={page}{self._orderby(sort)}"

        async def produce():
            res = await self.fetcher.fetch(url)
            sp = self._listing(res.body, res.final_url)
            sp.category, sp.page = category, page
            return sp

        return await self.cached(("browse", url), self.policy.cache_ttl_search_s, produce)

    def _listing(self, html: str, source_url: str) -> SearchPage:
        tree = ex.parse(html)
        sp = SearchPage(source_url=source_url)
        for card in tree.css(".product-item"):
            a = card.css_first("a[href^='/product/']")
            if not a:
                continue
            href = a.attributes.get("href") or ""
            url = urljoin(self.base_url, href)
            pid = (self.parse_url(url) or {}).get("id")
            sku_n = card.css_first(".sku-number, .sku")
            sku = (sku_n.text(strip=True) if sku_n else "") or (card.attributes.get("data-productid") or "")
            title_n = card.css_first(".product-title a") or a
            name = (title_n.attributes.get("title") or title_n.text(strip=True) or "").replace("&amp;", "&")
            price_n = card.css_first(".new-price, .actual-price, .price-value")
            img_n = card.css_first("img")
            if not (url and (sku or name)):
                continue
            sp.items.append(ItemRef(
                id=str(sku or pid or href), name=name, url=url,
                price=ex.parse_price(price_n.text(strip=True) if price_n else None), currency="AUD",
                image=(img_n.attributes.get("src") or img_n.attributes.get("data-src")) if img_n else None,
            ))
        # total / paging from the pager
        total = None
        m = re.search(r'of\s+([\d,]+)\s+(?:items|products|results)', html, re.I)
        if m:
            total = int(m.group(1).replace(",", ""))
        sp.total = total
        sp.page_size = len(sp.items)
        sp.has_more = bool(tree.css_first("li.next-page a, a.next-page, a[rel='next']")) or bool(total and sp.page * self.PAGE_SIZE < total)
        if not sp.items and "product-item" not in html:
            raise ExtractionError("No product grid on Altronics listing page - layout changed or zero results. "
                                  "Run probe_page on the URL.")
        return sp

    # ------------------------------------------------------------------ product
    async def get_item(self, id_or_url: str) -> Item:
        if id_or_url.startswith("http"):
            url = id_or_url
        elif id_or_url.startswith("/"):
            url = urljoin(self.base_url, id_or_url)
        else:
            # bare SKU: resolve via search (Altronics has no sku->url shortcut)
            sp = await self.search(id_or_url, page=1)
            hit = next((i for i in sp.items if i.id.lower() == id_or_url.lower()), sp.items[0] if sp.items else None)
            if not hit:
                raise ExtractionError(f"No Altronics product found for {id_or_url!r}.")
            url = hit.url

        async def produce():
            res = await self.fetcher.fetch(url)
            tree = ex.parse(res.body)
            prod = ex.json_ld_of_type(tree, "Product")
            if not prod:
                name = ex.text(tree, "h1") or ex.meta(tree, "og:title")
                if not name:
                    raise ExtractionError(f"No JSON-LD Product nor <h1> on {url} - not a product page or layout changed.")
                item = Item(id=id_or_url, name=name, url=res.final_url, currency="AUD",
                            price=ex.parse_price(ex.text(tree, ".new-price, .actual-price, .price-value")),
                            image=ex.meta(tree, "og:image"), description=ex.meta(tree, "og:description"))
            else:
                offer = prod.get("offers") or {}
                item = Item(
                    id=str(prod.get("sku") or (self.parse_url(url) or {}).get("id") or url),
                    name=prod.get("name") or "", url=res.final_url,
                    brand=(prod.get("brand") or {}).get("name") if isinstance(prod.get("brand"), dict) else (prod.get("brand") or None),
                    price=ex.parse_price(str(offer.get("price") or "")), currency=offer.get("priceCurrency") or "AUD",
                    availability=self._avail(str(offer.get("availability", ""))),
                    image=prod.get("image") if isinstance(prod.get("image"), str) else None,
                    description=(prod.get("description") or ex.meta(tree, "og:description") or "")[:2000] or None,
                )
                if prod.get("mpn"):
                    item.extras["mpn"] = prod["mpn"]
            # spec table
            for row in tree.css(".product-specs-table tr, .spec-table tr, table.data-table tr"):
                cells = [c.text(strip=True) for c in row.css("th, td")]
                if len(cells) == 2 and cells[0] and len(cells[0]) < 60:
                    item.specs[cells[0]] = cells[1]
            bc = ex.json_ld_of_type(tree, "BreadcrumbList")
            if bc:
                item.category_path = [e.get("name") or (e.get("item") or {}).get("name", "") for e in bc.get("itemListElement", [])][1:]
            return item

        return await self.cached(("item", url), self.policy.cache_ttl_item_s, produce)

    async def categories(self, parent: str | None = None) -> list[Category]:
        url = parent if parent and parent.startswith("http") else urljoin(self.base_url, parent or "/")
        res = await self.fetcher.fetch(url)
        tree = ex.parse(res.body)
        out: list[Category] = []
        seen: set[str] = set()
        for a in tree.css("a[href^='/category/']"):
            href = urljoin(self.base_url, a.attributes.get("href") or "").split("?")[0].rstrip("/")
            if href in seen:
                continue
            seen.add(href)
            name = a.text(strip=True)
            if name:
                out.append(Category(name=name, slug=href.replace(self.base_url, ""), url=href))
        out.sort(key=lambda c: c.slug)
        return out[:200]

    @staticmethod
    def _avail(a: str) -> Availability:
        a = a.lower()
        if "instock" in a:
            return Availability.IN_STOCK
        if "outofstock" in a or "soldout" in a:
            return Availability.OUT_OF_STOCK
        if "preorder" in a or "backorder" in a:
            return Availability.SPECIAL_ORDER
        return Availability.UNKNOWN
