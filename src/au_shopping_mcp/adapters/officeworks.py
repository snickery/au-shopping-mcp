"""Officeworks (officeworks.com.au) adapter.

Verified live 2026-09-06. Officeworks is a React app: search results come from Algolia
(the storefront's search key is public, embedded in the page's window.__INITIAL_STATE__.
searchConfig), and product pages are server-rendered with the full record in
window.__INITIAL_STATE__.owProduct.product + owProductPrice.

  search:   POST https://<appId>-dsn.algolia.net/1/indexes/<index>/query   (tier 0)
  product:  GET  /shop/officeworks/p/<slug-sku>  -> parse __INITIAL_STATE__

Prices in Algolia + the price state are in CENTS. Ratings are stored x10 (39 => 3.9).
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import Availability, Category, ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter


class Adapter(SiteAdapter):
    name = "officeworks"
    domain = "officeworks.com.au"
    base_url = "https://www.officeworks.com.au"
    description = "Officeworks product search, detail and category browse (office, tech, stationery, furniture, art)."
    policy = FetchPolicy(start_tier=FetchTier.HTTP, min_delay_s=1.0, jitter_s=0.6)

    # Bootstrap defaults (public search key). Refreshed from the live page if a query 4xxs.
    ALGOLIA_APP = "K535CAAWVE"
    ALGOLIA_KEY = "8a831febe0110932cfa06ff0e2024b4f"
    BASE_INDEX = "prod-product-wc"
    # sort -> replica index suffix; relevance uses the personalised best-match index.
    SORT_MAP = {
        "relevance": "bestmatch-personal", "newest": "newest", "name": "name",
        "price_asc": "price-asc", "price_desc": "price-desc",
        "rating_desc": "rating-desc", "rating_asc": "rating-asc",
    }
    HITS = 36
    _PRODUCT_RE = re.compile(r"/shop/officeworks/p/[a-z0-9-]+")

    def __init__(self) -> None:
        super().__init__()
        self._config_loaded = False

    def parse_url(self, url: str) -> dict[str, str] | None:
        if self._PRODUCT_RE.search(url):
            m = re.search(r"/p/([a-z0-9-]+)", url)
            return {"kind": "item", "id": m.group(1) if m else url}
        if "/shop/officeworks/c/" in url:
            return {"kind": "category", "id": url}
        return None

    def sort_options(self) -> list[str]:
        return list(self.SORT_MAP)

    def site_specific_status(self) -> dict[str, Any]:
        return {"algolia": {"app_id": self.ALGOLIA_APP, "base_index": self.BASE_INDEX, "config_refreshed": self._config_loaded}}

    def _index(self, sort: str | None) -> str:
        if sort and sort not in self.SORT_MAP:
            raise ValueError(f"unknown sort {sort!r}; one of {', '.join(self.SORT_MAP)}")
        return f"{self.BASE_INDEX}-{self.SORT_MAP.get(sort or 'relevance')}"

    async def _refresh_config(self) -> None:
        """Pull fresh Algolia app id / search key / base index from the live storefront."""
        try:
            res = await self.fetcher.fetch(f"{self.base_url}/shop/officeworks/search?q=pen")
            m = re.search(r'"searchConfig"\s*:\s*(\{.*?\})\s*,\s*"[a-zA-Z]', res.body)
            if m:
                cfg = json.loads(m.group(1))
                self.ALGOLIA_APP = cfg.get("searchAppId", self.ALGOLIA_APP)
                self.ALGOLIA_KEY = cfg.get("searchApiKey", self.ALGOLIA_KEY)
                self.BASE_INDEX = cfg.get("baseIndex", self.BASE_INDEX)
                self._config_loaded = True
        except Exception:
            pass

    async def _algolia(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"https://{self.ALGOLIA_APP}-dsn.algolia.net/1/indexes/{index}/query"
        headers = {"X-Algolia-Application-Id": self.ALGOLIA_APP, "X-Algolia-API-Key": self.ALGOLIA_KEY,
                   "Content-Type": "application/json", "Accept": "application/json"}
        try:
            res = await self.fetcher.fetch_json(url, method="POST", headers=headers, json_body=body)
        except Exception:
            if self._config_loaded:
                raise
            await self._refresh_config()
            url = f"https://{self.ALGOLIA_APP}-dsn.algolia.net/1/indexes/{index}/query"
            headers.update({"X-Algolia-Application-Id": self.ALGOLIA_APP, "X-Algolia-API-Key": self.ALGOLIA_KEY})
            res = await self.fetcher.fetch_json(url, method="POST", headers=headers, json_body=body)
        return res.json_data or {}

    # ------------------------------------------------------------------ search / browse
    async def search(self, query: str, *, page: int = 1, sort: str | None = None, filters: dict[str, str] | None = None) -> SearchPage:
        index = self._index(sort)
        body: dict[str, Any] = {"query": query, "hitsPerPage": self.HITS, "page": max(0, page - 1), "facets": ["brand", "price", "rating"]}
        if filters:
            body["facetFilters"] = [f"{k}:{v}" for k, v in filters.items()]

        async def produce():
            data = await self._algolia(index, body)
            return self._to_page(data, page, query=query)

        return await self.cached(("search", index, query, page, str(filters)), self.policy.cache_ttl_search_s, produce)

    async def browse(self, category: str, *, page: int = 1, sort: str | None = None) -> SearchPage:
        # category is a catPaths slug (leaf), e.g. "staplers-staple-guns"
        slug = category.rstrip("/").split("/")[-1] if "/" in category else category
        index = self._index(sort)
        body = {"query": "", "hitsPerPage": self.HITS, "page": max(0, page - 1),
                "facetFilters": [[f"catPaths:{slug}"]], "facets": ["brand", "price", "rating"]}

        async def produce():
            data = await self._algolia(index, body)
            sp = self._to_page(data, page, query=None)
            sp.category = category
            return sp

        return await self.cached(("browse", index, slug, page), self.policy.cache_ttl_search_s, produce)

    def _to_page(self, data: dict[str, Any], page: int, *, query: str | None) -> SearchPage:
        sp = SearchPage(query=query, page=page)
        for h in data.get("hits") or []:
            ref = self._ref(h)
            if ref:
                sp.items.append(ref)
        sp.total = data.get("nbHits")
        sp.page_size = data.get("hitsPerPage") or len(sp.items)
        nb = data.get("nbPages")
        sp.has_more = bool(nb and page < nb)
        for fname, fvals in (data.get("facets") or {}).items():
            if isinstance(fvals, dict):
                sp.facets[fname] = list(fvals.keys())[:20]
        if not sp.items and "nbHits" not in data:
            raise ExtractionError("Officeworks Algolia returned no hits/nbHits - search key may have rotated. "
                                  "site_status shows the current app id.")
        return sp

    def _ref(self, h: dict[str, Any]) -> ItemRef | None:
        sku = h.get("sku") or h.get("objectID")
        seo = h.get("seoPath")
        if not (sku and seo):
            return None
        return ItemRef(
            id=str(sku), name=h.get("name") or "", url=urljoin(self.base_url, seo),
            brand=h.get("brand"), price=self._cents(h.get("price")), currency="AUD",
            availability=self._avail(h), rating=self._rating(h.get("rating")),
            review_count=int(h["reviewCount"]) if isinstance(h.get("reviewCount"), (int, float)) else None,
            image=self._img(h.get("image")),
        )

    # ------------------------------------------------------------------ product (SSR)
    async def get_item(self, id_or_url: str) -> Item:
        if id_or_url.startswith("http"):
            url = id_or_url
        elif id_or_url.startswith("/"):
            url = urljoin(self.base_url, id_or_url)
        else:
            url = f"{self.base_url}/shop/officeworks/p/{id_or_url}"

        async def produce():
            res = await self.fetcher.fetch(url)
            st = self._state(res.body)
            prod = ((st.get("owProduct") or {}).get("product") or {}) if st else {}
            if not prod.get("sku"):
                raise ExtractionError(f"No owProduct.product on {url} - not a product page or layout changed.")
            sku = prod["sku"]
            price = None
            pr = (((st.get("owProductPrice") or {}).get("price") or {}).get(sku) or {})
            if isinstance(pr.get("price"), (int, float)):
                price = self._cents(pr["price"])
            brand = prod.get("brand") or {}
            desc = (prod.get("description") or {}).get("longDescription") or (prod.get("description") or {}).get("shortDescription")
            imgs = prod.get("images") or {}
            preview = (imgs.get("preview") or imgs.get("hero") or [None])
            item = Item(
                id=str(sku), name=prod.get("name") or "", url=res.final_url,
                brand=brand.get("name") if isinstance(brand, dict) else brand, price=price, currency="AUD",
                availability=Availability.IN_STOCK if prod.get("rangedOnline") else Availability.UNKNOWN,
                image=self._img(preview[0] if preview else None),
                description=(desc or None),
            )
            rev = prod.get("review") or {}
            item.rating = self._rating(rev.get("rating"))
            if isinstance(rev.get("count") or rev.get("reviewCount"), (int, float)):
                item.review_count = int(rev.get("count") or rev.get("reviewCount"))
            _skip_sub = ("pack. comp", "packaging", "prim. pack", "sec. pack", "tert. pack")
            _skip = ("numerator", "gtin", "sellable unit of measure", "units per pack/carton",
                     "part number", "long product copy", "case quantity")
            for a in (prod.get("attributes") or []) + (prod.get("specs") or []):
                n = a.get("name") or a.get("label")
                v = a.get("value")
                if not (n and v):
                    continue
                nl = str(n).strip().lower()
                if nl in _skip or any(k in nl for k in _skip_sub) or len(str(n)) > 50 or n in item.specs or len(item.specs) >= 40:
                    continue
                item.specs[str(n)] = str(v)[:200]
            item.category_path = [b.get("name") for b in prod.get("breadcrumbs") or [] if isinstance(b, dict) and b.get("name")]
            if prod.get("gtin"):
                item.extras["gtin"] = prod["gtin"]
            if prod.get("manufacturerPartNumber"):
                item.extras["mpn"] = prod["manufacturerPartNumber"]
            return item

        return await self.cached(("item", url), self.policy.cache_ttl_item_s, produce)

    async def categories(self, parent: str | None = None) -> list[Category]:
        async def produce():
            res = await self.fetcher.fetch(f"{self.base_url}/")
            st = self._state(res.body) or {}
            cats = st.get("categories") or st.get("category") or {}
            out: list[Category] = []
            seen: set[str] = set()

            def walk(node: Any):
                if isinstance(node, dict):
                    url = node.get("url") or node.get("seoPath") or ""
                    name = node.get("name") or node.get("label")
                    if isinstance(url, str) and "/c/" in url and name and url not in seen:
                        seen.add(url)
                        out.append(Category(name=str(name), slug=url, url=urljoin(self.base_url, url)))
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)

            walk(cats)
            if parent:
                pl = parent.lower()
                out = [c for c in out if pl in c.slug.lower() or pl in c.name.lower()]
            if not out:
                raise ExtractionError("No category tree found in Officeworks home state.")
            return out[:150]

        return await self.cached(("categories", parent), self.policy.cache_ttl_search_s, produce)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _state(html: str) -> dict[str, Any] | None:
        i = html.find("window.__INITIAL_STATE__")
        if i < 0:
            return None
        j = html.find("{", i)
        try:
            obj, _ = json.JSONDecoder().raw_decode(html[j:])
            return obj
        except Exception:
            return None

    @staticmethod
    def _cents(v: Any) -> float | None:
        return round(v / 100, 2) if isinstance(v, (int, float)) else None

    @staticmethod
    def _rating(v: Any) -> float | None:
        if not isinstance(v, (int, float)):
            return None
        return round(v / 10, 1) if v > 5 else round(float(v), 1)

    @staticmethod
    def _img(u: Any) -> str | None:
        if not isinstance(u, str) or not u:
            return None
        return "https:" + u if u.startswith("//") else u

    @staticmethod
    def _avail(h: dict[str, Any]) -> Availability:
        if h.get("status") and str(h["status"]).lower() != "active":
            return Availability.OUT_OF_STOCK
        if h.get("rangedOnline") or h.get("availState"):
            return Availability.IN_STOCK
        return Availability.UNKNOWN
