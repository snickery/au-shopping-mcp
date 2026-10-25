"""Cutter Electronics (cutter.com.au) adapter.

Verified live 2026-09-21. Cutter runs WooCommerce and leaves the **WooCommerce Store API**
open, so this is a tier-0 JSON adapter - no HTML parsing anywhere.

  search:     GET /wp-json/wc/store/v1/products?search=<q>&page=<n>&per_page=<n>&orderby=&order=
  category:   GET /wp-json/wc/store/v1/products?category=<term_id>&...
  product:    GET /wp-json/wc/store/v1/products/<id>   (or ?slug=<slug> from a permalink)
  categories: GET /wp-json/wc/store/v1/products/categories?per_page=100&page=<n>   (232 terms)

Paging comes from the `x-wp-total` / `x-wp-totalpages` response headers, not the body.
Prices are integer **minor units** with the scale in `prices.currency_minor_unit`
(`"5295"` + minor_unit 2 -> $52.95) - never read `prices.price` as dollars.
`orderby` is a fixed WooCommerce enum (see SORT_MAP); anything else 400s.

Category filtering needs the numeric term id, and the categories endpoint ignores `?slug=`,
so slugs/names are resolved client-side against the cached full category list.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from selectolax.parser import HTMLParser

from au_shopping_mcp.core.models import (
    Availability,
    Category,
    ExtractionError,
    FetchPolicy,
    FetchTier,
    Item,
    ItemRef,
    SearchPage,
)
from au_shopping_mcp.adapters.base import SiteAdapter

_PRODUCT_URL_RE = re.compile(r"/product/([a-z0-9-]+)/?", re.I)
_CATEGORY_URL_RE = re.compile(r"/product-category/([a-z0-9-]+(?:/[a-z0-9-]+)*)/?", re.I)


def _text(html: str | None, limit: int = 2000) -> str | None:
    """WooCommerce returns HTML fragments for descriptions; tools want text."""
    if not html:
        return None
    out = re.sub(r"\s+", " ", HTMLParser(html).text(separator=" ")).strip()
    return out[:limit] or None


class Adapter(SiteAdapter):
    name = "cutter"
    domain = "cutter.com.au"
    base_url = "https://www.cutter.com.au"
    description = ("Cutter Electronics: LED lighting components, drivers, optics, heatsinks and "
                   "electronics - product search, detail and category browse. Search matches "
                   "product titles and SKUs literally (a generic word like 'heatsink' can return "
                   "nothing while the category holds 54 items), so when a search comes back empty "
                   "try list_categories + browse_category, which also accepts a category name.")
    policy = FetchPolicy(start_tier=FetchTier.HTTP, min_delay_s=1.0, jitter_s=0.6)

    API = "/wp-json/wc/store/v1"
    PAGE_SIZE = 24
    # WooCommerce Store API enum; an unknown value is a 400 from the API, so validate first.
    SORT_MAP = {"relevance": (None, None), "popularity": ("popularity", "desc"),
                "rating_desc": ("rating", "desc"), "price_asc": ("price", "asc"),
                "price_desc": ("price", "desc"), "newest": ("date", "desc"),
                "name": ("title", "asc"), "name_desc": ("title", "desc")}

    # ------------------------------------------------------------------ urls / config
    def parse_url(self, url: str) -> dict[str, str] | None:
        m = _CATEGORY_URL_RE.search(url)
        if m:
            return {"kind": "category", "id": m.group(1).rstrip("/").split("/")[-1]}
        m = _PRODUCT_URL_RE.search(url)
        if m:
            return {"kind": "item", "id": m.group(1)}
        return None

    def sort_options(self) -> list[str]:
        return list(self.SORT_MAP)

    def _sort_params(self, sort: str | None) -> str:
        if sort and sort not in self.SORT_MAP:
            raise ValueError(f"unknown sort {sort!r}; one of {', '.join(self.SORT_MAP)}")
        orderby, order = self.SORT_MAP.get(sort or "relevance", (None, None))
        return f"&orderby={orderby}&order={order}" if orderby else ""

    def _api(self, path: str, query: str = "") -> str:
        return f"{self.base_url}{self.API}/{path.lstrip('/')}" + (f"?{query}" if query else "")

    # ------------------------------------------------------------------ search / browse
    async def search(self, query: str, *, page: int = 1, sort: str | None = None,
                     filters: dict[str, str] | None = None) -> SearchPage:
        q = f"search={quote_plus(query)}&page={page}&per_page={self.PAGE_SIZE}{self._sort_params(sort)}"
        url = self._api("products", q)

        async def produce():
            sp = await self._listing(url)
            sp.query, sp.page = query, page
            return sp

        return await self.cached(("search", url), self.policy.cache_ttl_search_s, produce)

    async def browse(self, category: str, *, page: int = 1, sort: str | None = None) -> SearchPage:
        """`category` is a term id, a slug, a category name, or a /product-category/... URL."""
        term = await self._resolve_category(category)
        q = f"category={term['id']}&page={page}&per_page={self.PAGE_SIZE}{self._sort_params(sort)}"
        url = self._api("products", q)

        async def produce():
            sp = await self._listing(url)
            sp.category, sp.page = f"{term['name']} ({term['slug']})", page
            return sp

        return await self.cached(("browse", url), self.policy.cache_ttl_search_s, produce)

    async def _listing(self, url: str) -> SearchPage:
        res = await self.fetcher.fetch_json(url)
        rows = res.json_data
        if not isinstance(rows, list):
            raise ExtractionError(
                f"Store API did not return a product list for {url} - the endpoint may have been "
                f"closed off or the parameters rejected. Run probe_page on a normal page to check."
            )
        sp = SearchPage(source_url=url, page_size=len(rows))
        total = res.headers.get("x-wp-total")
        pages = res.headers.get("x-wp-totalpages")
        sp.total = int(total) if total and total.isdigit() else None
        for row in rows:
            ref = self._ref(row)
            if ref:
                sp.items.append(ref)
        page_no = int(re.search(r"[?&]page=(\d+)", url).group(1)) if "page=" in url else 1
        sp.has_more = bool(pages and pages.isdigit() and page_no < int(pages))
        return sp

    async def categories(self, parent: str | None = None) -> list[Category]:
        terms = await self._all_categories()
        if parent:
            p = await self._resolve_category(parent)
            wanted = [t for t in terms if t.get("parent") == p["id"]]
        else:
            # Top level: WooCommerce marks roots with parent 0 (or a missing parent).
            wanted = [t for t in terms if not t.get("parent")]
        return [Category(name=t["name"], slug=t["slug"],
                         url=t.get("link") or urljoin(self.base_url, f"/product-category/{t['slug']}/"))
                for t in sorted(wanted, key=lambda t: t["name"])]

    async def _all_categories(self) -> list[dict[str, Any]]:
        async def produce():
            out: list[dict[str, Any]] = []
            for page in range(1, 6):  # 232 terms today; stop early when a page comes back short
                res = await self.fetcher.fetch_json(
                    self._api("products/categories", f"per_page=100&page={page}"))
                rows = res.json_data if isinstance(res.json_data, list) else []
                out.extend(rows)
                if len(rows) < 100:
                    break
            if not out:
                raise ExtractionError("Store API returned no product categories.")
            return out

        return await self.cached(("categories",), 6 * 3600, produce)

    async def _resolve_category(self, category: str) -> dict[str, Any]:
        """term id | slug | name | /product-category/<path>/ URL -> the category term."""
        raw = str(category).strip()
        if raw.isdigit():
            key = None
            term_id = int(raw)
        else:
            parsed = self.parse_url(raw) if "/" in raw else None
            key = (parsed or {}).get("id", raw).strip("/").lower()
            term_id = None
        for t in await self._all_categories():
            if term_id is not None and t.get("id") == term_id:
                return t
            if key and (t.get("slug", "").lower() == key or t.get("name", "").lower() == key):
                return t
        raise ValueError(f"unknown Cutter category {category!r} - list_categories shows the tree")

    # ------------------------------------------------------------------ product
    async def get_item(self, id_or_url: str) -> Item:
        raw = str(id_or_url).strip()
        if raw.isdigit():
            url = self._api(f"products/{raw}")
        else:
            parsed = self.parse_url(raw)
            slug = (parsed or {}).get("id") if (parsed or {}).get("kind") == "item" else (
                None if "/" in raw else raw)
            if not slug:
                raise ValueError(f"{id_or_url!r} is not a Cutter product id, slug or /product/<slug>/ URL")
            url = self._api("products", f"slug={quote_plus(slug)}")

        async def produce():
            res = await self.fetcher.fetch_json(url)
            data = res.json_data
            if isinstance(data, list):
                data = data[0] if data else None
            if not isinstance(data, dict) or not data.get("id"):
                raise ExtractionError(
                    f"No product at {url} - wrong id/slug, or the item was removed from the catalogue.")
            return self._item(data)

        return await self.cached(("item", url), self.policy.cache_ttl_item_s, produce)

    # ------------------------------------------------------------------ mapping
    @staticmethod
    def _price(prices: dict[str, Any], key: str = "price") -> float | None:
        """Store API money: integer minor units + the scale in currency_minor_unit."""
        raw = (prices or {}).get(key)
        if raw in (None, ""):
            return None
        try:
            return int(raw) / (10 ** int((prices or {}).get("currency_minor_unit", 2)))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _availability(row: dict[str, Any]) -> Availability:
        if row.get("is_on_backorder"):
            return Availability.SPECIAL_ORDER
        if not row.get("is_in_stock"):
            return Availability.OUT_OF_STOCK
        if row.get("low_stock_remaining"):
            return Availability.LOW_STOCK
        return Availability.IN_STOCK

    @staticmethod
    def _brand(row: dict[str, Any]) -> str | None:
        for b in row.get("brands") or []:
            if isinstance(b, dict) and b.get("name"):
                return b["name"]
        # Cutter mostly carries the manufacturer as a product attribute instead of a brand term.
        for attr in row.get("attributes") or []:
            if "manufacturer" in (attr.get("name") or "").lower():
                terms = [t.get("name") for t in attr.get("terms") or [] if t.get("name")]
                if terms:
                    return terms[0]
        return None

    def _ref(self, row: dict[str, Any]) -> ItemRef | None:
        if not row.get("id"):
            return None
        prices = row.get("prices") or {}
        ref = ItemRef(
            id=str(row["id"]), name=row.get("name") or "",
            url=row.get("permalink") or urljoin(self.base_url, f"/?p={row['id']}"),
            brand=self._brand(row),
            price=self._price(prices), currency=prices.get("currency_code") or "AUD",
            availability=self._availability(row),
            rating=float(row["average_rating"]) if (row.get("average_rating") or "0") not in ("", "0") else None,
            review_count=row.get("review_count") or None,
            image=((row.get("images") or [{}])[0] or {}).get("src"),
        )
        was = self._price(prices, "regular_price")
        if row.get("on_sale") and was and ref.price and was > ref.price:
            ref.extras["regular_price"] = was
            ref.extras["discount_pct"] = round((was - ref.price) / was * 100)
        if row.get("sku"):
            ref.extras["sku"] = row["sku"]
        if row.get("type") == "variable":
            ref.extras["variations"] = len(row.get("variations") or [])
        return ref

    def _item(self, row: dict[str, Any]) -> Item:
        prices = row.get("prices") or {}
        item = Item(
            id=str(row["id"]), name=row.get("name") or "",
            url=row.get("permalink") or urljoin(self.base_url, f"/?p={row['id']}"),
            brand=self._brand(row),
            price=self._price(prices), currency=prices.get("currency_code") or "AUD",
            availability=self._availability(row),
            rating=float(row["average_rating"]) if (row.get("average_rating") or "0") not in ("", "0") else None,
            review_count=row.get("review_count") or None,
            image=((row.get("images") or [{}])[0] or {}).get("src"),
            description=_text(row.get("description")) or _text(row.get("short_description")),
            category_path=[c.get("name") for c in row.get("categories") or [] if c.get("name")],
        )
        for attr in row.get("attributes") or []:
            name = attr.get("name")
            terms = ", ".join(t.get("name") for t in attr.get("terms") or [] if t.get("name"))
            if name and terms:
                item.specs[name] = terms
        for key, label in (("formatted_dimensions", "Dimensions"), ("formatted_weight", "Weight")):
            # WooCommerce renders unset dimensions/weight as the literal "N/A".
            if row.get(key) and str(row[key]).strip().upper() != "N/A":
                item.specs.setdefault(label, row[key])
        was = self._price(prices, "regular_price")
        if row.get("on_sale") and was and item.price and was > item.price:
            item.extras["regular_price"] = was
            item.extras["discount_pct"] = round((was - item.price) / was * 100)
        if row.get("sku"):
            item.extras["sku"] = row["sku"]
        if (row.get("stock_availability") or {}).get("text"):
            item.extras["stock_text"] = row["stock_availability"]["text"]
        if row.get("low_stock_remaining"):
            item.extras["low_stock_remaining"] = row["low_stock_remaining"]
        if row.get("type") == "variable":
            item.extras["variations"] = len(row.get("variations") or [])
            item.extras["variation_note"] = "Price is the base/from price; options change it."
        if prices.get("price_range"):
            item.extras["price_range"] = {
                "min": self._price({**prices, "price": (prices["price_range"] or {}).get("min_amount")}),
                "max": self._price({**prices, "price": (prices["price_range"] or {}).get("max_amount")}),
            }
        tags = [t.get("name") for t in row.get("tags") or [] if t.get("name")]
        if tags:
            item.extras["tags"] = tags[:20]
        images = [i.get("src") for i in row.get("images") or [] if i.get("src")]
        if len(images) > 1:
            item.extras["images"] = images[:10]
        return item
