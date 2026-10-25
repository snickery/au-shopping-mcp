"""AliExpress adapter, driven offline against real saved captures (2026-09-21).

`search.html` is a real /w/wholesale-esp32.html response (AUD, ship-to AU); `product_dom.json`
is the payload the in-page extractor returned for item 1005006860885924, so the DOM-shape
assumptions and the Python post-processing are both pinned. A site redesign shows up as a red
test here rather than as a confused agent.
"""
import json
import os

import pytest

from au_shopping_mcp.core.models import Availability, ExtractionError, FetchResult, FetchTier

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "aliexpress")


def _adapter(tmp_path, monkeypatch, **env):
    from au_shopping_mcp.core import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from au_shopping_mcp.adapters import aliexpress
    return aliexpress.Adapter()


def _stub_fetch(monkeypatch, adapter, body):
    seen = {}

    async def fake_fetch(url, **kw):
        seen["url"], seen["cookies"] = url, kw.get("cookies")
        return FetchResult(url=url, final_url=url, status=200, tier=FetchTier.HTTP, body=body)

    monkeypatch.setattr(adapter.fetcher, "fetch", fake_fetch)
    return seen


def _stub_dom(monkeypatch, adapter, payload):
    seen = {}

    async def fake_dom(url, js, **kw):
        seen["url"], seen["cookies"], seen["wait"] = url, kw.get("cookies"), kw.get("wait_selector")
        return FetchResult(url=url, final_url=url, status=200, tier=FetchTier.BROWSER, body="",
                           json_data=payload)

    monkeypatch.setattr(adapter.fetcher, "fetch_dom", fake_dom)
    return seen


