"""Sydney Tools (sydneytools.com.au) adapter - power tools / trade gear.

Verified live 2026-09-06. Sydney Tools is a Relay/GraphQL SPA; search results come from a single
POST /graphql call (captured from the site and replayed here) that returns rich product nodes -
no HTML scraping, no bot protection (tier 0, JSON). Product pages are SPA shells with no JSON-LD,
so get_product resolves an item by re-querying search and matching its urlSlug.

  search:  POST /graphql  routes_SearchView_Query(count, cursor, query, filters, orderBy, page)
           -> data.viewer.products.edges[].node
  product: /product/<urlSlug>
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus, urljoin

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import Availability, ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter

# Captured verbatim from the live storefront (routes_SearchView_Query + its Relay fragments).
_SEARCH_QUERY = 'query routes_SearchView_Query(\n  $count: Int!\n  $cursor: String\n  $query: String!\n  $filters: String\n  $orderBy: OrderBy\n  $page: Int\n) {\n  viewer {\n    ...SearchView_viewer_1MtKB9\n    id\n  }\n}\n\nfragment CmsBanner_viewer on Customer {\n  cmsBanners(first: 999) {\n    edges {\n      node {\n        id\n        title\n        link\n        urlPaths\n        categoryBig\n        categorySmall\n      }\n    }\n  }\n}\n\nfragment List_viewer on Customer {\n  id\n  freeShippingAmount\n  ...ProductCard_viewer\n}\n\nfragment ProductCard_product on Product {\n  id\n  alerts\n  name\n  type\n  configurables(first: 99) {\n    edges {\n      node {\n        id\n        type\n        discountCode\n        name\n        model\n        secondModel\n        sku\n        regularPrice\n        price\n        priceMatch\n        sellable\n        nonStock\n      }\n    }\n  }\n  model\n  secondModel\n  regularPrice\n  price\n  priceDrop\n  sku\n  urlSlug\n  freeShipping\n  priorityShipping {\n    getByDate\n  }\n  globalFreeShipping\n  preorderDate\n  backInStockDate\n  nonStock\n  onlineOnly\n  pickupOnly\n  callForPrice\n  inStoreOnly\n  sellable\n  discountCode\n  brand {\n    id\n    name\n  }\n  loyaltyPoints {\n    accruing\n    dollars\n  }\n  labels(first: 999) {\n    edges {\n      node {\n        id\n        imageUrl\n        placement\n        placementProductListing\n        placementProductView\n        type\n      }\n    }\n  }\n  mainImage {\n    id\n    thumbnail\n  }\n  priceGreen\n}\n\nfragment ProductCard_viewer on Customer {\n  id\n  freeShippingAmount\n}\n\nfragment SearchView_viewer_1MtKB9 on Customer {\n  ...List_viewer\n  ...CmsBanner_viewer\n  configs\n  products(first: $count, orderBy: $orderBy, after: $cursor, query: $query, filters: $filters, page: $page) {\n    totalCount\n    facets {\n      __typename\n      __isFacetType: __typename\n      ... on Facet {\n        key\n        name\n        position\n        buckets {\n          key\n          docCount\n          imageUrl\n        }\n      }\n      ... on FacetStats {\n        key\n        name\n        position\n        buckets {\n          key\n          docCount\n          to\n          from\n        }\n        max\n        min\n      }\n    }\n    canonicalLink\n    redirect\n    edges {\n      node {\n        id\n        ...ProductCard_product\n        __typename\n      }\n      cursor\n    }\n    pageInfo {\n      endCursor\n      hasNextPage\n    }\n  }\n}\n'


class Adapter(SiteAdapter):
    name = "sydneytools"
    domain = "sydneytools.com.au"
    base_url = "https://sydneytools.com.au"
    description = "Sydney Tools: power tools, hand tools and trade gear - product search and detail."
    policy = FetchPolicy(start_tier=FetchTier.HTTP, min_delay_s=1.2, jitter_s=0.6)

    HITS = 40

    def parse_url(self, url: str) -> dict[str, str] | None:
        import re
        m = re.search(r"/product/([a-z0-9-]+)", url, re.I)
        if m:
            return {"kind": "item", "id": m.group(1)}
        if "/search" in url:
            return {"kind": "search", "id": url}
        return None

    def sort_options(self) -> list[str]:
        return ["relevance"]  # the GraphQL orderBy enum is not exposed; default relevance

    async def _graphql(self, query: str, page: int) -> dict[str, Any]:
        body = {"query": _SEARCH_QUERY,
                "variables": {"count": self.HITS, "cursor": None, "query": query,
                              "filters": None, "orderBy": None, "page": page}}
        res = await self.fetcher.fetch_json(
            f"{self.base_url}/graphql", method="POST", json_body=body,
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "Accept-Encoding": "gzip, deflate", "Origin": self.base_url, "Referer": f"{self.base_url}/search"})
        return res.json_data or {}

    async def search(self, query: str, *, page: int = 1, sort: str | None = None, filters: dict[str, str] | None = None) -> SearchPage:
        async def produce():
            data = await self._graphql(query, page)
            products = (((data.get("data") or {}).get("viewer") or {}).get("products") or {})
            sp = SearchPage(query=query, page=page, source_url=f"{self.base_url}/search?q={quote_plus(query)}")
            for edge in products.get("edges") or []:
                ref = self._ref(edge.get("node") or {})
                if ref:
                    sp.items.append(ref)
            sp.total = products.get("totalCount")
            sp.page_size = len(sp.items)
            pi = products.get("pageInfo") or {}
            sp.has_more = bool(pi.get("hasNextPage")) or bool(sp.total and page * self.HITS < sp.total)
            if not sp.items and products.get("totalCount") is None:
                raise ExtractionError("Sydney Tools GraphQL returned no products/totalCount - the query shape "
                                      "changed. Re-capture routes_SearchView_Query.")
            return sp

        return await self.cached(("search", query, page), self.policy.cache_ttl_search_s, produce)

    def _ref(self, n: dict[str, Any]) -> ItemRef | None:
        slug = n.get("urlSlug")
        if not slug:
            return None
        brand = n.get("brand")
        img = n.get("mainImage") or {}
        price = None if n.get("callForPrice") else self._num(n.get("price"))
        return ItemRef(
            id=str(n.get("sku") or n.get("id") or slug), name=n.get("name") or "",
            url=urljoin(self.base_url, f"/product/{slug}"),
            brand=brand.get("name") if isinstance(brand, dict) else brand,
            price=price, currency="AUD", availability=self._avail(n),
            image=img.get("thumbnail") or img.get("url") if isinstance(img, dict) else None,
        )

    async def get_item(self, id_or_url: str) -> Item:
        slug = (self.parse_url(id_or_url) or {}).get("id") or id_or_url.rstrip("/").split("/")[-1]
        words = slug.replace("-", " ")

        async def produce():
            data = await self._graphql(words, 1)
            products = (((data.get("data") or {}).get("viewer") or {}).get("products") or {})
            nodes = [e.get("node") or {} for e in products.get("edges") or []]
            n = next((x for x in nodes if x.get("urlSlug") == slug), nodes[0] if nodes else None)
            if not n:
                raise ExtractionError(f"No Sydney Tools product matched {slug!r}.")
            brand = n.get("brand")
            img = n.get("mainImage") or {}
            item = Item(
                id=str(n.get("sku") or slug), name=n.get("name") or "", url=urljoin(self.base_url, f"/product/{n.get('urlSlug', slug)}"),
                brand=brand.get("name") if isinstance(brand, dict) else brand,
                price=None if n.get("callForPrice") else self._num(n.get("price")), currency="AUD",
                availability=self._avail(n), image=img.get("thumbnail") if isinstance(img, dict) else None,
            )
            if n.get("model"):
                item.specs["Model"] = str(n["model"])
            rp = self._num(n.get("regularPrice"))
            if rp and item.price and rp > item.price:
                item.extras["was_price"] = rp
            if n.get("callForPrice"):
                item.extras["call_for_price"] = True
            for flag in ("inStoreOnly", "onlineOnly", "pickupOnly", "freeShipping", "preorderDate"):
                if n.get(flag):
                    item.extras[flag] = n[flag]
            return item

        return await self.cached(("item", slug), self.policy.cache_ttl_item_s, produce)

    @staticmethod
    def _num(v: Any) -> float | None:
        return float(v) if isinstance(v, (int, float)) else None

    @staticmethod
    def _avail(n: dict[str, Any]) -> Availability:
        if n.get("nonStock") or n.get("sellable") is False:
            return Availability.OUT_OF_STOCK
        if n.get("preorderDate"):
            return Availability.SPECIAL_ORDER
        if n.get("inStoreOnly") or n.get("pickupOnly"):
            return Availability.LOW_STOCK
        if n.get("sellable"):
            return Availability.IN_STOCK
        return Availability.UNKNOWN
