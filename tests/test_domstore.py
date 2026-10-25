"""DomStoreAdapter base + the sites built on it (umart, msy, mwave, littlebird, supercheap,
rebel, anaconda). Hermetic: the fetcher is stubbed with synthetic HTML / cards."""
import importlib

import pytest

from au_shopping_mcp.core.models import Availability, FetchResult, FetchTier

DOM_SITES = ["umart", "msy", "mwave", "littlebird", "supercheap", "rebel", "anaconda"]


def _adapter(mod, tmp_path, monkeypatch):
    from au_shopping_mcp.core import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    return importlib.import_module(f"au_shopping_mcp.adapters.{mod}").Adapter()


def _res(body="", json_data=None, url="https://x/"):
    return FetchResult(url=url, final_url=url, status=200, tier=FetchTier.HTTP, body=body, json_data=json_data)


@pytest.mark.parametrize("site", DOM_SITES)
def test_loads_and_config(site, tmp_path, monkeypatch):
    a = _adapter(site, tmp_path, monkeypatch)
    assert a.name == site and a.base_url.startswith("https://") and a.SEARCH_URL and a.PRODUCT_LINK_RE
    assert "{q}" in a.SEARCH_URL
    assert a.sort_options() == ["relevance"]


@pytest.mark.asyncio
async def test_search_ssr_html(tmp_path, monkeypatch):
    a = _adapter("supercheap", tmp_path, monkeypatch)
    html = """<html><body>
      <div class="card"><a href="/p/penrite-oil/34876" title="Penrite Oil 5W-30">img</a><span class="price">$90.99</span></div>
      <div class="card"><a href="/p/bendix-pads/55011" title="Bendix Brake Pads">x</a><span>$120.99</span></div>
      <a href="/c/car-care">Category</a>
    </body></html>"""
    async def fetch(url, **kw):
        return _res(body=html, url=url)
    monkeypatch.setattr(a.fetcher, "fetch", fetch)
    sp = await a.search("oil")
    assert [i.id for i in sp.items] == ["34876", "55011"]
    assert sp.items[0].name == "Penrite Oil 5W-30" and sp.items[0].price == 90.99


@pytest.mark.asyncio
async def test_search_browser_fallback(tmp_path, monkeypatch):
    a = _adapter("umart", tmp_path, monkeypatch)
    async def fetch(url, **kw):
        return _res(body="<html><body>no products here</body></html>", url=url)  # HTTP yields nothing
    async def fetch_dom(url, js, **kw):
        return _res(json_data=[{"href": "/product/asus-rtx-5070-99001", "title": "ASUS RTX 5070", "price": "1099", "image": "//cdn/x.jpg"}], url=url)
    monkeypatch.setattr(a.fetcher, "fetch", fetch)
    monkeypatch.setattr(a.fetcher, "fetch_dom", fetch_dom)
    sp = await a.search("rtx 5070")
    assert len(sp.items) == 1 and sp.items[0].id == "99001" and sp.items[0].price == 1099.0
    assert sp.items[0].image == "https://cdn/x.jpg"


@pytest.mark.asyncio
async def test_get_item_jsonld(tmp_path, monkeypatch):
    a = _adapter("rebel", tmp_path, monkeypatch)
    html = """<html><head><script type="application/ld+json">
      {"@type":"Product","name":"Nike Air Max 270","sku":"524401","brand":{"name":"Nike"},
       "offers":{"@type":"Offer","availability":"https://schema.org/InStock"}}
    </script></head><body><span class="price">$229.99</span></body></html>"""
    async def fetch(url, **kw):
        return _res(body=html, url=url)
    monkeypatch.setattr(a.fetcher, "fetch", fetch)
    it = await a.get_item("/p/nike-air-max-270-524401")
    assert it.name == "Nike Air Max 270" and it.brand == "Nike"
    assert it.availability == Availability.IN_STOCK and it.price == 229.99  # backfilled from HTML


@pytest.mark.asyncio
async def test_get_item_h1_fallback(tmp_path, monkeypatch):
    a = _adapter("anaconda", tmp_path, monkeypatch)
    html = '<html><body><h1>Gozney Tread Gas Pizza Oven</h1><div class="price">$899.99</div>Add to cart</body></html>'
    async def fetch(url, **kw):
        return _res(body=html, url=url)
    monkeypatch.setattr(a.fetcher, "fetch", fetch)
    it = await a.get_item("/en-au/p/gozney-tread")
    assert it.name == "Gozney Tread Gas Pizza Oven" and it.price == 899.99
    assert it.availability == Availability.IN_STOCK


def test_parse_url_and_search_url(tmp_path, monkeypatch):
    a = _adapter("mwave", tmp_path, monkeypatch)
    assert a.parse_url("https://www.mwave.com.au/products/asus-card-ac3468")["kind"] == "item"
    assert a.parse_url("https://www.mwave.com.au/laptops") is None
    assert a._search_url("rtx 5070", 1) == "https://www.mwave.com.au/searchresult?w=rtx+5070"


@pytest.mark.parametrize("site,sample", [
    ("bcf", "/p/xtm-lightweight-hardtop-rooftop-tent/691595.html"),
    ("qbd", "/the-whyte-python-world-tour/travis-kennedy/9781405974219/"),
    ("ple", "/products/679463/acer-nitro-monitor"),
    ("priceline", "/product/54950/blackmores-bio-c-1000"),
    ("petbarn", "/p/advance-high-protein-chicken-adult-dog-food"),
    ("chemistwarehouse", "/buy/84686/wagner-vitamin-c-1000"),
])
def test_product_link_regex_matches_real_url(site, sample, tmp_path, monkeypatch):
    """Guards the r'{!r}' template escaping bug that produced a literal \\d instead of \\d (digit)."""
    import re
    a = _adapter(site, tmp_path, monkeypatch)
    assert "\\\\" not in a.PRODUCT_LINK_RE, f"{site} regex double-escaped: {a.PRODUCT_LINK_RE!r}"
    assert re.search(a.PRODUCT_LINK_RE, sample), f"{site} regex {a.PRODUCT_LINK_RE!r} should match {sample}"
