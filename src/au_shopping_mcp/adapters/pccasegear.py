"""PC Case Gear (pccasegear.com) adapter - PC parts & peripherals.

Verified live 2026-09-06. A JS storefront: search results (/search?q=) render client-side and
are read from the DOM via the browser tier; product pages are server-rendered HTML (name in
<h1>, price + a spec table + og:image), no JSON-LD.

  search:   /search?q=<q>              (browser DOM extraction)
  product:  /products/<id>/<slug>       (server-rendered HTML over plain HTTP)
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import Availability, ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter

_PRODUCT_RE = re.compile(r"/products/(\d+)/([a-z0-9-]+)", re.I)

_CARD_JS = r'''() => {
  const rx = /\/products\/\d+\//;
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
    name = "pccasegear"
    domain = "pccasegear.com"
    base_url = "https://www.pccasegear.com"
    description = "PC Case Gear: PC components, peripherals and systems - product search and detail."
    policy = FetchPolicy(start_tier=FetchTier.HTTP, min_delay_s=1.5, jitter_s=0.8, timeout_s=40)

    def parse_url(self, url: str) -> dict[str, str] | None:
        m = _PRODUCT_RE.search(url)
        if m:
            return {"kind": "item", "id": m.group(1)}
        if "/category/" in url:
            return {"kind": "category", "id": url}
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
            res = await self.fetcher.fetch_dom(url, _CARD_JS, wait_selector='a[href*="/products/"]')
            sp = SearchPage(query=query, page=page, source_url=url)
            for c in res.json_data or []:
                href = (c.get("href") or "").split("?")[0]
                m = _PRODUCT_RE.search(href)
                if not m:
                    continue
                sp.items.append(ItemRef(
                    id=m.group(1), name=self._clean_name(c.get("title"), m.group(2)),
                    url=urljoin(self.base_url, href), price=self._num(c.get("price")), currency="AUD",
                    image=c.get("image"),
                ))
            sp.page_size = len(sp.items)
            sp.has_more = False
            if not sp.items:
                raise ExtractionError("No PC Case Gear product cards rendered - zero results or card markup changed.")
            return sp

        return await self.cached(("search", url), self.policy.cache_ttl_search_s, produce)

    async def get_item(self, id_or_url: str) -> Item:
        if id_or_url.startswith("http"):
            url = id_or_url
        elif id_or_url.startswith("/"):
            url = urljoin(self.base_url, id_or_url)
        elif id_or_url.isdigit():
            # bare product id: resolve slug via a redirect-friendly path
            url = f"{self.base_url}/products/{id_or_url}/x"
        else:
            url = urljoin(self.base_url, id_or_url)

        async def produce():
            res = await self.fetcher.fetch(url, tier=FetchTier.HTTP)
            tree = ex.parse(res.body)
            name = ex.text(tree, "h1") or ex.meta(tree, "og:title")
            if not name:
                raise ExtractionError(f"No <h1>/og:title on {url} - not a product page or layout changed.")
            name = re.sub(r"^Buy\s+|\s*\|\s*PC Case Gear.*$", "", name).strip()
            # sku often in og:title as [CODE]
            ogt = ex.meta(tree, "og:title") or ""
            skum = re.search(r"\[([A-Za-z0-9._-]+)\]", ogt)
            price = None
            for sel in (".price", ".product-price", "[class*='price']"):
                p = ex.parse_price(ex.text(tree, sel))
                if p:
                    price = p
                    break
            if price is None:
                m = re.search(r"\$([0-9,]+(?:\.\d\d)?)", res.body)
                price = ex.parse_price(m.group(0)) if m else None
            item = Item(
                id=str((self.parse_url(url) or {}).get("id") or (skum.group(1) if skum else url)),
                name=name, url=res.final_url, price=price, currency="AUD",
                availability=self._avail(res.body),
                image=ex.meta(tree, "og:image"), description=ex.meta(tree, "og:description"),
            )
            if skum:
                item.extras["sku"] = skum.group(1)
            for row in tree.css("table tr"):
                cells = [c.text(strip=True) for c in row.css("th, td")]
                if len(cells) == 2 and cells[0] and len(cells[0]) < 60 and len(item.specs) < 40:
                    item.specs[cells[0]] = cells[1]
            return item

        return await self.cached(("item", url), self.policy.cache_ttl_item_s, produce)

    @staticmethod
    def _clean_name(title: str | None, slug: str) -> str:
        t = (title or "").strip()
        # the anchor wraps the whole card, so textContent trails price/stock badges - cut them.
        t = re.split(r"\s*\$[0-9]", t)[0]
        t = re.sub(r"\s*(In Stock Now!?|Pre-?Order!?|On Sale!?|Anniversary Edition!?)\s*$", "", t, flags=re.I).strip(" !")
        return t if len(t) >= 4 else slug.replace("-", " ")

    @staticmethod
    def _num(v: Any) -> float | None:
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _avail(body: str) -> Availability:
        b = body.lower()
        if "out of stock" in b or "sold out" in b:
            return Availability.OUT_OF_STOCK
        if "in stock" in b or "add to cart" in b:
            return Availability.IN_STOCK
        if "pre-order" in b or "preorder" in b:
            return Availability.SPECIAL_ORDER
        return Availability.UNKNOWN
