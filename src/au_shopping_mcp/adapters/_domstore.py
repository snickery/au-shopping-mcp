"""DomStoreAdapter - shared base for retail sites that expose products as HTML/DOM (no clean
JSON search API). Subclass and set a handful of class attrs; search and get_product are generic.

Search is adaptive: it first parses the plain-HTTP search HTML (fast, for server-rendered sites);
if that yields too few products it re-fetches through the browser tier and reads the rendered
product cards (for client-rendered SPAs). get_product prefers the product page's schema.org
Product JSON-LD, falling back to <h1> + a price/spec scrape, and escalates HTTP->browser if the
plain response carried nothing.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import Availability, ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter

_PRICE = re.compile(r"\$\s?([0-9][0-9,]*(?:\.\d{2})?)")


def _card_js(link_re: str) -> str:
    return r'''() => {
  const rx = new RegExp(%r);
  const isProd = (h) => { h = (h||'').replace(/^https?:\/\/[^/]+/, '').split('?')[0]; return rx.test(h); };
  const seen = new Set(); const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    let h = (a.getAttribute('href') || ''); const path = h.replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    if (!isProd(h) || seen.has(path)) continue; seen.add(path);
    let c = a, card = a;
    for (let i = 0; i < 8 && c.parentElement; i++) {
      c = c.parentElement;
      const links = [...c.querySelectorAll('a[href]')].map(x=>x.getAttribute('href')||'').filter(isProd);
      card = c;
      if (/\$\s?[0-9]/.test(c.innerText||'') && new Set(links.map(x=>x.replace(/^https?:\/\/[^/]+/,'').split('?')[0])).size <= 1) break;
    }
    const t = card.innerText || '';
    const pm = t.match(/\$\s?([0-9,]+(?:\.[0-9][0-9])?)/);
    const img = card.querySelector('img');
    out.push({href: path, title: (a.getAttribute('title')||a.getAttribute('aria-label')||a.textContent||'').trim().replace(/\s+/g,' '),
              price: pm ? pm[1].replace(/,/g,'') : null, image: img?(img.getAttribute('src')||img.getAttribute('data-src')):null});
    if (out.length >= 40) break;
  }
  return out;
}''' % link_re


class DomStoreAdapter(SiteAdapter):
    # --- subclass config ---
    SEARCH_URL: str = ""             # e.g. "{base}/search?q={q}&page={page}"
    PRODUCT_LINK_RE: str = r"/p/"    # regex tested against the URL path
    WAIT_SELECTOR: str | None = None  # DOM selector to await when using the browser tier
    SEARCH_QUOTE_PLUS: bool = True    # False -> %20 path encoding (for path-based search URLs)
    PRODUCT_BROWSER: bool = False    # True -> get_product uses only the browser tier (Cloudflare sites
                                     # whose plain-HTTP product page is a challenge/partial without JSON-LD)
    NAME_STRIP: str = r"$^"          # regex removed from scraped names (site suffix noise)
    policy = FetchPolicy(start_tier=FetchTier.HTTP, min_delay_s=1.5, jitter_s=0.8, timeout_s=40)

    def __init__(self) -> None:
        super().__init__()
        self._link_re = re.compile(self.PRODUCT_LINK_RE, re.I)

    # --- required overrides usually left as-is ---
    def parse_url(self, url: str) -> dict[str, str] | None:
        path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
        if self._link_re.search(path):
            nums = re.findall(r"(\d{3,})", path)  # product id is the trailing number, not a slug digit
            return {"kind": "item", "id": nums[-1] if nums else path}
        return None

    def sort_options(self) -> list[str]:
        return ["relevance"]

    def _search_url(self, query: str, page: int) -> str:
        from urllib.parse import quote
        q = quote_plus(query) if self.SEARCH_QUOTE_PLUS else quote(query, safe="")
        return self.SEARCH_URL.format(base=self.base_url, q=q, page=page)

    # --- search (adaptive HTTP -> browser) ---
    async def search(self, query: str, *, page: int = 1, sort: str | None = None, filters: dict[str, str] | None = None) -> SearchPage:
        url = self._search_url(query, page)

        async def produce():
            cards: list[dict[str, Any]] = []
            try:
                res = await self.fetcher.fetch(url, tier=FetchTier.HTTP)
                cards = self._cards_from_html(res.body)
            except Exception:
                cards = []
            if len(cards) < 2:
                # Some SPAs serve an empty grid to a cold browser session; retry once warmer.
                for _ in range(2):
                    res = await self.fetcher.fetch_dom(url, _card_js(self.PRODUCT_LINK_RE), wait_selector=self.WAIT_SELECTOR)
                    cards = res.json_data or []
                    if len(cards) >= 2:
                        break
            sp = SearchPage(query=query, page=page, source_url=url)
            seen: set[str] = set()
            for c in cards:
                href = (c.get("href") or "").split("?")[0]
                if not href or href in seen or not self._link_re.search(href):
                    continue
                seen.add(href)
                name = re.sub(self.NAME_STRIP, "", c.get("title") or "").strip() or href.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
                sp.items.append(ItemRef(
                    id=(self.parse_url(href) or {}).get("id") or href, name=name[:200],
                    url=urljoin(self.base_url, href), price=self._num(c.get("price")), currency="AUD",
                    image=self._abs(c.get("image")),
                ))
            sp.page_size = len(sp.items)
            sp.has_more = len(sp.items) >= 20
            if not sp.items:
                raise ExtractionError(f"No products parsed for {self.name} search - zero results, a block, or the "
                                      f"page/link pattern changed. Run probe_page on {url}.")
            return sp

        return await self.cached(("search", url), self.policy.cache_ttl_search_s, produce)

    def _cards_from_html(self, html: str) -> list[dict[str, Any]]:
        """Server-rendered path: find product anchors, climb to the smallest card holding a price."""
        tree = ex.parse(html)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for a in tree.css("a[href]"):
            href = a.attributes.get("href") or ""
            path = re.sub(r"^https?://[^/]+", "", href).split("?")[0]
            if not self._link_re.search(path) or path in seen:
                continue
            node = a
            card = a
            for _ in range(6):
                node = node.parent
                if node is None:
                    break
                card = node
                if _PRICE.search(card.text() or ""):
                    break
            price_m = _PRICE.search(card.text() or "")
            img = card.css_first("img")
            title = (a.attributes.get("title") or a.text(strip=True) or "").strip()
            if not title:
                continue
            seen.add(path)
            out.append({"href": path, "title": title,
                        "price": price_m.group(1).replace(",", "") if price_m else None,
                        "image": (img.attributes.get("src") or img.attributes.get("data-src")) if img else None})
            if len(out) >= 40:
                break
        return out

    # --- product ---
    async def get_item(self, id_or_url: str) -> Item:
        url = id_or_url if id_or_url.startswith("http") else urljoin(self.base_url, "/" + id_or_url.lstrip("/"))

        async def produce():
            tiers = (FetchTier.BROWSER,) if self.PRODUCT_BROWSER else (FetchTier.HTTP, FetchTier.BROWSER)
            item = None
            for tier in tiers:
                res = await self.fetcher.fetch(url, tier=tier)
                item = self._item_from_html(res.body, res.final_url)
                # Prefer a result backed by JSON-LD; only accept an HTTP h1-scrape when there is
                # no browser tier left to try (a Cloudflare-served HTTP page can carry a stray price).
                if item and (tier == tiers[-1] or "json_ld" in item.extras):
                    return item
            if not item or not item.name:
                raise ExtractionError(f"No product data on {url} - not a product page, a block, or layout changed.")
            return item

        return await self.cached(("item", url), self.policy.cache_ttl_item_s, produce)

    def _item_from_html(self, html: str, url: str) -> Item | None:
        tree = ex.parse(html)
        prod = ex.json_ld_of_type(tree, "Product")
        if prod and prod.get("name"):
            offer = prod.get("offers") or {}
            if isinstance(offer, list):
                offer = offer[0] if offer else {}
            item = Item(
                id=str(prod.get("sku") or (self.parse_url(url) or {}).get("id") or url),
                name=re.sub(self.NAME_STRIP, "", prod.get("name") or "").strip(), url=url,
                brand=(prod.get("brand") or {}).get("name") if isinstance(prod.get("brand"), dict) else (prod.get("brand") or None),
                price=self._num(offer.get("price") or offer.get("lowPrice")), currency=offer.get("priceCurrency") or "AUD",
                availability=self._avail_ld(str(offer.get("availability", ""))),
                image=prod.get("image") if isinstance(prod.get("image"), str) else (prod.get("image") or [None])[0] if isinstance(prod.get("image"), list) else None,
                description=(prod.get("description") or "")[:2000] or None,
            )
            item.extras["json_ld"] = True
            if item.price is None:
                item.price = self._num(ex.meta(tree, "product:price:amount") or ex.meta(tree, "og:price:amount"))
                if item.price is None:
                    for sel in ("[data-testid*='price']", ".product-price", ".price--current", ".price"):
                        p2 = ex.parse_price(ex.text(tree, sel))
                        if p2:
                            item.price = p2
                            break
            ar = prod.get("aggregateRating") or {}
            if ar.get("ratingValue"):
                item.rating = self._num(ar["ratingValue"])
            if ar.get("reviewCount"):
                item.review_count = int(ar["reviewCount"]) if str(ar["reviewCount"]).isdigit() else None
        else:
            name = ex.text(tree, "h1") or ex.meta(tree, "og:title")
            if not name:
                return None
            name = re.sub(self.NAME_STRIP, "", name).strip()
            price = self._num(ex.meta(tree, "product:price:amount") or ex.meta(tree, "og:price:amount"))
            if price is None:
                for sel in ("[data-testid*='price']", "[class*='product'][class*='price']", ".product-price", ".price--current", ".price"):
                    price = ex.parse_price(ex.text(tree, sel))
                    if price:
                        break
            if price is None:
                m = _PRICE.search(html)
                price = self._num(m.group(1).replace(",", "")) if m else None
            item = Item(id=(self.parse_url(url) or {}).get("id") or url, name=name, url=url, price=price, currency="AUD",
                        image=ex.meta(tree, "og:image"), description=ex.meta(tree, "og:description"),
                        availability=self._avail_body(html))
        for row in tree.css("table tr"):
            cells = [c.text(strip=True) for c in row.css("th, td")]
            if len(cells) == 2 and cells[0] and len(cells[0]) < 60 and len(item.specs) < 40:
                item.specs[cells[0]] = cells[1]
        return item

    # --- helpers ---
    @staticmethod
    def _num(v: Any) -> float | None:
        try:
            return float(str(v).replace(",", "")) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _abs(self, u: Any) -> str | None:
        if not isinstance(u, str) or not u:
            return None
        if u.startswith("//"):
            return "https:" + u
        return urljoin(self.base_url, u) if u.startswith("/") else u

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

    @staticmethod
    def _avail_body(body: str) -> Availability:
        b = body.lower()
        if "out of stock" in b or "sold out" in b:
            return Availability.OUT_OF_STOCK
        if "in stock" in b or "add to cart" in b or "add to bag" in b:
            return Availability.IN_STOCK
        return Availability.UNKNOWN
