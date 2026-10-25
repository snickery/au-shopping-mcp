"""Bunnings (bunnings.com.au) adapter.

Verified against the live site 2026-09-06. Bunnings is a Next.js app that server-renders
every page with a react-query cache in `__NEXT_DATA__.props.pageProps.dehydratedState`, so
the plain HTTP tier already carries all the structured data — no Chromium needed, no JSON-LD
on any page.

Extraction order per page:
  1. Hydration state (react-query cache) — the primary and verified source:
       listings:  query key  ["coveo-search" | "category-search", <path>, <region>, <storeId>, {body}]
                  (since ~2026-10-25 prefixed: ["server-prefetch", "coveo-search", ...]; _rq_data drops it)
                  data.results[].raw = Coveo product document, data.totalCount
       product:   ["retail-product", <item>, <storeId>]         catalogue record
                  ["product-retail-price", <item>, <storeId>]   store-scoped price
                  ["product-fulfilment", <item>, <storeId>, …]  store stock / click&collect / delivery
                  ["product-aisle-item-location", <item>, <storeId>]  aisle + bay
  2. JSON-LD (schema.org Product / ItemList) — not currently emitted by Bunnings; kept as a
     cheap fallback should they add it.
  3. CSS selectors — last resort, expect to re-check after site changes.

Store scoping: the SSR data is scoped by the cookies `storeId` (4-digit store code, e.g. 7313 =
Alexandria NSW) AND `regionCode` (pricing region, e.g. NSWMetro). Both are required — a bare
storeId is ignored and the site falls back to its default (6400 Mentone, VICMetro). Store codes
are the `name` field on https://www.bunnings.com.au/stores/<state>/<suburb>.

URL conventions: search `/search/products?q=<q>&page=<n>&sort=<SortKey>`, category
`/products/<path>?page=<n>&sort=<SortKey>`, product `/<slug>_p<itemNumber>` (`/_p<itemNumber>`
redirects to the canonical slug).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.models import Availability, Category, ExtractionError, FetchPolicy, FetchTier, Item, ItemRef, SearchPage
from au_shopping_mcp.adapters.base import SiteAdapter

log = logging.getLogger("au_shopping_mcp.bunnings")

_PRODUCT_URL_RE = re.compile(r"_p(\d{7,})(?:[/?#]|$)")
_ITEM_NUMBER_RE = re.compile(r"^\d{7,}$")

LISTING_QUERY_KEYS = ("coveo-search", "category-search")
# Namespaces the site puts in front of a query key (server-side prefetched listings,
# since ~2026-10-25). Dropped before matching, so the key positions above still hold.
QUERY_KEY_PREFIXES = ("server-prefetch",)


class Adapter(SiteAdapter):
    name = "bunnings"
    domain = "bunnings.com.au"
    base_url = "https://www.bunnings.com.au"
    description = "Bunnings Warehouse AU product search, product detail and category browsing. Prices/stock are store-scoped."
    policy = FetchPolicy(
        start_tier=FetchTier.HTTP,
        min_delay_s=1.2,
        jitter_s=0.8,
        max_concurrency=2,
        # Every page is fully server-rendered (verified); a JS-only shell would show up as a
        # page with no __NEXT_DATA__, which _parse_listing reports as an ExtractionError.
        browser_wait_selector=None,
    )

    SEARCH_PATH = "/search/products"
    # URL `sort=` values (site's own keys, from its sort-options config).
    SORT_MAP = {
        "relevance": None,
        "price_asc": "PriceAscending",
        "price_desc": "PriceDescending",
        "rating_desc": "RatingDescending",
        "rating_asc": "RatingAscending",
        "name": "NameAscending",
        "name_desc": "NameDescending",
    }

    # Store scoping — both cookies are required for the site to honour the selection.
    STORE_ID = os.getenv("BUNNINGS_STORE_ID")            # e.g. "7313" (Alexandria NSW)
    REGION = os.getenv("BUNNINGS_REGION")                # e.g. "NSWMetro"
    STORE_COOKIE_NAME = "storeId"
    REGION_COOKIE_NAME = "regionCode"

    # Selector fallbacks, only reached when the hydration state and JSON-LD both yield nothing.
    SELECTORS = {
        "search_card": "[data-locator='product-tile'], article[class*='product']",
        "card_link": "a[href*='_p']",
        "card_name": "[data-locator='product-title'], h3, h4",
        "card_price": "[data-locator='product-price'], [class*='price']",
        "pdp_name": "h1",
        "pdp_price": "[data-locator='product-price'], [class*='price']",
        "pdp_specs_rows": "table tr",
    }

    def __init__(self) -> None:
        super().__init__()
        # (region, storeId) the site actually applied on the last parsed page — lets
        # site_status prove whether the configured store took effect.
        self._last_seen_store: tuple[str, str] | None = None
        if self.STORE_ID and not self.REGION:
            log.warning("BUNNINGS_STORE_ID=%s set without BUNNINGS_REGION - the site ignores a bare storeId "
                        "and will use its default store", self.STORE_ID)

    # ------------------------------------------------------------------ url helpers
    def parse_url(self, url: str) -> dict[str, str] | None:
        m = _PRODUCT_URL_RE.search(url)
        if m:
            return {"kind": "item", "id": m.group(1)}
        if self.SEARCH_PATH in url:
            return {"kind": "search", "id": url}
        if "/products/" in url or "/our-range/" in url:
            return {"kind": "category", "id": url}
        return None

    def _item_url(self, id_or_url: str) -> str:
        if id_or_url.startswith("http"):
            return id_or_url
        if _ITEM_NUMBER_RE.match(id_or_url):
            return f"{self.base_url}/_p{id_or_url}"  # 200 + canonical slug (verified)
        return urljoin(self.base_url, id_or_url)

    def _sort_param(self, sort: str | None) -> str:
        if sort and sort not in self.SORT_MAP:
            raise ValueError(f"unknown sort {sort!r}; one of {', '.join(self.SORT_MAP)}")
        s = self.SORT_MAP.get(sort or "relevance")
        return f"&sort={s}" if s else ""

    def _search_url(self, query: str, page: int, sort: str | None) -> str:
        return f"{self.base_url}{self.SEARCH_PATH}?q={quote_plus(query)}&page={page}{self._sort_param(sort)}"

    def _store_cookies(self) -> dict[str, str]:
        if self.STORE_ID and self.REGION:
            return {self.STORE_COOKIE_NAME: self.STORE_ID, self.REGION_COOKIE_NAME: self.REGION}
        return {}

    def sort_options(self) -> list[str]:
        return list(self.SORT_MAP)

    def site_specific_status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "store": {
                "configured_store_id": self.STORE_ID,
                "configured_region": self.REGION,
                "effective": (f"{self._last_seen_store[1]} ({self._last_seen_store[0]})" if self._last_seen_store
                              else "(no page parsed yet)"),
            }
        }
        if not (self.STORE_ID and self.REGION):
            out["store"]["note"] = ("Set BUNNINGS_STORE_ID (4-digit code from /stores/<state>/<suburb>) and "
                                    "BUNNINGS_REGION (e.g. NSWMetro); prices/stock default to the site's fallback store otherwise.")
        return out

    # ------------------------------------------------------------------ search / browse
    async def search(self, query: str, *, page: int = 1, sort: str | None = None, filters: dict[str, str] | None = None) -> SearchPage:
        url = self._search_url(query, page, sort)
        if filters:
            url += "".join(f"&{k}={quote_plus(v)}" for k, v in filters.items())

        async def produce():
            res = await self.fetcher.fetch(url, cookies=self._store_cookies())
            sp = self._parse_listing(res.body, res.final_url)
            sp.query, sp.page = query, page
            return sp

        return await self.cached(("search", url, self.STORE_ID, self.REGION), self.policy.cache_ttl_search_s, produce)

    async def browse(self, category: str, *, page: int = 1, sort: str | None = None) -> SearchPage:
        url = category if category.startswith("http") else urljoin(self.base_url, category)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}page={page}{self._sort_param(sort)}"

        async def produce():
            res = await self.fetcher.fetch(url, cookies=self._store_cookies())
            sp = self._parse_listing(res.body, res.final_url)
            sp.category, sp.page = category, page
            return sp

        return await self.cached(("browse", url, self.STORE_ID, self.REGION), self.policy.cache_ttl_search_s, produce)

    # ---- hydration helpers ----
    @staticmethod
    def _rq_queries(state: dict[str, Any] | None) -> list[dict[str, Any]]:
        """react-query dehydrated cache entries: [{queryKey: [...], state: {data: ...}}, ...]"""
        if not state:
            return []
        return (((state.get("props") or {}).get("pageProps") or {}).get("dehydratedState") or {}).get("queries") or []

    @classmethod
    def _rq_data(cls, state: dict[str, Any] | None, key0: str) -> tuple[list[Any], Any] | None:
        for q in cls._rq_queries(state):
            key = q.get("queryKey") or []
            if key and key[0] in QUERY_KEY_PREFIXES:
                key = key[1:]
            if key and key[0] == key0:
                return key, (q.get("state") or {}).get("data")
        return None

    def _note_store(self, key: list[Any]) -> None:
        # listing keys: [name, path, region, storeId, body]; product keys: [name, item, storeId]
        if len(key) >= 4 and isinstance(key[2], str) and isinstance(key[3], str):
            self._last_seen_store = (key[2], key[3])
        elif len(key) >= 3 and isinstance(key[2], str) and key[2].isdigit():
            self._last_seen_store = (self._last_seen_store[0] if self._last_seen_store else "?", key[2])

    def _parse_listing(self, html: str, source_url: str) -> SearchPage:
        tree = ex.parse(html)
        sp = SearchPage(source_url=source_url)

        # 1. react-query cache: Coveo search / category-search result set
        state = ex.next_data(tree)
        found = None
        for k in LISTING_QUERY_KEYS:
            found = self._rq_data(state, k)
            if found:
                break
        if found:
            key, data = found
            self._note_store(key)
            body = (key[4].get("body") if len(key) > 4 and isinstance(key[4], dict) else None) or {}
            data = data or {}
            for r in data.get("results") or []:
                raw = r.get("raw") if isinstance(r, dict) else None
                if not raw:
                    continue
                ref = self._ref_from_coveo(raw)
                if ref:
                    sp.items.append(ref)
            sp.total = data.get("totalCount")
            sp.page_size = body.get("numberOfResults") or len(sp.items)
            first = body.get("firstResult") or 0
            sp.has_more = bool(sp.total is not None and first + len(sp.items) < sp.total)
            for f in data.get("facets") or []:
                vals = [str(v.get("value")) for v in (f.get("values") or []) if isinstance(v, dict) and v.get("value")]
                fid = f.get("facetId") or f.get("field")
                if fid and vals:
                    sp.facets[str(fid)] = vals[:20]
            corr = data.get("correctionData") or {}
            if corr.get("correctedQuery"):
                sp.facets["_corrected_query"] = [str(corr["correctedQuery"])]

        # 2. JSON-LD ItemList (not emitted today; cheap to keep)
        if not sp.items:
            il = ex.json_ld_of_type(tree, "ItemList")
            if il and il.get("itemListElement"):
                for el in il["itemListElement"]:
                    node = el.get("item", el)
                    url = node.get("url") or el.get("url")
                    if not url:
                        continue
                    pid = (self.parse_url(url) or {}).get("id") or node.get("sku") or url
                    sp.items.append(ItemRef(id=str(pid), name=node.get("name", ""), url=urljoin(self.base_url, url),
                                            brand=_brand(node), price=_price(node), currency=_currency(node),
                                            image=_image(node), rating=_rating(node), review_count=_reviews(node)))
                sp.total = il.get("numberOfItems")

        # 3. Selectors
        if not sp.items:
            for card in tree.css(self.SELECTORS["search_card"]):
                a = card.css_first(self.SELECTORS["card_link"])
                if not a:
                    continue
                url = urljoin(self.base_url, a.attributes.get("href") or "")
                pid = (self.parse_url(url) or {}).get("id")
                if not pid:
                    continue
                name_n = card.css_first(self.SELECTORS["card_name"])
                price_n = card.css_first(self.SELECTORS["card_price"])
                sp.items.append(ItemRef(id=pid, name=name_n.text(strip=True) if name_n else a.text(strip=True), url=url,
                                        price=ex.parse_price(price_n.text() if price_n else None), currency="AUD"))

        if not sp.items:
            if found:
                # Genuine zero-result page: the site answered with an (empty) result set.
                sp.page_size = 0
                return sp
            raise ExtractionError(
                "No products found on listing page and no Coveo result set in __NEXT_DATA__ - the page "
                "layout changed (or this wasn't a listing page). Run probe_page on the same URL."
            )
        if sp.page_size is None:
            sp.page_size = len(sp.items)
            sp.has_more = bool(sp.total and sp.total > sp.page * sp.page_size) or _has_next_link(tree)
        return sp

    def _ref_from_coveo(self, raw: dict[str, Any]) -> ItemRef | None:
        pid = raw.get("itemnumber") or raw.get("code")
        url = raw.get("productroutingurl")
        if not url and pid:
            url = f"/_p{pid}"
        if not (pid and url):
            return None
        price = raw.get("price")
        return ItemRef(
            id=str(pid),
            name=raw.get("name") or raw.get("title") or "",
            url=urljoin(self.base_url, url),
            brand=raw.get("brandname"),
            price=float(price) if isinstance(price, (int, float)) else ex.parse_price(str(price or "")),
            currency=raw.get("currency") or "AUD",
            availability=Availability.UNKNOWN if raw.get("isactive", True) else Availability.OUT_OF_STOCK,
            rating=float(raw["rating"]) if isinstance(raw.get("rating"), (int, float)) else None,
            review_count=int(raw["ratingcount"]) if isinstance(raw.get("ratingcount"), (int, float)) else None,
            image=raw.get("thumbnailimageurl") or raw.get("imageurl"),
        )

    # ------------------------------------------------------------------ product detail
    async def get_item(self, id_or_url: str) -> Item:
        url = self._item_url(id_or_url)

        async def produce():
            res = await self.fetcher.fetch(url, cookies=self._store_cookies())
            return self._parse_product(res.body, res.final_url)

        return await self.cached(("item", url, self.STORE_ID, self.REGION), self.policy.cache_ttl_item_s, produce)

    def _parse_product(self, html: str, url: str) -> Item:
        tree = ex.parse(html)
        pid = (self.parse_url(url) or {}).get("id")
        state = ex.next_data(tree)

        # 1. react-query cache
        rp = self._rq_data(state, "retail-product")
        if rp and isinstance(rp[1], dict) and rp[1].get("code"):
            key, prod = rp
            self._note_store(key)
            item = self._item_from_retail_product(prod, url, pid)
            price = self._rq_data(state, "product-retail-price")
            if price and isinstance(price[1], dict):
                p = price[1]
                if p.get("value") is not None:
                    item.price = float(p["value"])
                item.currency = p.get("currencyIso") or item.currency
                if p.get("priceType"):
                    item.extras["price_type"] = p["priceType"]
                if p.get("hasMultipleOffers"):
                    item.extras["has_multiple_offers"] = True
            ful = self._rq_data(state, "product-fulfilment")
            if ful and isinstance(ful[1], dict):
                self._apply_fulfilment(item, ful[1])
            aisle = self._rq_data(state, "product-aisle-item-location")
            if aisle and isinstance(aisle[1], list) and aisle[1]:
                locs = (aisle[1][0] or {}).get("inStoreLocations") or []
                if locs:
                    item.extras["aisle_locations"] = [{"aisle": l.get("aisle"), "bay": l.get("bay")} for l in locs[:6]]
            return item

        # 2. JSON-LD Product
        prod_ld = ex.json_ld_of_type(tree, "Product")
        if prod_ld:
            item = Item(
                id=str(prod_ld.get("sku") or pid or url), name=prod_ld.get("name", ""), url=url,
                brand=_brand(prod_ld), price=_price(prod_ld), currency=_currency(prod_ld), availability=_availability(prod_ld),
                rating=_rating(prod_ld), review_count=_reviews(prod_ld), image=_image(prod_ld),
                description=(prod_ld.get("description") or "")[:2000] or None,
            )
        else:
            # 3. Selectors / og meta
            name = ex.text(tree, self.SELECTORS["pdp_name"]) or ex.meta(tree, "og:title")
            if not (pid or name):
                raise ExtractionError("Product page had neither a retail-product record, JSON-LD Product nor a "
                                      "recognisable title. Run probe_page on it.")
            item = Item(id=pid or url, name=name or "", url=url, price=ex.parse_price(ex.text(tree, self.SELECTORS["pdp_price"])),
                        currency="AUD", image=ex.meta(tree, "og:image"), description=ex.meta(tree, "og:description"))

        for row in tree.css(self.SELECTORS["pdp_specs_rows"]):
            cells = [c.text(strip=True) for c in row.css("th, td")]
            if len(cells) == 2 and cells[0] and len(cells[0]) < 60:
                item.specs[cells[0]] = cells[1]
        bc = ex.json_ld_of_type(tree, "BreadcrumbList")
        if bc:
            item.category_path = [e.get("name") or (e.get("item") or {}).get("name", "") for e in bc.get("itemListElement", [])][1:]
        return item

    def _item_from_retail_product(self, prod: dict[str, Any], url: str, pid: str | None) -> Item:
        brand = prod.get("brand")
        images = prod.get("images") or []
        primary = next((i.get("url") for i in images if isinstance(i, dict) and i.get("imageType") == "PRIMARY" and i.get("url")), None)
        feature = prod.get("feature") or {}
        desc = (feature.get("description") if isinstance(feature, dict) else None) or prod.get("summary") or None
        item = Item(
            id=str(prod.get("code") or prod.get("itemNumber") or pid or url),
            name=prod.get("name") or "",
            url=url,
            brand=brand.get("name") if isinstance(brand, dict) else brand,
            currency="AUD",
            rating=float(prod["averageRating"]) if isinstance(prod.get("averageRating"), (int, float)) else None,
            review_count=int(prod["numberOfReviews"]) if isinstance(prod.get("numberOfReviews"), (int, float)) else None,
            image=primary or (images[0].get("url") if images and isinstance(images[0], dict) else None),
            description=(desc or "")[:2000] or None,
        )
        # Specs: classification features -> flat dict
        for cls in prod.get("classifications") or []:
            for f in (cls.get("features") or []) if isinstance(cls, dict) else []:
                name = f.get("name")
                vals = [str(v.get("value")) for v in (f.get("featureValues") or []) if isinstance(v, dict) and v.get("value") not in (None, "")]
                if name and vals and name not in item.specs:
                    item.specs[name] = ", ".join(vals)
        cats = sorted((c for c in prod.get("allCategories") or [] if isinstance(c, dict)), key=lambda c: c.get("level", 0))
        item.category_path = [c.get("displayName") for c in cats if c.get("displayName")]
        if cats and cats[-1].get("internalPath"):
            item.extras["category_url"] = f"{self.base_url}/products{cats[-1]['internalPath']}"
        if isinstance(feature, dict) and feature.get("keySellingPoints"):
            item.extras["key_selling_points"] = feature["keySellingPoints"]
        if isinstance(prod.get("warrantyReturns"), (str, dict)):
            wr = prod["warrantyReturns"]
            item.extras["warranty"] = wr if isinstance(wr, str) else wr.get("description") or wr
        docs = [{"type": d.get("docType"), "url": d.get("url")} for d in prod.get("guideDocument") or [] if isinstance(d, dict) and d.get("url")]
        if docs:
            item.extras["documents"] = docs[:6]
        for flag in ("isSpecialProduct", "isTradeOnly", "isMarketLink", "forHire", "isAgeRestricted", "bestSeller", "newArrival", "customMade"):
            if prod.get(flag):
                item.extras[flag] = True
        if prod.get("isSpecialProduct"):
            item.availability = Availability.SPECIAL_ORDER
        return item

    @staticmethod
    def _apply_fulfilment(item: Item, ful: dict[str, Any]) -> None:
        cc = ful.get("clickNCollectData") or {}
        ins = ful.get("inStorePickUpData") or {}
        dl = ful.get("deliveryData") or {}
        stock = ins.get("stock") if ins.get("stock") is not None else cc.get("stock")
        txt = str(ins.get("inStoreStockTxt") or cc.get("clickNCollectStockTxt") or "").lower()
        if ful.get("poa"):
            item.availability = Availability.SPECIAL_ORDER
        elif isinstance(stock, (int, float)):
            if stock <= 0:
                item.availability = Availability.OUT_OF_STOCK if not dl.get("isDeliveryAvailable") else Availability.ONLINE_ONLY
            elif stock <= 3 or "low" in txt:
                item.availability = Availability.LOW_STOCK
            else:
                item.availability = Availability.IN_STOCK
        elif "instock" in txt:
            item.availability = Availability.IN_STOCK
        elif "outofstock" in txt or "soldout" in txt:
            item.availability = Availability.OUT_OF_STOCK
        item.extras["store_stock"] = {
            "store": ins.get("storeName") or cc.get("storeName"),
            "stock": stock,
            "status": ins.get("inStoreStockTxt") or cc.get("clickNCollectStockTxt"),
            "click_and_collect": bool(cc.get("isClicknCollectAvailable")),
            "drive_and_collect": ful.get("driveNCollect") not in (None, "NOT_AVAILABLE"),
            "delivery": bool(dl.get("isDeliveryAvailable")),
            "in_store_pickup": bool(ins.get("inStorePickUpAvailable")),
        }
        if ful.get("isSpecialProduct"):
            item.extras["isSpecialProduct"] = True

    # ------------------------------------------------------------------ categories
    async def categories(self, parent: str | None = None) -> list[Category]:
        """The mega-menu on every page lists the full /products/ tree (~160 links, 2 levels).
        With `parent`, return only its direct children (paths one segment longer)."""
        url = parent if parent and parent.startswith("http") else urljoin(self.base_url, parent or "/products")
        res = await self.fetcher.fetch(url)
        tree = ex.parse(res.body)
        parent_path = None
        if parent:
            parent_path = "/" + parent.replace(self.base_url, "").strip("/").split("?")[0]
            if not parent_path.startswith("/products"):
                parent_path = "/products" + parent_path
        out: list[Category] = []
        seen: set[str] = set()
        for a in tree.css("a[href*='/products/'], a[href*='/our-range/']"):
            href = urljoin(self.base_url, a.attributes.get("href") or "").split("?")[0].split("#")[0].rstrip("/")
            if href in seen or _PRODUCT_URL_RE.search(href):
                continue
            path = href.replace(self.base_url, "")
            if parent_path and not (path.startswith(parent_path + "/") and path.count("/") == parent_path.count("/") + 1):
                continue
            seen.add(href)
            name = a.text(strip=True)
            if name:
                out.append(Category(name=name, slug=path, url=href))
        out.sort(key=lambda c: c.slug)
        return out


# ---------------------------------------------------------------------- json-ld helpers
def _offer(node: dict[str, Any]) -> dict[str, Any]:
    o = node.get("offers") or {}
    return o[0] if isinstance(o, list) and o else o if isinstance(o, dict) else {}


def _price(node: dict[str, Any]) -> float | None:
    o = _offer(node)
    return ex.parse_price(str(o.get("price") or o.get("lowPrice") or ""))


def _currency(node: dict[str, Any]) -> str | None:
    return _offer(node).get("priceCurrency") or "AUD"


def _availability(node: dict[str, Any]) -> Availability:
    a = str(_offer(node).get("availability", "")).lower()
    if "instock" in a:
        return Availability.IN_STOCK
    if "limited" in a:
        return Availability.LOW_STOCK
    if "outofstock" in a or "soldout" in a:
        return Availability.OUT_OF_STOCK
    if "preorder" in a or "backorder" in a:
        return Availability.SPECIAL_ORDER
    if "onlineonly" in a:
        return Availability.ONLINE_ONLY
    return Availability.UNKNOWN


def _brand(node: dict[str, Any]) -> str | None:
    b = node.get("brand")
    return b.get("name") if isinstance(b, dict) else b


def _image(node: dict[str, Any]) -> str | None:
    img = node.get("image")
    if isinstance(img, list):
        img = img[0] if img else None
    return img.get("url") if isinstance(img, dict) else img


def _rating(node: dict[str, Any]) -> float | None:
    r = (node.get("aggregateRating") or {}).get("ratingValue")
    return float(r) if r not in (None, "") else None


def _reviews(node: dict[str, Any]) -> int | None:
    r = (node.get("aggregateRating") or {}).get("reviewCount")
    return int(r) if r not in (None, "") else None


def _has_next_link(tree) -> bool:
    return bool(tree.css_first("a[rel='next'], link[rel='next'], a[aria-label*='Next']"))
