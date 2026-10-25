"""JB Hi-Fi (jbhifi.com.au) adapter.

Verified live 2026-09-06. JB Hi-Fi is a Shopify (Hydrogen) storefront behind Cloudflare, with
search powered by Algolia. The Algolia key is a session-locked secured key - replaying it
directly 403s - so search runs through the browser tier and captures the response the PAGE
itself fetches (fetcher.fetch_capture). Product pages carry a clean schema.org Product JSON-LD.

  search:   /search?query=<q>&page=<n>   -> intercept the Algolia shopify_products_families call
  product:  /products/<handle>            -> JSON-LD Product

Browser-tier for both (Cloudflare + client rendering), so keep result counts modest.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import Availability, ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter


class Adapter(SiteAdapter):
    name = "jbhifi"
    domain = "jbhifi.com.au"
    base_url = "https://www.jbhifi.com.au"
    description = "JB Hi-Fi: tech, audio, appliances, entertainment - product search and detail."
    policy = FetchPolicy(start_tier=FetchTier.BROWSER, min_delay_s=2.0, jitter_s=1.0, timeout_s=40,
                         # JB Hi-Fi never goes networkidle; wait for the product cards / <h1> instead.
                         browser_wait_selector="h1")

    _CARD_JS = """() => {
      const cards = Array.from(document.querySelectorAll('[data-testid=\"product-card-content\"]'));
      const seen = new Set(); const out = [];
      for (const card of cards) {
        const a = card.querySelector('a[href^=\"/products/\"]'); if (!a) continue;
        const href = a.getAttribute('href'); if (!href || seen.has(href)) continue; seen.add(href);
        const txt = (card.innerText || '');
        const pm = txt.match(/\\$\\s?([0-9,]+(?:\\.[0-9][0-9])?)/);
        const wm = txt.match(/Was\\s*\\$\\s?([0-9,]+)/i);
        const img = card.querySelector('img');
        out.push({handle: href, title: (a.getAttribute('aria-label') || a.textContent || '').trim(),
                  price: pm ? pm[1].replace(/,/g,'') : null, was: wm ? wm[1].replace(/,/g,'') : null,
                  image: img ? (img.getAttribute('src') || img.getAttribute('data-src')) : null});
      }
      return out;
    }"""

    _PRODUCT_RE = re.compile(r"/products/([a-z0-9-]+)", re.I)

    def parse_url(self, url: str) -> dict[str, str] | None:
        m = self._PRODUCT_RE.search(url)
        if m:
            return {"kind": "item", "id": m.group(1)}
        if "/search" in url:
            return {"kind": "search", "id": url}
        return None

    def sort_options(self) -> list[str]:
        return ["relevance"]  # Algolia relevance; other sorts are JS-driven index swaps

    # ------------------------------------------------------------------ search
    async def search(self, query: str, *, page: int = 1, sort: str | None = None, filters: dict[str, str] | None = None) -> SearchPage:
        url = f"{self.base_url}/search?query={quote_plus(query)}"
        if page > 1:
            url += f"&page={page}"

        async def produce():
            res = await self.fetcher.fetch_dom(url, self._CARD_JS, wait_selector='[data-testid="product-card-content"]')
            cards = res.json_data or []
            sp = SearchPage(query=query, page=page, source_url=url)
            for c in cards:
                handle = (c.get("handle") or "").split("?")[0]
                if not handle:
                    continue
                item = ItemRef(
                    id=handle.rsplit("/", 1)[-1], name=c.get("title") or handle.rsplit("/", 1)[-1].replace("-", " "),
                    url=urljoin(self.base_url, handle), price=self._num(c.get("price")), currency="AUD",
                    image=c.get("image"),
                )
                if c.get("was") and item.price and self._num(c["was"]):
                    pass  # was-price kept on the product page; ItemRef has no extras
                sp.items.append(item)
            sp.page_size = len(sp.items)
            sp.has_more = False  # DOM shows one page; deeper paging is JS-driven
            if not sp.items:
                raise ExtractionError("No JB Hi-Fi product cards rendered - zero results, a Cloudflare block, "
                                      "or the card markup changed.")
            return sp

        return await self.cached(("search", url), self.policy.cache_ttl_search_s, produce)

    @staticmethod
    def _pick_result(payloads: Any) -> dict[str, Any]:
        """fetch_capture returns a LIST of captured /queries payloads (main search + suggestions +
        recommendations all hit the same endpoint). Flatten every result set and pick the
        shopify_products_families one with the most hits - that's the real query, not a rec/boost."""
        if isinstance(payloads, dict):
            payloads = [payloads]
        results: list[dict[str, Any]] = []
        for p in payloads or []:
            if isinstance(p, dict) and isinstance(p.get("results"), list):
                results.extend(r for r in p["results"] if isinstance(r, dict))
            elif isinstance(p, dict) and p.get("hits"):
                results.append(p)
        prod = [r for r in results if str(r.get("index", "")).startswith("shopify_products_families") and r.get("hits")]
        pool = prod or [r for r in results if r.get("hits")]
        return max(pool, key=lambda r: r.get("nbHits", 0), default={})

    def _ref(self, h: dict[str, Any]) -> ItemRef | None:
        handle = h.get("handle")
        sku = h.get("sku") or h.get("objectID")
        if not handle:
            return None
        brand = h.get("vendor")
        if not brand and isinstance(h.get("categoryBrand"), str) and "." in h["categoryBrand"]:
            brand = h["categoryBrand"].rsplit(".", 1)[-1]
        return ItemRef(
            id=str(sku or handle), name=h.get("title") or "", url=urljoin(self.base_url, f"/products/{handle}"),
            brand=brand or None, price=self._num(h.get("final_price") or h.get("price")), currency="AUD",
            availability=self._avail(h.get("availability")), rating=self._num(h.get("weighted_average_rating")),
            image=self._img(h),
        )

    # ------------------------------------------------------------------ product
    async def get_item(self, id_or_url: str) -> Item:
        if id_or_url.startswith("http"):
            url = id_or_url
        elif id_or_url.startswith("/"):
            url = urljoin(self.base_url, id_or_url)
        else:
            url = f"{self.base_url}/products/{id_or_url}"

        async def produce():
            # Browser tier (policy waits for <h1>, not networkidle - JB Hi-Fi polls forever).
            # The rendered page carries the schema.org Product JSON-LD.
            res = await self.fetcher.fetch(url)
            tree = ex.parse(res.body)
            prod = ex.json_ld_of_type(tree, "Product")
            if not prod:
                name = ex.text(tree, "h1") or ex.meta(tree, "og:title")
                if not name:
                    raise ExtractionError(f"No JSON-LD Product nor <h1> on {url} - Cloudflare block or layout changed.")
                return Item(id=(self.parse_url(url) or {}).get("id") or url, name=name, url=res.final_url,
                            currency="AUD", image=ex.meta(tree, "og:image"), description=ex.meta(tree, "og:description"))
            offer = prod.get("offers") or {}
            if isinstance(offer, list):
                offer = offer[0] if offer else {}
            item = Item(
                id=str(prod.get("sku") or (self.parse_url(url) or {}).get("id") or url), name=prod.get("name") or "",
                url=res.final_url,
                brand=(prod.get("brand") or {}).get("name") if isinstance(prod.get("brand"), dict) else (prod.get("brand") or None),
                price=self._num(offer.get("price")), currency=offer.get("priceCurrency") or "AUD",
                availability=self._avail_ld(str(offer.get("availability", ""))),
                image=prod.get("image") if isinstance(prod.get("image"), str) else (prod.get("image") or [None])[0] if isinstance(prod.get("image"), list) else None,
                description=(prod.get("description") or ex.meta(tree, "og:description") or "")[:2000] or None,
            )
            ar = prod.get("aggregateRating") or {}
            if ar.get("ratingValue"):
                item.rating = self._num(ar["ratingValue"])
            if ar.get("reviewCount"):
                item.review_count = int(ar["reviewCount"])
            if prod.get("mpn"):
                item.extras["mpn"] = prod["mpn"]
            for row in tree.css("table tr, .product-specifications tr"):
                cells = [c.text(strip=True) for c in row.css("th, td")]
                if len(cells) == 2 and cells[0] and len(cells[0]) < 60 and len(item.specs) < 40:
                    item.specs[cells[0]] = cells[1]
            return item

        return await self.cached(("item", url), self.policy.cache_ttl_item_s, produce)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _num(v: Any) -> float | None:
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _img(self, h: dict[str, Any]) -> str | None:
        for k in ("product_image", "imageUrl", "image_url", "image"):
            v = h.get(k)
            if isinstance(v, str) and v:
                return v if v.startswith("http") else "https:" + v if v.startswith("//") else urljoin(self.base_url, v)
            if isinstance(v, dict) and v.get("url"):
                return v["url"]
        return None

    @staticmethod
    def _avail(a: Any) -> Availability:
        if isinstance(a, dict):
            s = str(a.get("overallStatus") or a.get("deliveryStatus") or a.get("availabilityStatement") or "").lower()
        else:
            s = str(a or "").lower()
        if "instock" in s or "in stock" in s or "available" in s:
            return Availability.IN_STOCK
        if "outofstock" in s or "out of stock" in s or "sold" in s:
            return Availability.OUT_OF_STOCK
        if "preorder" in s or "pre-order" in s or "backorder" in s:
            return Availability.SPECIAL_ORDER
        return Availability.UNKNOWN

    @staticmethod
    def _avail_ld(a: str) -> Availability:
        a = a.lower()
        if "instock" in a:
            return Availability.IN_STOCK
        if "outofstock" in a or "soldout" in a:
            return Availability.OUT_OF_STOCK
        if "preorder" in a or "backorder" in a:
            return Availability.SPECIAL_ORDER
        return Availability.UNKNOWN
