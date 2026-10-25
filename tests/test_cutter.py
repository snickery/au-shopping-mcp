"""Cutter adapter, driven offline against real saved WooCommerce Store API responses (2026-09-21).

`search.json` keeps the `x-wp-*` paging headers alongside the body because that is where the
Store API puts the totals - a stub that drops them would hide a paging regression.
"""
import json
import os

import pytest

from au_shopping_mcp.core.models import Availability, ExtractionError, FetchResult, FetchTier

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "cutter")
SEARCH = json.load(open(f"{FIX}/search.json", encoding="utf-8"))
PRODUCT = json.load(open(f"{FIX}/product.json", encoding="utf-8"))
CATEGORIES = json.load(open(f"{FIX}/categories.json", encoding="utf-8"))


def _adapter(tmp_path, monkeypatch):
    from au_shopping_mcp.core import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    from au_shopping_mcp.adapters import cutter
    return cutter.Adapter()


def _stub(monkeypatch, adapter, router):
    """router(url) -> (json_data, headers)"""
    calls = []

    async def fake_fetch_json(url, **kw):
        calls.append(url)
        data, headers = router(url)
        return FetchResult(url=url, final_url=url, status=200, tier=FetchTier.API, body="",
                           headers=headers, json_data=data)

    monkeypatch.setattr(adapter.fetcher, "fetch_json", fake_fetch_json)
    return calls


def _default_router(url):
    if "products/categories" in url:
        return (CATEGORIES if "page=1" in url else []), {}
    if "/products/" in url:
        return PRODUCT, {}
    if "slug=" in url:
        return [PRODUCT], {}
    return SEARCH["body"], SEARCH["headers"]


