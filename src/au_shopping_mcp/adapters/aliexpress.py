"""AliExpress (aliexpress.com) adapter.

Verified live 2026-09-21.

Listings are server-rendered: the whole result set sits in a JS object literal

    window._dida_config_._init_data_= { data: {...} }

(note `data:` is unquoted, so it is brace-matched with json.JSONDecoder().raw_decode,
not json.loads on a regex group - `ex.window_state` cannot read it).

    data.root.fields.mods.itemList.content[]  -> productId, title.displayTitle, image.imgUrl,
                                                 prices.{salePrice,originalPrice}, evaluation.starRating,
                                                 trade.tradeDesc ("50,000+ sold")
    data.root.fields.pageInfo                 -> page, pageSize, totalResults, searchResultType
    data.root.fields.mods.searchRefineFilters -> facets (Deals & discounts, Shipping from, Price, ...)

    search  /w/wholesale-<query>.html?page=<n>&SortType=<s>
    filters selectedSwitches=filterCode:freeshipping[,filterCode:choice_atm,filterCode:bigsale]
            shpf_co=<CN|AU|...>   pr=<min>-<max>

Product pages are client-rendered (window.runParams is empty, window._d_c_.DCData holds only
images, no JSON-LD), so get_item renders the page and reads the DOM. Anchors are AliExpress's
stable `data-pl` attributes (product-title / product-reviewer / product-specs) plus module class
PREFIXES (`price-default--current--<hash>`): the hash suffix rotates on every deploy, the prefix
does not - never match a full hashed class.

Currency, ship-to region and language come from the `aep_usuc_f` cookie - the AliExpress
equivalent of a store cookie. Without it you get whatever the exit IP geolocates to, so prices
silently change currency. Defaults AUD/AU/en_US, overridable with ALIEXPRESS_CURRENCY /
ALIEXPRESS_REGION / ALIEXPRESS_LOCALE, and it is passed through `fetcher.fetch(cookies=...)`
so the cookie jar owns it (a raw Cookie: header gets overwritten by the jar).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import quote, urljoin

from au_shopping_mcp.core.models import (
    Availability,
    ExtractionError,
    FetchPolicy,
    FetchTier,
    Item,
    ItemRef,
    SearchPage,
)
from au_shopping_mcp.adapters.base import SiteAdapter

_INIT_DATA = re.compile(r"_init_data_\s*=\s*\{\s*data\s*:\s*")
_ITEM_URL_RE = re.compile(r"/item/(\d{6,})\.html")

# Rendered in the product page and evaluated by fetch_dom. Kept in one place so a layout
# change is a one-string fix; every selector is a data-pl attribute or a class PREFIX.
_PRODUCT_JS = r"""async () => {
  const T = (e) => (e && (e.innerText || e.textContent) || '').trim();
  const one = (s) => document.querySelector(s);
  const all = (s) => [...document.querySelectorAll(s)];
  const money = (s) => { const m = (s || '').match(/([0-9][0-9,]*(?:\.[0-9]+)?)/); return m ? parseFloat(m[1].replace(/,/g, '')) : null; };

  const descRoot = one('#product-description') || one('[class*="description--wrap--"]');
  if (descRoot) { descRoot.scrollIntoView(); await new Promise(r => setTimeout(r, 1200)); }

  const title = T(one('[data-pl="product-title"]'));
  const cur = T(one('[class*="price-default--current--"]')) || T(one('[class*="price--current"]'));
  const orig = T(one('[class*="price-default--original--"]')) || T(one('[class*="price--original"]'));
  const rev = T(one('[data-pl="product-reviewer"]'));

  const specs = {};
  const specRoot = one('[data-pl="product-specs"]');
  if (specRoot) {
    for (const row of specRoot.querySelectorAll('li, div')) {
      const kids = [...row.children];
      if (kids.length !== 2) continue;
      const k = T(kids[0]), v = T(kids[1]);
      if (!k || !v || k.includes('\n') || v.includes('\n') || k.length > 60) continue;
      if (!(k in specs) && Object.keys(specs).length < 60) specs[k.replace(/:$/, '')] = v;
    }
  }

  const skus = all('[class*="sku-item--property--"]').map((e) => {
    const label = T(e.querySelector('[class*="sku-item--title--"]'));
    const opts = [...e.querySelectorAll('[class*="sku-item--image--"] img, [class*="sku-item--text--"]')]
      .map((o) => (o.tagName === 'IMG' ? (o.getAttribute('alt') || '') : T(o))).filter(Boolean);
    return { property: label.split(':')[0].trim(), selected: (label.split(':')[1] || '').trim() || null,
             options: [...new Set(opts)].slice(0, 40) };
  }).filter((s) => s.property);

  const storeA = one('a[href*="/store/"]') || one('a[href*="showcredential"]');
  const desc = T(descRoot);
  return {
    title,
    price_text: cur || null,
    original_text: orig || null,
    reviewer_text: rev || null,
    specs, skus,
    shipping: (T(one('.dynamic-shipping')) || T(one('[class*="shipping--content--"]')) || '').replace(/\s*\n\s*/g, ' | ') || null,
    store: storeA ? { name: T(storeA), url: storeA.getAttribute('href') } : null,
    images: [...new Set(all('[class*="slider--img--"] img, [class*="image-view--previewWrap--"] img')
      .map((i) => (i.getAttribute('src') || '').split('?')[0].replace(/_\d+x\d+[^/]*$/, '')).filter(Boolean))].slice(0, 10),
    description: desc || null,
    sold_out: /sold out|no longer available/i.test(document.body.innerText.slice(0, 4000)),
  };
}"""


class Adapter(SiteAdapter):
    name = "aliexpress"
    domain = "aliexpress.com"
    base_url = "https://www.aliexpress.com"
    description = (
        "AliExpress global marketplace: product search with orders/rating/discount signals, "
        "product detail (price, SKU options, specs, shipping estimate, store), prices in the "
        "configured currency."
    )
    # No browser_wait_selector: that would make the HTTP tier reject the (perfectly good)
    # server-rendered listing HTML, which never contains a product-detail selector.
    policy = FetchPolicy(start_tier=FetchTier.HTTP, min_delay_s=2.0, jitter_s=1.0, timeout_s=45)

    SORT_MAP = {
        "relevance": None,            # AliExpress "Best matches"
        "orders": "total_tranpro_desc",
        "price_asc": "price_asc",
        "price_desc": "price_desc",
    }
    # Switch filters are AND-ed in one comma-separated param.
    SWITCHES = {"free_shipping": "filterCode:freeshipping", "choice": "filterCode:choice_atm",
                "on_sale": "filterCode:bigsale"}

    def __init__(self) -> None:
        super().__init__()
        self.currency = os.environ.get("ALIEXPRESS_CURRENCY", "AUD").upper()
        self.region = os.environ.get("ALIEXPRESS_REGION", "AU").upper()
        self.locale = os.environ.get("ALIEXPRESS_LOCALE", "en_US")
        self._effective_currency: str | None = None   # what the site actually priced in, last call

    @property
    def _cookies(self) -> dict[str, str]:
        return {"aep_usuc_f": f"site=glo&c_tp={self.currency}&region={self.region}&b_locale={self.locale}"}

    # ------------------------------------------------------------------ urls
    def parse_url(self, url: str) -> dict[str, str] | None:
        m = _ITEM_URL_RE.search(url)
        if m:
            return {"kind": "item", "id": m.group(1)}
        if "/w/wholesale-" in url:
            return {"kind": "search", "id": url}
        if "/category/" in url:
            return {"kind": "category", "id": url}
        return None

    def sort_options(self) -> list[str]:
        return list(self.SORT_MAP)

    def _sort_param(self, sort: str | None) -> str:
        if sort and sort not in self.SORT_MAP:
            raise ValueError(f"unknown sort {sort!r}; one of {', '.join(self.SORT_MAP)}")
        s = self.SORT_MAP.get(sort or "relevance")
        return f"&SortType={s}" if s else ""

    def _filter_params(self, filters: dict[str, str] | None) -> str:
        """free_shipping/choice/on_sale -> selectedSwitches; ship_from -> shpf_co; price -> pr."""
        if not filters:
            return ""
        out, switches = [], []
        for key, value in filters.items():
            if key in self.SWITCHES:
                if str(value).lower() not in ("0", "false", "no", ""):
                    switches.append(self.SWITCHES[key])
            elif key in ("ship_from", "shpf_co"):
                out.append(f"&shpf_co={quote(str(value).upper())}")
            elif key in ("price", "pr"):
                out.append(f"&pr={quote(str(value))}")
            else:
                raise ValueError(
                    f"unknown filter {key!r}; one of {', '.join([*self.SWITCHES, 'ship_from', 'price'])}"
                )
        if switches:
            out.append("&selectedSwitches=" + quote(",".join(switches), safe=",:"))
        return "".join(out)

    def _listing_url(self, query: str, page: int, sort: str | None, filters: dict[str, str] | None) -> str:
        slug = quote(re.sub(r"\s+", "-", query.strip()), safe="")
        return (f"{self.base_url}/w/wholesale-{slug}.html?page={page}"
                f"{self._sort_param(sort)}{self._filter_params(filters)}")

    # ------------------------------------------------------------------ search / browse
    async def search(self, query: str, *, page: int = 1, sort: str | None = None,
                     filters: dict[str, str] | None = None) -> SearchPage:
        url = self._listing_url(query, page, sort, filters)

        async def produce():
            res = await self.fetcher.fetch(url, cookies=self._cookies)
            sp = self._listing(res.body, res.final_url)
            sp.query, sp.page = query, page
            return sp

        return await self.cached(("search", url), self.policy.cache_ttl_search_s, produce)

    async def browse(self, category: str, *, page: int = 1, sort: str | None = None) -> SearchPage:
        """`category` is an AliExpress listing URL or path (a /w/wholesale-*.html link with the
        filters you picked on the site, or a legacy /category/<id>/<slug>.html)."""
        if not self.parse_url(category if category.startswith("http") else urljoin(self.base_url, category)):
            raise ValueError(
                "AliExpress has no browsable category tree - pass a listing URL copied from the site "
                "(/w/wholesale-<term>.html... or /category/<id>/<slug>.html), or use search_products."
            )
        path = category if category.startswith("http") else urljoin(self.base_url, category)
        sep = "&" if "?" in path else "?"
        url = f"{path}{sep}page={page}{self._sort_param(sort)}"

        async def produce():
            res = await self.fetcher.fetch(url, cookies=self._cookies)
            sp = self._listing(res.body, res.final_url)
            sp.category, sp.page = category, page
            return sp

        return await self.cached(("browse", url), self.policy.cache_ttl_search_s, produce)

    async def categories(self, parent: str | None = None) -> list:
        raise NotImplementedError(
            "AliExpress does not expose a stable category tree (the legacy /category/<id> pages mostly "
            f"return zero results). Use {self.name}_search_products, or paste a listing URL from the "
            f"site into {self.name}_browse_category."
        )

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def init_data(html: str) -> dict[str, Any] | None:
        """`window._dida_config_._init_data_= { data: {...} }` -> the parsed literal.

        Shape is {"hierarchy", "data", "global"}; the page's own fields live under
        ["data"]["root"]["fields"] (the outer `data:` key is the JS wrapper, not that one)."""
        m = _INIT_DATA.search(html)
        if not m:
            return None
        start = html.find("{", m.end() - 1)
        if start < 0:
            return None
        try:
            return json.JSONDecoder().raw_decode(html[start:])[0]
        except json.JSONDecodeError:
            return None

    def _listing(self, html: str, source_url: str) -> SearchPage:
        data = self.init_data(html)
        fields = ((((data or {}).get("data") or {}).get("root") or {}).get("fields")) or {}
        if not fields:
            raise ExtractionError(
                "No _init_data_ hydration blob on the AliExpress listing page - a challenge page, "
                "or the page shape changed. Run probe_page on the same URL."
            )
        info = fields.get("pageInfo") or {}
        content = ((fields.get("mods") or {}).get("itemList") or {}).get("content") or []
        sp = SearchPage(source_url=source_url, total=info.get("totalResults"),
                        page_size=info.get("pageSize") or len(content))
        for raw in content:
            ref = self._ref(raw)
            if ref:
                sp.items.append(ref)
        if not sp.items:
            if info.get("searchResultType") == "zero_result":
                raise ExtractionError(
                    "AliExpress returned zero results for this listing (a dead legacy category id, or "
                    "filters that exclude everything). Try search_products, or drop the filters."
                )
            raise ExtractionError(
                "No items in the AliExpress hydration blob - the mods.itemList shape changed. "
                "Run probe_page on the same URL."
            )
        page = info.get("page") or 1
        total = info.get("totalResults")
        sp.has_more = bool(total and page * (info.get("pageSize") or len(sp.items)) < total)
        sp.facets = self._facets(fields)
        return sp

    @staticmethod
    def _facets(fields: dict[str, Any]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for f in ((fields.get("mods") or {}).get("searchRefineFilters") or {}).get("content") or []:
            title = (f.get("title") or "").strip()
            values = [str(c.get("text") or c.get("priceRanges") or "").strip()
                      for c in (f.get("content") or []) if isinstance(c, dict)]
            values = [v for v in values if v]
            if title and values:
                out[title] = values[:20]
        return out

    def _ref(self, raw: dict[str, Any]) -> ItemRef | None:
        pid = raw.get("productId") or raw.get("redirectedId")
        if not pid:
            return None
        prices = raw.get("prices") or {}
        sale = prices.get("salePrice") or {}
        original = prices.get("originalPrice") or {}
        if sale.get("currencyCode"):
            self._effective_currency = sale["currencyCode"]
        ref = ItemRef(
            id=str(pid),
            name=((raw.get("title") or {}).get("displayTitle") or "").strip(),
            url=f"{self.base_url}/item/{pid}.html",
            price=self._num(sale.get("minPrice")),
            currency=sale.get("currencyCode") or self.currency,
            rating=self._num((raw.get("evaluation") or {}).get("starRating")),
            image=self._abs((raw.get("image") or {}).get("imgUrl")),
        )
        was = self._num(original.get("minPrice"))
        if was and ref.price and was > ref.price:
            ref.extras["original_price"] = was
            ref.extras["discount_pct"] = round((was - ref.price) / was * 100)
        sold = (raw.get("trade") or {}).get("tradeDesc")
        if sold:
            ref.extras["sold"] = sold
        if any((sp.get("source") or "").startswith("choice") for sp in raw.get("sellingPoints") or []):
            ref.extras["choice"] = True
        return ref

    # ------------------------------------------------------------------ product
    async def get_item(self, id_or_url: str) -> Item:
        parsed = self.parse_url(id_or_url) if id_or_url.startswith("http") else None
        pid = (parsed or {}).get("id") if parsed else id_or_url.strip()
        if not pid or not pid.isdigit():
            raise ValueError(f"{id_or_url!r} is not an AliExpress item id or /item/<id>.html URL")
        url = f"{self.base_url}/item/{pid}.html"

        async def produce():
            res = await self.fetcher.fetch_dom(
                url, _PRODUCT_JS, wait_selector='[data-pl="product-title"]', cookies=self._cookies,
            )
            return self._item(pid, url, res.json_data or {})

        return await self.cached(("item", url, self.currency), self.policy.cache_ttl_item_s, produce)

    def _item(self, pid: str, url: str, dom: dict[str, Any]) -> Item:
        title = (dom.get("title") or "").strip()
        price = self._money(dom.get("price_text"))
        if not title or price is None:
            raise ExtractionError(
                f"AliExpress product page for {pid} rendered without a title/price - the item may be "
                f"delisted, or the detail layout changed. Run probe_page on {url}."
            )
        rev = dom.get("reviewer_text") or ""
        rating = re.match(r"\s*([0-9.]+)", rev)
        reviews = re.search(r"([0-9,]+)\s*Review", rev, re.I)
        item = Item(
            id=pid, name=title, url=url, price=price,
            currency=self._currency_of(dom.get("price_text")) or self._effective_currency or self.currency,
            availability=Availability.OUT_OF_STOCK if dom.get("sold_out") else Availability.IN_STOCK,
            rating=float(rating.group(1)) if rating else None,
            review_count=int(reviews.group(1).replace(",", "")) if reviews else None,
            image=self._abs((dom.get("images") or [None])[0]),
            specs={str(k): str(v) for k, v in (dom.get("specs") or {}).items()},
        )
        desc = (dom.get("description") or "").strip()
        # The description panel renders a "Description / report / View more" chrome shell before
        # the seller's own copy loads; anything that short is that shell, not a description.
        if len(desc) > 60:
            item.description = desc[:2000]
        was = self._money(dom.get("original_text"))
        if was and was > price:
            item.extras["original_price"] = was
            item.extras["discount_pct"] = round((was - price) / was * 100)
        sold = re.search(r"([0-9,+]+\s*(?:sold|orders))", rev, re.I)
        if sold:
            item.extras["sold"] = sold.group(1)
        if dom.get("shipping"):
            item.extras["shipping"] = dom["shipping"]
        if dom.get("skus"):
            item.extras["sku_options"] = dom["skus"]
        store = dom.get("store") or {}
        if store.get("name"):
            # "Sold By\n<store>\n(Trader)" -> "<store>"
            lines = [ln.strip() for ln in str(store["name"]).splitlines() if ln.strip()]
            named = [ln for ln in lines if not re.fullmatch(r"(Sold By|\(.*\))", ln, re.I)]
            item.extras["store"] = {"name": (named or lines)[0], "url": self._abs(store.get("url"))}
        if len(dom.get("images") or []) > 1:
            item.extras["images"] = [self._abs(i) for i in dom["images"]]
        return item

    # ------------------------------------------------------------------ status / helpers
    def site_specific_status(self) -> dict[str, Any]:
        return {"locale": {"currency": self.currency, "region": self.region, "language": self.locale,
                           "cookie": "aep_usuc_f", "effective_currency": self._effective_currency}}

    @staticmethod
    def _num(v: Any) -> float | None:
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _money(s: Any) -> float | None:
        m = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)", str(s or ""))
        return float(m.group(1).replace(",", "")) if m else None

    @staticmethod
    def _currency_of(price_text: Any) -> str | None:
        """'AU$8.19' -> AUD, 'US $1.79' -> USD, '€3.99' -> EUR."""
        sym = re.sub(r"[\d.,\s]", "", str(price_text or ""))
        return {"AU$": "AUD", "US$": "USD", "$": "USD", "£": "GBP", "€": "EUR", "NZ$": "NZD",
                "C$": "CAD", "CA$": "CAD"}.get(sym)

    def _abs(self, u: Any) -> str | None:
        if not isinstance(u, str) or not u:
            return None
        if u.startswith("//"):
            return "https:" + u
        return urljoin(self.base_url, u) if u.startswith("/") else u
