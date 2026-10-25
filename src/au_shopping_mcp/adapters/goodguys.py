"""The Good Guys (thegoodguys.com.au) adapter - appliances & tech.

Verified live 2026-09-06. A Remix SPA: search results render client-side (parsed from the DOM
via the browser tier), but product pages are server-rendered with a schema.org Product JSON-LD
in the plain HTTP response, so get_product needs no browser.

  search:   /search?q=<q>          (browser DOM extraction of product cards)
  product:  /<slug>-<code>          (JSON-LD Product over plain HTTP)
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import Availability, ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter

# Product URLs are /<slug>-<code>; the code segment always contains a digit (e.g. -hd939690,
# -lke842pdb2ian1), which separates products from pure-word category links (/fridges-and-freezers).
_PRODUCT_RE = re.compile(r"^/[a-z0-9-]*[a-z]-[a-z0-9]*[0-9][a-z0-9]*$", re.I)

_CARD_JS = r'''() => {
  const rx = /^\/[a-z0-9-]*[a-z]-[a-z0-9]*[0-9][a-z0-9]*$/i;
  const isProd = (h) => rx.test((h||'').split('?')[0]);
  const seen = new Set(); const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    let h = (a.getAttribute('href') || '');
    if (h.startsWith('http')) h = h.replace(/^https?:\/\/[^/]+/, '');
    h = h.split('?')[0];
    if (!isProd(h) || seen.has(h)) continue; seen.add(h);
    let c = a, card = a;
    for (let i = 0; i < 8 && c.parentElement; i++) {
      c = c.parentElement;
      const links = [...c.querySelectorAll('a[href]')].map(x => (x.getAttribute('href')||'').replace(/^https?:\/\/[^/]+/, '').split('?')[0]).filter(isProd);
      card = c;
      if (/\$\s?[0-9]/.test(c.innerText||'') && new Set(links).size <= 1) break;
    }
    const t = card.innerText || '';
    const pm = t.match(/\$\s?([0-9,]+(?:\.[0-9][0-9])?)/);
    const img = card.querySelector('img');
    let title = (a.getAttribute('title') || a.getAttribute('aria-label') || a.textContent || '').trim().replace(/\s+/g,' ').replace(/^View details for\s*/i, '');
    out.push({href: h, title: title, price: pm ? pm[1].replace(/,/g,'') : null,
              image: img ? (img.getAttribute('src') || img.getAttribute('data-src')) : null});
    if (out.length >= 40) break;
  }
  return out;
}'''

class Adapter(SiteAdapter):
    name = "goodguys"
    domain = "thegoodguys.com.au"
    base_url = "https://www.thegoodguys.com.au"
    description = "The Good Guys: home appliances, kitchen, TV & audio, computers - product search and detail."
    policy = FetchPolicy(start_tier=FetchTier.HTTP, min_delay_s=1.5, jitter_s=0.8, timeout_s=40)

    def parse_url(self, url: str) -> dict[str, str] | None:
        path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
        if _PRODUCT_RE.match(path):
            return {"kind": "item", "id": path.lstrip("/")}
        if "/search" in url:
            return {"kind": "search", "id": url}
        return None

    def sort_options(self) -> list[str]:
        return ["relevance"]

    async def search(self, query: str, *, page: int = 1, sort: str | None = None, filters: dict[str, str] | None = None) -> SearchPage:
        url = f"{self.base_url}/search?q={quote_plus(query)}"
        if page > 1:
            url += f"&page={page}"

        async def produce():
            res = await self.fetcher.fetch_dom(url, _CARD_JS, wait_selector='a[href*="-"]')
            sp = SearchPage(query=query, page=page, source_url=url)
            for c in res.json_data or []:
                href = (c.get("href") or "").split("?")[0]
                if not href:
                    continue
                name = re.split(r"\s*\$[0-9]", (c.get("title") or ""))[0].strip()
                sp.items.append(ItemRef(
                    id=href.lstrip("/"), name=name or href.lstrip("/").replace("-", " "),
                    url=urljoin(self.base_url, href), price=self._num(c.get("price")), currency="AUD",
                    image=c.get("image"),
                ))
            sp.page_size = len(sp.items)
            sp.has_more = False
            if not sp.items:
                raise ExtractionError("No Good Guys product cards rendered - zero results or card markup changed.")
            return sp

        return await self.cached(("search", url), self.policy.cache_ttl_search_s, produce)

    async def get_item(self, id_or_url: str) -> Item:
        if id_or_url.startswith("http"):
            url = id_or_url
        else:
            url = urljoin(self.base_url, "/" + id_or_url.lstrip("/"))

        async def produce():
            res = await self.fetcher.fetch(url, tier=FetchTier.HTTP)  # JSON-LD is server-rendered
            tree = ex.parse(res.body)
            prod = ex.json_ld_of_type(tree, "Product")
            if not prod:
                name = ex.text(tree, "h1") or ex.meta(tree, "og:title")
                if not name:
                    raise ExtractionError(f"No JSON-LD Product nor <h1> on {url} - not a product page or layout changed.")
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
                availability=self._avail(str(offer.get("availability", ""))),
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
            for row in tree.css("table tr"):
                cells = [c.text(strip=True) for c in row.css("th, td")]
                if len(cells) == 2 and cells[0] and len(cells[0]) < 60 and len(item.specs) < 40:
                    item.specs[cells[0]] = cells[1]
            return item

        return await self.cached(("item", url), self.policy.cache_ttl_item_s, produce)

    @staticmethod
    def _num(v: Any) -> float | None:
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

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