# ----------------------------------------------------------------- search
@pytest.mark.asyncio
async def test_search_maps_store_api_rows(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    calls = _stub(monkeypatch, a, _default_router)
    sp = await a.search("led driver", sort="price_asc")

    assert "/wp-json/wc/store/v1/products?search=led+driver" in calls[0]
    assert "orderby=price&order=asc" in calls[0] and "per_page=24" in calls[0]
    assert sp.items and sp.total == int(SEARCH["headers"]["x-wp-total"])
    assert sp.has_more is (int(SEARCH["headers"]["x-wp-totalpages"]) > 1)
    i0 = sp.items[0]
    assert i0.id.isdigit() and i0.url.startswith("https://www.cutter.com.au/product/")
    assert i0.currency == "AUD"
    # minor units -> dollars, never the raw integer
    raw = SEARCH["body"][0]["prices"]
    assert i0.price == int(raw["price"]) / 10 ** raw["currency_minor_unit"]
    assert i0.price < 10_000


def test_price_scale_and_sort_validation(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    assert a._price({"price": "5295", "currency_minor_unit": 2}) == 52.95
    assert a._price({"price": "5295", "currency_minor_unit": 0}) == 5295
    assert a._price({"price": None}) is None
    assert a._sort_params(None) == ""
    assert a._sort_params("popularity") == "&orderby=popularity&order=desc"
    with pytest.raises(ValueError):
        a._sort_params("cheapest")   # would be a 400 from the Store API


def test_parse_url(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    assert a.parse_url("https://www.cutter.com.au/product/rcd-24-dc-dc-led-driver-wired/") == \
        {"kind": "item", "id": "rcd-24-dc-dc-led-driver-wired"}
    assert a.parse_url("https://www.cutter.com.au/product-category/led-drivers/luxdrive-led-drivers/") == \
        {"kind": "category", "id": "luxdrive-led-drivers"}
    assert a.parse_url("https://www.cutter.com.au/about/") is None


@pytest.mark.asyncio
async def test_non_list_response_is_an_extraction_error(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    _stub(monkeypatch, a, lambda url: ({"code": "rest_no_route"}, {}))
    with pytest.raises(ExtractionError, match="product list"):
        await a.search("anything")


# ----------------------------------------------------------------- product
@pytest.mark.asyncio
async def test_get_item_by_id_slug_and_url(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    calls = _stub(monkeypatch, a, _default_router)

    it = await a.get_item("1696")
    assert calls[-1].endswith("/products/1696")
    assert it.id == "1696" and it.name and it.price and it.currency == "AUD"
    assert it.availability == Availability.SPECIAL_ORDER   # fixture is on back-order
    assert it.brand == "Recom"                             # from the manufacturer attribute
    assert it.description and "<" not in it.description    # HTML stripped
    assert it.specs.get("Drive Current")
    assert "N/A" not in it.specs.values()                  # WooCommerce's unset dimensions/weight
    assert it.extras["variations"] == 5 and it.extras["stock_text"]
    assert it.category_path

    await a.get_item("https://www.cutter.com.au/product/rcd-24-dc-dc-led-driver-wired/")
    assert "slug=rcd-24-dc-dc-led-driver-wired" in calls[-1]

    with pytest.raises(ValueError):
        await a.get_item("https://www.cutter.com.au/about/")


@pytest.mark.asyncio
async def test_missing_product_reports_clearly(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    _stub(monkeypatch, a, lambda url: ([], {}))
    with pytest.raises(ExtractionError, match="No product"):
        await a.get_item("someone-elses-slug")


@pytest.mark.asyncio
async def test_sale_price_and_stock_mapping(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    row = {"id": 42, "name": "Discounted", "permalink": "https://www.cutter.com.au/product/x/",
           "prices": {"price": "1000", "regular_price": "2000", "currency_code": "AUD",
                      "currency_minor_unit": 2},
           "on_sale": True, "is_in_stock": True, "low_stock_remaining": 3, "average_rating": "4.5",
           "review_count": 7, "images": [{"src": "https://img/1.jpg"}], "sku": "ABC-1"}
    it = a._item(row)
    assert it.price == 10.0 and it.extras["regular_price"] == 20.0 and it.extras["discount_pct"] == 50
    assert it.availability == Availability.LOW_STOCK and it.extras["low_stock_remaining"] == 3
    assert it.rating == 4.5 and it.review_count == 7 and it.extras["sku"] == "ABC-1"
    assert a._item({**row, "is_in_stock": False}).availability == Availability.OUT_OF_STOCK
    assert a._item({**row, "is_on_backorder": True}).availability == Availability.SPECIAL_ORDER
    # a zero rating is "no ratings", not a 0-star product
    assert a._item({**row, "average_rating": "0"}).rating is None


# ----------------------------------------------------------------- categories
@pytest.mark.asyncio
async def test_categories_tree_and_browse_resolution(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    calls = _stub(monkeypatch, a, _default_router)

    tops = await a.categories()
    assert tops and all(c.url.startswith("https://www.cutter.com.au") for c in tops)
    assert tops == sorted(tops, key=lambda c: c.name)

    # the fixture is page 1 of 3, so pick a child whose parent term is on this page too
    ids = {t["id"] for t in CATEGORIES}
    child_of = next(t for t in CATEGORIES if t.get("parent") in ids)
    kids = await a.categories(parent=str(child_of["parent"]))
    assert any(k.slug == child_of["slug"] for k in kids)

    # browse accepts id, slug and a /product-category/ URL, and always sends the numeric term id
    await a.browse(str(child_of["id"]))
    assert f"category={child_of['id']}" in calls[-1]
    await a.browse(child_of["slug"])
    assert f"category={child_of['id']}" in calls[-1]
    await a.browse(f"https://www.cutter.com.au/product-category/{child_of['slug']}/")
    assert f"category={child_of['id']}" in calls[-1]

    with pytest.raises(ValueError, match="unknown Cutter category"):
        await a.browse("not-a-real-category")


@pytest.mark.asyncio
async def test_category_list_is_cached_not_refetched(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    calls = _stub(monkeypatch, a, _default_router)
    await a.categories()
    first = len([c for c in calls if "products/categories" in c])
    await a.categories()
    assert len([c for c in calls if "products/categories" in c]) == first
