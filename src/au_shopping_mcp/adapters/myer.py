"""Myer (myer.com.au) adapter.

Verified live 2026-09-06. Myer is a Next.js app with a react-query cache in
__NEXT_DATA__.props.pageProps.dehydratedState.queries (same mechanism as Bunnings).

  search:    /search?query=<q>&pageNumber=<n>   -> query key ["search", q, "<n>", [], sort]
  category:  /c/<path>?pageNumber=<n>           -> query key ["category", "/<path>", n, [], sort]
  product:   /p/<seoToken>                       -> query key ["product", seoToken] (+ JSON-LD Product)

Prices are plain dollars. Listing entries carry priceFrom/listPriceFrom (was-price) and colour
variants; the product page adds a full description, attributes and per-size variants.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import Availability, Category, ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter

_IMG_BASE = "https://prod-cdn.myer.com/production/"


class Adapter(SiteAdapter):
    name = "myer"
    domain = "myer.com.au"
    base_url = "https://www.myer.com.au"
    description = "Myer department store: product search, detail and category browse (fashion, beauty, home, tech)."
    policy = FetchPolicy(start_tier=FetchTier.HTTP, min_delay_s=1.2, jitter_s=0.7)

    SORT_MAP = {"relevance": None, "price_asc": "priceAscending", "price_desc": "priceDescending",
                "newest": "newIn", "discount": "highestDiscount"}
    _PRODUCT_RE = re.compile(r"/p/([a-z0-9-]+)", re.I)

    def parse_url(self, url: str) -> dict[str, str] | None:
        m = self._PRODUCT_RE.search(url)
        if m:
            return {"kind": "item", "id": m.group(1)}
        if "/c/" in url:
            return {"kind": "category", "id": url}
        if "/search" in url:
            return {"kind": "search", "id": url}
        return None

    def sort_options(self) -> list[str]:
        return list(self.SORT_MAP)

    def _sort_param(self, sort: str | None) -> str:
        if sort and sort not in self.SORT_MAP:
            raise ValueError(f"unknown sort {sort!r}; one of {', '.join(self.SORT_MAP)}")
        s = self.SORT_MAP.get(sort or "relevance")
        return f"&sortBy={s}" if s else ""

    # ------------------------------------------------------------------ search / browse
    async def search(self, query: str, *, page: int = 1, sort: str | None = None, filters: dict[str, str] | None = None) -> SearchPage:
        url = f"{self.base_url}/search?query={quote_plus(query)}&pageNumber={page}{self._sort_param(sort)}"

        async def produce():
            res = await self.fetcher.fetch(url)
            sp = self._listing(res.body, res.final_url, ("search",))
            sp.query, sp.page = query, page
            return sp

        return await self.cached(("search", url), self.policy.cache_ttl_search_s, produce)

    async def browse(self, category: str, *, page: int = 1, sort: str | None = None) -> SearchPage:
        path = category if category.startswith("http") else urljoin(self.base_url, category if category.startswith("/c/") else "/c/" + category.lstrip("/"))
        sep = "&" if "?" in path else "?"
        url = f"{path}{sep}pageNumber={page}{self._sort_param(sort)}"

        async def produce():
            res = await self.fetcher.fetch(url)
            sp = self._listing(res.body, res.final_url, ("category",))
            sp.category, sp.page = category, page
            return sp

        return await self.cached(("browse", url), self.policy.cache_ttl_search_s, produce)

    def _queries(self, html: str) -> list[dict[str, Any]]:
        nd = ex.next_data(ex.parse(html))
        if not nd:
            return []
        return (((nd.get("props") or {}).get("pageProps") or {}).get("dehydratedState") or {}).get("queries") or []

    def _listing(self, html: str, source_url: str, key0s: tuple[str, ...]) -> SearchPage:
        sp = SearchPage(source_url=source_url)
        data = None
        for q in self._queries(html):
            k = q.get("queryKey") or []
            if k and k[0] in key0s and isinstance(q.get("state", {}).get("data"), dict) and "productList" in q["state"]["data"]:
                data = q["state"]["data"]
                break
        if data is None:
            raise ExtractionError("No search/category result set in Myer __NEXT_DATA__ - not a listing page or layout changed.")
        for p in data.get("productList") or []:
            ref = self._ref(p)
            if ref:
                sp.items.append(ref)
        sp.total = data.get("productTotalCount")
        sp.page_size = data.get("productsPerPage") or len(sp.items)
        off = data.get("offset") or 0
        sp.has_more = bool(sp.total is not None and off + len(sp.items) < sp.total)
        for f in data.get("facetList") or []:
            name = f.get("name") or f.get("id") or f.get("displayName")
            vals = [str(v.get("name") or v.get("value")) for v in (f.get("values") or f.get("facetValues") or []) if isinstance(v, dict)]
            if name and vals:
                sp.facets[str(name)] = vals[:20]
        return sp

    def _ref(self, p: dict[str, Any]) -> ItemRef | None:
        pid = p.get("id")
        seo = p.get("seoToken")
        if not pid:
            return None
        url = urljoin(self.base_url, f"/p/{seo}") if seo else urljoin(self.base_url, f"/p/{pid}")
        return ItemRef(
            id=str(pid), name=p.get("name") or "", url=url, brand=p.get("brand"),
            price=self._num(p.get("priceFrom")), currency="AUD",
            availability=Availability.IN_STOCK if p.get("isAvailable") else Availability.OUT_OF_STOCK,
            image=self._img(p.get("media")),
        )

    # ------------------------------------------------------------------ product
    async def get_item(self, id_or_url: str) -> Item:
        if id_or_url.startswith("http"):
            url = id_or_url
        elif id_or_url.startswith("/"):
            url = urljoin(self.base_url, id_or_url)
        else:
            url = f"{self.base_url}/p/{id_or_url}"

        async def produce():
            res = await self.fetcher.fetch(url)
            prod = None
            for q in self._queries(res.body):
                k = q.get("queryKey") or []
                if k and k[0] == "product" and isinstance(q.get("state", {}).get("data"), dict) and q["state"]["data"].get("id"):
                    prod = q["state"]["data"]
                    break
            tree = ex.parse(res.body)
            if not prod:
                ld = ex.json_ld_of_type(tree, "Product")
                if not ld:
                    raise ExtractionError(f"No product record on {url} - not a product page or layout changed.")
                return Item(id=str(ld.get("sku") or url), name=ld.get("name", ""), url=res.final_url,
                            brand=(ld.get("brand") or {}).get("name") if isinstance(ld.get("brand"), dict) else ld.get("brand"),
                            price=self._num((ld.get("offers") or {}).get("price")), currency="AUD",
                            description=(ld.get("description") or "")[:2000] or None)
            item = Item(
                id=str(prod["id"]), name=prod.get("name") or prod.get("title") or "", url=res.final_url,
                brand=prod.get("brand"), price=self._num(prod.get("priceFrom")), currency="AUD",
                availability=Availability.IN_STOCK if prod.get("isAvailable") else Availability.OUT_OF_STOCK,
                image=self._img(prod.get("media")),
                description=self._text(prod.get("longDescription") or prod.get("shortDescription")),
            )
            lp = self._num(prod.get("listPriceFrom"))
            if lp and item.price and lp > item.price:
                item.extras["was_price"] = lp
                item.extras["saved"] = round(lp - item.price, 2)
            for a in prod.get("attributes") or []:
                name = a.get("name")
                vals = [str(v.get("value") or v.get("identifier")) for v in (a.get("values") or []) if isinstance(v, dict)]
                if name and vals and name not in item.specs:
                    item.specs[str(name)] = ", ".join(vals)
            variants = prod.get("variants") or []
            sizes = sorted({str(v.get("size")) for v in variants if isinstance(v, dict) and v.get("size")})
            colours = sorted({str(v.get("colour")) for v in variants if isinstance(v, dict) and v.get("colour")})
            if sizes:
                item.extras["sizes"] = sizes
            if colours:
                item.extras["colours"] = colours
            ld = ex.json_ld_of_type(tree, "Product")
            if ld:
                ar = ld.get("aggregateRating") or {}
                if ar.get("ratingValue"):
                    item.rating = float(ar["ratingValue"])
                if ar.get("reviewCount"):
                    item.review_count = int(ar["reviewCount"])
            return item

        return await self.cached(("item", url), self.policy.cache_ttl_item_s, produce)

    async def categories(self, parent: str | None = None) -> list[Category]:
        async def produce():
            res = await self.fetcher.fetch(f"{self.base_url}/")
            out: list[Category] = []
            seen: set[str] = set()
            for q in self._queries(res.body):
                if (q.get("queryKey") or [None])[0] not in ("catalogue", "cms"):
                    continue
                data = q.get("state", {}).get("data")

                def walk(node: Any):
                    if isinstance(node, dict):
                        seo = node.get("seoToken") or node.get("url")
                        name = node.get("description") or node.get("name") or node.get("label")
                        if isinstance(seo, str) and name and seo not in seen and not seo.startswith("http"):
                            slug = seo if seo.startswith("/") else "/c/" + seo
                            seen.add(seo)
                            out.append(Category(name=str(name), slug=slug, url=urljoin(self.base_url, slug)))
                        for v in node.values():
                            walk(v)
                    elif isinstance(node, list):
                        for v in node:
                            walk(v)

                walk(data)
            if parent:
                pl = parent.lower()
                out = [c for c in out if pl in c.slug.lower() or pl in c.name.lower()]
            if not out:
                raise ExtractionError("No catalogue tree in Myer home state.")
            return out[:150]

        return await self.cached(("categories", parent), self.policy.cache_ttl_search_s, produce)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _num(v: Any) -> float | None:
        return float(v) if isinstance(v, (int, float)) else None

    @staticmethod
    def _text(html: str | None) -> str | None:
        if not html:
            return None
        txt = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", txt).strip()[:2000] or None

    @staticmethod
    def _img(media: Any) -> str | None:
        if isinstance(media, list) and media:
            m0 = media[0]
            base = m0.get("baseUrl") if isinstance(m0, dict) else None
            if base:
                return _IMG_BASE + base.replace("{{size}}", "500")
        return None