# ----------------------------------------------------------------- listing parse
@pytest.mark.asyncio
async def test_search_parses_hydration_blob(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    seen = _stub_fetch(monkeypatch, a, open(f"{FIX}/search.html", encoding="utf-8").read())
    sp = await a.search("esp32")

    assert len(sp.items) == 60 and sp.total and sp.total > 1000
    assert sp.has_more is True and sp.page == 1
    i0 = sp.items[0]
    assert i0.id.isdigit() and i0.url == f"https://www.aliexpress.com/item/{i0.id}.html"
    assert i0.name and i0.price and i0.currency == "AUD"
    assert i0.rating and 0 < i0.rating <= 5
    assert i0.image.startswith("https://")
    # listing-only signals ride in extras, not new top-level model fields
    assert i0.extras["sold"].endswith("sold")
    assert i0.extras["original_price"] > i0.price and 0 < i0.extras["discount_pct"] < 100
    assert "Shipping from" in " ".join(sp.facets)


@pytest.mark.asyncio
async def test_search_url_carries_sort_filters_and_currency_cookie(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, ALIEXPRESS_CURRENCY="usd", ALIEXPRESS_REGION="us")
    seen = _stub_fetch(monkeypatch, a, open(f"{FIX}/search.html", encoding="utf-8").read())
    await a.search("usb c cable", page=3, sort="orders",
                   filters={"free_shipping": True, "ship_from": "cn", "price": "5-20"})

    assert "/w/wholesale-usb-c-cable.html" in seen["url"]
    assert "page=3" in seen["url"] and "SortType=total_tranpro_desc" in seen["url"]
    assert "selectedSwitches=filterCode:freeshipping" in seen["url"]
    assert "shpf_co=CN" in seen["url"] and "pr=5-20" in seen["url"]
    # currency/region must travel as the aep_usuc_f cookie through the jar, never a header
    assert seen["cookies"]["aep_usuc_f"] == "site=glo&c_tp=USD&region=US&b_locale=en_US"


def test_sort_and_filter_validation(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    assert a.sort_options() == ["relevance", "orders", "price_asc", "price_desc"]
    assert a._sort_param("relevance") == ""
    with pytest.raises(ValueError):
        a._sort_param("cheapest")
    with pytest.raises(ValueError):
        a._filter_params({"colour": "red"})
    assert a._filter_params({"free_shipping": False}) == ""


def test_parse_url(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    assert a.parse_url("https://www.aliexpress.com/item/1005006860885924.html") == \
        {"kind": "item", "id": "1005006860885924"}
    assert a.parse_url("https://www.aliexpress.com/w/wholesale-esp32.html")["kind"] == "search"
    assert a.parse_url("https://www.aliexpress.com/store/1103164014") is None


@pytest.mark.asyncio
async def test_zero_result_listing_says_so(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    html = open(f"{FIX}/search.html", encoding="utf-8").read()
    blob = a.init_data(html)
    blob["data"]["root"]["fields"]["mods"]["itemList"]["content"] = []
    blob["data"]["root"]["fields"]["pageInfo"]["searchResultType"] = "zero_result"
    _stub_fetch(monkeypatch, a, "window._dida_config_._init_data_= { data: " + json.dumps(blob) + "};")
    with pytest.raises(ExtractionError, match="zero results"):
        await a.search("nothing at all")


@pytest.mark.asyncio
async def test_challenge_page_without_blob_raises(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    _stub_fetch(monkeypatch, a, "<html><body>slide to verify</body></html>")
    with pytest.raises(ExtractionError, match="hydration blob"):
        await a.search("esp32")


@pytest.mark.asyncio
async def test_browse_requires_a_listing_url(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    _stub_fetch(monkeypatch, a, open(f"{FIX}/search.html", encoding="utf-8").read())
    with pytest.raises(ValueError, match="listing URL"):
        await a.browse("power tools")
    sp = await a.browse("/w/wholesale-esp32.html?shpf_co=AU", page=2)
    assert sp.category and sp.items


@pytest.mark.asyncio
async def test_categories_unsupported(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    with pytest.raises(NotImplementedError, match="category tree"):
        await a.categories()


# ----------------------------------------------------------------- product parse
@pytest.mark.asyncio
async def test_get_item_from_dom_payload(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    dom = json.load(open(f"{FIX}/product_dom.json", encoding="utf-8"))
    seen = _stub_dom(monkeypatch, a, dom)
    it = await a.get_item("1005006860885924")

    assert seen["url"] == "https://www.aliexpress.com/item/1005006860885924.html"
    assert seen["wait"] == '[data-pl="product-title"]'
    assert seen["cookies"]["aep_usuc_f"].startswith("site=glo&c_tp=AUD")
    assert it.id == "1005006860885924" and it.name.startswith("ESP32-S3")
    assert it.price == 8.19 and it.currency == "AUD"
    assert it.availability == Availability.IN_STOCK
    assert it.rating == 4.9 and it.review_count == 2653
    assert it.specs.get("Package") == "DIP"
    assert it.extras["original_price"] == 16.38 and it.extras["discount_pct"] == 50
    assert it.extras["sold"].endswith("sold")
    assert "Free shipping" in it.extras["shipping"]
    assert it.extras["sku_options"][0]["property"] == "Color"
    assert it.extras["store"]["name"] == "Top Tech Store"
    assert it.image.startswith("https://ae-pic")


@pytest.mark.asyncio
async def test_get_item_accepts_url_and_rejects_junk(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    _stub_dom(monkeypatch, a, json.load(open(f"{FIX}/product_dom.json", encoding="utf-8")))
    it = await a.get_item("https://www.aliexpress.com/item/1005006860885924.html?spm=x")
    assert it.id == "1005006860885924"
    with pytest.raises(ValueError):
        await a.get_item("https://www.aliexpress.com/store/1103164014")


@pytest.mark.asyncio
async def test_unrendered_product_page_raises(tmp_path, monkeypatch):
    """A CSR page that never hydrated has no title/price - say so instead of inventing an Item."""
    a = _adapter(tmp_path, monkeypatch)
    _stub_dom(monkeypatch, a, {"title": "", "price_text": None, "specs": {}, "skus": []})
    with pytest.raises(ExtractionError, match="delisted"):
        await a.get_item("1005006860885924")


@pytest.mark.asyncio
async def test_sold_out_and_store_name_cleanup(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    _stub_dom(monkeypatch, a, {
        "title": "Thing", "price_text": "US $3.20", "original_text": None,
        "reviewer_text": "4.5 12 Reviews ౹ 100+ sold", "specs": {}, "skus": [],
        "store": {"name": "Sold By\nYANXD Store\n(Trader)", "url": "//www.aliexpress.com/store/1103164014"},
        "images": [], "description": "short", "sold_out": True,
    })
    it = await a.get_item("1005006860885924")
    assert it.availability == Availability.OUT_OF_STOCK
    assert it.currency == "USD" and it.price == 3.20
    assert it.extras["store"] == {"name": "YANXD Store", "url": "https://www.aliexpress.com/store/1103164014"}
    assert it.extras["sold"] == "100+ sold"
    assert it.description is None  # the "Description / report / View more" shell is not a description


def test_site_status_reports_locale(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, ALIEXPRESS_CURRENCY="nzd", ALIEXPRESS_REGION="nz")
    loc = a.site_specific_status()["locale"]
    assert loc["currency"] == "NZD" and loc["region"] == "NZ" and loc["cookie"] == "aep_usuc_f"
    assert loc["effective_currency"] is None
