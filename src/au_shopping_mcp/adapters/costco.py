"""Costco Australia (costco.com.au) adapter.

Verified live 2026-09-06. Costco runs SAP hybris (Spartacus storefront) with a clean JSON
REST API under /rest/v2/australia — no bot protection, no HTML scraping needed (tier 0).

  search:   GET /rest/v2/australia/products/search?fields=FULL&query=<q>&pageSize=<n>&currentPage=<0-based>&sort=<key>&lang=en_AU&curr=AUD
  product:  GET /rest/v2/australia/products/<code>?fields=FULL&lang=en_AU&curr=AUD

Membership: some prices are members-only (`membershipRestrictionApplied` / `hidePriceValue`).
An authenticated session (`au-shopping-mcp login costco`, cookies persisted to DATA_DIR) exposes
those. Unauthenticated still returns the public catalogue + non-restricted prices.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import Availability, Category, ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter

_CODE_RE = re.compile(r"/p/(\d+)|^(\d{5,})$")


class Adapter(SiteAdapter):
    name = "costco"
    domain = "costco.com.au"
    base_url = "https://www.costco.com.au"
    description = ("Costco Australia warehouse catalogue: product search, detail and category browse. "
                   "Some prices are members-only and only appear when logged in (au-shopping-mcp login costco).")
    policy = FetchPolicy(start_tier=FetchTier.HTTP, min_delay_s=1.0, jitter_s=0.6)

    login_url = "https://www.costco.com.au/LogonForm"
    logged_in_selector = None  # Costco has no reliable server-marker; login flow uses a manual confirm

    API = "/rest/v2/australia"
    COMMON = {"lang": "en_AU", "curr": "AUD"}
    SORT_MAP = {
        "relevance": "relevance", "name": "name-asc", "name_desc": "name-desc",
        "price_asc": "price-asc", "price_desc": "price-desc",
        "bestselling": "sellCount-desc", "rating_desc": "ratings-desc",
    }
    PAGE_SIZE = 48

    # ------------------------------------------------------------------ helpers
    def _q(self, extra: dict[str, Any]) -> str:
        parts = {**self.COMMON, **extra}
        return "&".join(f"{k}={quote_plus(str(v))}" for k, v in parts.items() if v not in (None, ""))

    def parse_url(self, url: str) -> dict[str, str] | None:
        m = _CODE_RE.search(url)
        if m:
            return {"kind": "item", "id": m.group(1) or m.group(2)}
        if "/c/" in url or re.search(r"/[A-Za-z-]+/[A-Za-z-]+$", url):
            return {"kind": "category", "id": url}
        return None

    def sort_options(self) -> list[str]:
        return list(self.SORT_MAP)

    def site_specific_status(self) -> dict[str, Any]:
        return {"membership_note": "Members-only prices require an authenticated session (au-shopping-mcp login costco)."}

    def _sort(self, sort: str | None) -> str:
        if sort and sort not in self.SORT_MAP:
            raise ValueError(f"unknown sort {sort!r}; one of {', '.join(self.SORT_MAP)}")
        return self.SORT_MAP.get(sort or "relevance", "relevance")

    # ------------------------------------------------------------------ search / browse
    async def search(self, query: str, *, page: int = 1, sort: str | None = None, filters: dict[str, str] | None = None) -> SearchPage:
        return await self._listing(query=query, category=None, page=page, sort=sort)

    async def browse(self, category: str, *, page: int = 1, sort: str | None = None) -> SearchPage:
        code = category
        if category.startswith("http") or "/" in category:
            # e.g. a /c/... url or a category code like cos_21.1.2
            m = re.search(r"(cos_[\d.]+)", category)
            code = m.group(1) if m else category.rstrip("/").split("/")[-1]
        return await self._listing(query="", category=code, page=page, sort=sort)

    async def _listing(self, *, query: str, category: str | None, page: int, sort: str | None) -> SearchPage:
        srt = self._sort(sort)
        extra = {"fields": "FULL", "query": query, "pageSize": self.PAGE_SIZE,
                 "currentPage": max(0, page - 1), "sort": srt}
        if category:
            extra["category"] = category
        url = f"{self.base_url}{self.API}/products/search?{self._q(extra)}"

        async def produce():
            res = await self.fetcher.fetch_json(url, headers={"Accept": "application/json"})
            data = res.json_data or {}
            sp = SearchPage(query=query or None, category=category, page=page, source_url=res.final_url)
            for p in data.get("products") or []:
                ref = self._ref(p)
                if ref:
                    sp.items.append(ref)
            pg = data.get("pagination") or {}
            sp.total = pg.get("totalNumberOfResults")
            sp.page_size = pg.get("pageSize") or len(sp.items)
            tp = pg.get("totalPages")
            sp.has_more = bool(tp and page < tp)
            for f in data.get("facets") or []:
                vals = [str(v.get("name")) for v in (f.get("values") or []) if isinstance(v, dict) and v.get("name")]
                if f.get("name") and vals:
                    sp.facets[str(f["name"])] = vals[:20]
            if not sp.items and not (data.get("pagination")):
                raise ExtractionError("Costco search returned no product/pagination payload - API shape changed. "
                                      "Run probe_page on the search URL.")
            return sp

        return await self.cached(("search", url), self.policy.cache_ttl_search_s, produce)

    def _ref(self, p: dict[str, Any]) -> ItemRef | None:
        code = p.get("code")
        if not code:
            return None
        return ItemRef(
            id=str(code), name=p.get("name") or "", url=urljoin(self.base_url, p.get("url") or f"/p/{code}"),
            brand=(p.get("manufacturer") or None), price=self._price(p), currency="AUD",
            availability=self._avail(p), rating=self._rating(p.get("averageRating")),
            review_count=int(p["numberOfReviews"]) if isinstance(p.get("numberOfReviews"), (int, float)) else None,
            image=self._image(p),
        )

    # ------------------------------------------------------------------ product
    async def get_item(self, id_or_url: str) -> Item:
        code = (self.parse_url(id_or_url) or {}).get("id")
        if not code:
            code = id_or_url.rstrip("/").split("/")[-1]
        url = f"{self.base_url}{self.API}/products/{quote_plus(code)}?{self._q({'fields': 'FULL'})}"

        async def produce():
            res = await self.fetcher.fetch_json(url, headers={"Accept": "application/json"})
            p = res.json_data or {}
            if not p.get("code"):
                raise ExtractionError(f"No Costco product for code {code!r} (fields returned: {sorted(p)[:8]}).")
            item = Item(
                id=str(p["code"]), name=p.get("name") or "", url=urljoin(self.base_url, p.get("url") or f"/p/{p['code']}"),
                brand=p.get("manufacturer") or None, price=self._price(p), currency="AUD", availability=self._avail(p),
                rating=self._rating(p.get("averageRating")),
                review_count=int(p["numberOfReviews"]) if isinstance(p.get("numberOfReviews"), (int, float)) else None,
                image=self._image(p), description=self._text(p.get("description")),
            )
            for c in p.get("classifications") or []:
                for f in (c.get("features") or []) if isinstance(c, dict) else []:
                    name = f.get("name")
                    vals = [str(v.get("value")) for v in (f.get("featureValues") or []) if isinstance(v, dict) and v.get("value") not in (None, "")]
                    if name and vals and name not in item.specs:
                        item.specs[name] = ", ".join(vals)
            cats = [c.get("name") for c in p.get("categories") or [] if isinstance(c, dict) and c.get("name")]
            if cats:
                item.category_path = cats
            st = p.get("stock") or {}
            if st.get("stockLevel") is not None:
                item.extras["stock_level"] = st["stockLevel"]
            for flag in ("membershipRestrictionApplied", "availableForPickup", "availableForPreorder", "idVerificationEnabled"):
                if p.get(flag):
                    item.extras[flag] = True
            if p.get("hidePriceValue") or (item.price is None and p.get("membershipRestrictionApplied")):
                item.extras["price_hidden"] = "Members-only price - run 'au-shopping-mcp login costco' and retry."
            return item

        return await self.cached(("item", url), self.policy.cache_ttl_item_s, produce)

    async def categories(self, parent: str | None = None) -> list[Category]:
        url = f"{self.base_url}{self.API}/cms/navigationMenu?{self._q({})}".replace("v2", "v3")

        async def produce():
            res = await self.fetcher.fetch_json(url, headers={"Accept": "application/json"})
            data = res.json_data or {}
            out: list[Category] = []
            seen: set[str] = set()

            def walk(node: Any):
                if isinstance(node, dict):
                    link = node.get("url") or node.get("link") or ""
                    name = node.get("name") or node.get("title") or node.get("linkName")
                    ok = (isinstance(link, str) and link.startswith("/") and link not in seen
                          and "/navigation_nodes/" not in link and not re.search(r"/blt[0-9a-f]{6,}", link)
                          and re.search(r"/[A-Za-z][A-Za-z0-9-]+", link))
                    if ok and name:
                        seen.add(link)
                        out.append(Category(name=str(name), slug=link, url=urljoin(self.base_url, link)))
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)

            walk(data)
            if parent:
                pl = parent.lower()
                out = [c for c in out if pl in c.slug.lower() or pl in c.name.lower()]
            return out[:150]

        return await self.cached(("categories", url, parent), self.policy.cache_ttl_search_s, produce)

    # ------------------------------------------------------------------ field helpers
    @staticmethod
    def _price(p: dict[str, Any]) -> float | None:
        pr = p.get("price") or {}
        return float(pr["value"]) if isinstance(pr.get("value"), (int, float)) else None

    @staticmethod
    def _rating(r: Any) -> float | None:
        return round(float(r), 2) if isinstance(r, (int, float)) else None

    @staticmethod
    def _avail(p: dict[str, Any]) -> Availability:
        st = (p.get("stock") or {}).get("stockLevelStatus", "")
        st = str(st).lower()
        if p.get("isProductAvailable") is False:
            return Availability.OUT_OF_STOCK
        if "outofstock" in st:
            return Availability.OUT_OF_STOCK
        if "lowstock" in st:
            return Availability.LOW_STOCK
        if "instock" in st:
            return Availability.IN_STOCK
        return Availability.UNKNOWN

    def _image(self, p: dict[str, Any]) -> str | None:
        imgs = p.get("images") or []
        primary = next((i.get("url") for i in imgs if isinstance(i, dict) and i.get("imageType") == "PRIMARY" and i.get("format") in ("product", "zoom", None) and i.get("url")), None)
        url = primary or (imgs[0].get("url") if imgs and isinstance(imgs[0], dict) else None)
        return urljoin(self.base_url, url) if url and url.startswith("/") else url

    @staticmethod
    def _text(html: str | None) -> str | None:
        if not html:
            return None
        txt = re.sub(r"<[^>]+>", " ", html)
        txt = re.sub(r"\s+", " ", txt).strip()
        return txt[:2000] or None
