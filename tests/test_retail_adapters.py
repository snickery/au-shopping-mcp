"""Costco / Officeworks / Myer / Altronics adapters, driven offline against real saved
fixtures (2026-09-06) by stubbing the fetcher. Verifies the full search()/get_item() parse
path, not just helpers."""
import json
import os

import pytest

from au_shopping_mcp.core.models import Availability, FetchResult, FetchTier

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _result(body="", json_data=None, url="https://x/"):
    return FetchResult(url=url, final_url=url, status=200, tier=FetchTier.HTTP, body=body, json_data=json_data)


def _adapter(mod, tmp_path, monkeypatch):
    from au_shopping_mcp.core import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    import importlib
    m = importlib.import_module(f"au_shopping_mcp.adapters.{mod}")
    return m.Adapter()


def _stub_fetch(monkeypatch, adapter, *, body="", json_data=None):
    async def fake_fetch(url, **kw):
        return _result(body=body, json_data=json_data, url=url)
    monkeypatch.setattr(adapter.fetcher, "fetch", fake_fetch)
    monkeypatch.setattr(adapter.fetcher, "fetch_json", fake_fetch)


# ----------------------------------------------------------------- Costco
@pytest.mark.asyncio
async def test_costco_search_and_product(tmp_path, monkeypatch):
    a = _adapter("costco", tmp_path, monkeypatch)
    search = json.load(open(f"{FIX}/costco/search.json"))
    _stub_fetch(monkeypatch, a, json_data=search)
    sp = await a.search("tv", sort="price_asc")
    assert sp.items and all(i.id and i.url.startswith("https://www.costco.com.au") for i in sp.items)
    assert sp.has_more is True
    i0 = sp.items[0]
    assert i0.price and i0.currency == "AUD"

    prod = json.load(open(f"{FIX}/costco/product.json"))
    _stub_fetch(monkeypatch, a, json_data=prod)
    it = await a.get_item("100087")
    assert it.id == "100087" and it.name and it.price == 169.99
    assert it.availability == Availability.IN_STOCK
    assert it.specs and it.extras.get("stock_level")


def test_costco_sort_validation(tmp_path, monkeypatch):
    a = _adapter("costco", tmp_path, monkeypatch)
    assert a._sort("price_asc") == "price-asc"
    with pytest.raises(ValueError):
        a._sort("cheapest")
    assert a.parse_url("https://www.costco.com.au/Electronics/x/p/100087") == {"kind": "item", "id": "100087"}


# ----------------------------------------------------------------- Officeworks
@pytest.mark.asyncio
async def test_officeworks_search_and_product(tmp_path, monkeypatch):
    a = _adapter("officeworks", tmp_path, monkeypatch)
    algolia = json.load(open(f"{FIX}/officeworks/search_algolia.json"))
    _stub_fetch(monkeypatch, a, json_data=algolia)
    sp = await a.search("stapler")
    assert sp.total == 137 and sp.items
    i0 = sp.items[0]
    assert i0.price == 4.0 and i0.url.startswith("https://www.officeworks.com.au/shop/officeworks/p/")

    html = open(f"{FIX}/officeworks/product.html", encoding="utf-8").read()
    _stub_fetch(monkeypatch, a, body=html)
    it = await a.get_item("keji-half-strip-stapler-black-kehsstapbk")
    assert it.id == "KEHSSTAPBK" and it.price == 4.0 and it.brand == "Keji"
    assert 0 < len(it.specs) <= 40


def test_officeworks_helpers(tmp_path, monkeypatch):
    a = _adapter("officeworks", tmp_path, monkeypatch)
    assert a._cents(400) == 4.0
    assert a._rating(39) == 3.9 and a._rating(4.5) == 4.5
    assert a._img("//s3/x.jpg") == "https://s3/x.jpg"
    assert a._index("price_asc") == "prod-product-wc-price-asc"
    with pytest.raises(ValueError):
        a._index("cheapest")


# ----------------------------------------------------------------- Myer
@pytest.mark.asyncio
async def test_myer_search_and_product(tmp_path, monkeypatch):
    a = _adapter("myer", tmp_path, monkeypatch)
    html = open(f"{FIX}/myer/search.html", encoding="utf-8").read()
    _stub_fetch(monkeypatch, a, body=html)
    sp = await a.search("jeans")
    assert sp.total and sp.items and sp.has_more
    assert sp.items[0].url.startswith("https://www.myer.com.au/p/") and sp.items[0].price

    phtml = open(f"{FIX}/myer/product.html", encoding="utf-8").read()
    _stub_fetch(monkeypatch, a, body=phtml)
    it = await a.get_item("rodd---gunn-motion-melange-straight-fit-jean-regular-leg-in-denim")
    assert it.id and it.name and it.price == 99.0 and it.brand == "Rodd & Gunn"
    assert it.extras.get("was_price") == 159.0 and it.availability == Availability.IN_STOCK


def test_myer_sort_and_url(tmp_path, monkeypatch):
    a = _adapter("myer", tmp_path, monkeypatch)
    assert a._sort_param("price_asc") == "&sortBy=priceAscending"
    assert a._sort_param(None) == ""
    with pytest.raises(ValueError):
        a._sort_param("cheapest")
    assert a.parse_url("https://www.myer.com.au/p/some-thing-123")["kind"] == "item"


# ----------------------------------------------------------------- Altronics
@pytest.mark.asyncio
async def test_altronics_search_and_product(tmp_path, monkeypatch):
    a = _adapter("altronics", tmp_path, monkeypatch)
    html = open(f"{FIX}/altronics/search.html", encoding="utf-8").read()
    _stub_fetch(monkeypatch, a, body=html)
    sp = await a.search("arduino")
    assert len(sp.items) >= 5 and all(i.url.startswith("https://www.altronics.com.au/product/") for i in sp.items)
    assert any(i.price for i in sp.items)

    phtml = open(f"{FIX}/altronics/product.html", encoding="utf-8").read()
    _stub_fetch(monkeypatch, a, body=phtml)
    it = await a.get_item("/product/z6320-temperature-humidity-sensor-breakout-for-arduino")
    assert it.id == "Z6320" and it.price == 21.95 and it.availability == Availability.IN_STOCK


def test_altronics_sort_and_url(tmp_path, monkeypatch):
    a = _adapter("altronics", tmp_path, monkeypatch)
    assert a._orderby("price_asc") == "&orderby=10"
    assert a._orderby(None) == ""
    with pytest.raises(ValueError):
        a._orderby("cheapest")
    assert a.parse_url("https://www.altronics.com.au/product/z6320-foo")["kind"] == "item"


# ----------------------------------------------------------------- all adapters load + prefix
@pytest.mark.parametrize("site", ["costco", "officeworks", "myer", "altronics"])
def test_adapter_loads(site, tmp_path, monkeypatch):
    a = _adapter(site, tmp_path, monkeypatch)
    assert a.name == site and a.sort_options() and a.domain.endswith(".com.au")


# ----------------------------------------------------------------- staticICE (aggregator)
@pytest.mark.asyncio
async def test_staticice_search(tmp_path, monkeypatch):
    a = _adapter("staticice", tmp_path, monkeypatch)
    html = open(f"{FIX}/staticice/search.html", encoding="utf-8").read()
    _stub_fetch(monkeypatch, a, body=html)
    sp = await a.search("rtx 5070")
    assert sp.items and all(i.price and i.brand for i in sp.items)
    assert any("computeralliance" in (i.url or "") for i in sp.items)
    assert sp.has_more is False


@pytest.mark.asyncio
async def test_staticice_get_item_explains(tmp_path, monkeypatch):
    from au_shopping_mcp.core.models import ExtractionError
    a = _adapter("staticice", tmp_path, monkeypatch)
    with pytest.raises(ExtractionError):
        await a.get_item("anything")


# ----------------------------------------------------------------- Scorptec (browser tier)
@pytest.mark.asyncio
async def test_scorptec_search_and_product(tmp_path, monkeypatch):
    a = _adapter("scorptec", tmp_path, monkeypatch)
    html = open(f"{FIX}/scorptec/search.html", encoding="utf-8").read()
    _stub_fetch(monkeypatch, a, body=html)
    sp = await a.search("rtx 5070")
    assert sp.items and all("/product/" in i.url for i in sp.items)
    assert len({i.url for i in sp.items}) == len(sp.items)  # de-duped

    phtml = open(f"{FIX}/scorptec/product.html", encoding="utf-8").read()
    _stub_fetch(monkeypatch, a, body=phtml)
    it = await a.get_item("/product/graphics-cards/nvidia/120778-palit-rtx-5070")
    assert it.name  # category_path is best-effort on Scorptec model-group pages


def test_scorptec_starts_at_browser_tier(tmp_path, monkeypatch):
    a = _adapter("scorptec", tmp_path, monkeypatch)
    assert a.policy.start_tier == FetchTier.BROWSER
    assert a.parse_url("https://www.scorptec.com.au/product/graphics-cards/nvidia/120778-x")["id"] == "120778"


# ----------------------------------------------------------------- JB Hi-Fi (browser tier)
@pytest.mark.asyncio
async def test_jbhifi_search_maps_cards(tmp_path, monkeypatch):
    a = _adapter("jbhifi", tmp_path, monkeypatch)
    cards = [
        {"handle": "/products/sony-wh-ch520-black", "title": "Sony WH-CH520 (Black)", "price": "59", "was": "79", "image": "https://cdn/x.jpg"},
        {"handle": "/products/bose-qc-black?variant=1", "title": "Bose QuietComfort", "price": "449", "was": None, "image": None},
        {"handle": "", "title": "junk", "price": None},  # dropped
    ]
    async def fake_dom(url, js, **kw):
        return _result(json_data=cards, url=url)
    monkeypatch.setattr(a.fetcher, "fetch_dom", fake_dom)
    sp = await a.search("headphones")
    assert [i.id for i in sp.items] == ["sony-wh-ch520-black", "bose-qc-black"]
    assert sp.items[0].price == 59.0 and sp.items[0].url == "https://www.jbhifi.com.au/products/sony-wh-ch520-black"


@pytest.mark.asyncio
async def test_jbhifi_get_product_jsonld(tmp_path, monkeypatch):
    a = _adapter("jbhifi", tmp_path, monkeypatch)
    html = open(f"{FIX}/jbhifi/product.html", encoding="utf-8").read()
    _stub_fetch(monkeypatch, a, body=html)
    it = await a.get_item("sony-wh-ch520-wireless-on-ear-headphones-black")
    assert it.id == "623348" and it.price == 59.0 and it.brand == "SONY"
    assert it.availability == Availability.IN_STOCK


def test_jbhifi_helpers(tmp_path, monkeypatch):
    a = _adapter("jbhifi", tmp_path, monkeypatch)
    assert a.parse_url("https://www.jbhifi.com.au/products/foo-bar")["id"] == "foo-bar"
    assert a._num("129") == 129.0 and a._num(None) is None
    from au_shopping_mcp.core.models import Availability as Av
    assert a._avail({"overallStatus": "InStock"}) == Av.IN_STOCK
    assert a._avail_ld("https://schema.org/InStock") == Av.IN_STOCK
    assert a.policy.start_tier == FetchTier.BROWSER


# ----------------------------------------------------------------- Sydney Tools (GraphQL)
@pytest.mark.asyncio
async def test_sydneytools_search(tmp_path, monkeypatch):
    a = _adapter("sydneytools", tmp_path, monkeypatch)
    resp = json.load(open(f"{FIX}/sydneytools/search.json"))["resp"]
    _stub_fetch(monkeypatch, a, json_data=resp)
    sp = await a.search("drill")
    assert sp.total and sp.items and all(i.url.startswith("https://sydneytools.com.au/product/") for i in sp.items)
    i0 = next(i for i in sp.items if i.price)
    assert i0.brand and i0.price and i0.currency == "AUD"


@pytest.mark.asyncio
async def test_sydneytools_get_item_matches_slug(tmp_path, monkeypatch):
    a = _adapter("sydneytools", tmp_path, monkeypatch)
    resp = json.load(open(f"{FIX}/sydneytools/search.json"))["resp"]
    _stub_fetch(monkeypatch, a, json_data=resp)
    slug = resp["data"]["viewer"]["products"]["edges"][0]["node"]["urlSlug"]
    it = await a.get_item(f"/product/{slug}")
    assert it.name and it.id
    assert a.parse_url(f"https://sydneytools.com.au/product/{slug}")["kind"] == "item"


# ----------------------------------------------------------------- The Good Guys (DOM + JSON-LD)
@pytest.mark.asyncio
async def test_goodguys_search_dom(tmp_path, monkeypatch):
    a = _adapter("goodguys", tmp_path, monkeypatch)
    cards = [
        {"href": "/philips-7000-series-kettle-black-hd939690", "title": "Philips 7000 Series Kettle $79", "price": "79", "image": "x.jpg"},
        {"href": "/breville-smart-kettle-bke840clr4jan2?ref=1", "title": "Breville Smart Kettle", "price": "199", "image": None},
    ]
    async def fake_dom(url, js, **kw):
        return _result(json_data=cards, url=url)
    monkeypatch.setattr(a.fetcher, "fetch_dom", fake_dom)
    sp = await a.search("kettle")
    assert len(sp.items) == 2
    assert sp.items[0].name == "Philips 7000 Series Kettle" and sp.items[0].price == 79.0
    assert sp.items[0].url == "https://www.thegoodguys.com.au/philips-7000-series-kettle-black-hd939690"


@pytest.mark.asyncio
async def test_goodguys_get_product_jsonld(tmp_path, monkeypatch):
    a = _adapter("goodguys", tmp_path, monkeypatch)
    html = open(f"{FIX}/goodguys/product.html", encoding="utf-8").read()
    _stub_fetch(monkeypatch, a, body=html)
    it = await a.get_item("philips-7000-series-kettle-black-with-temperature-control-hd939690")
    assert it.name and it.brand == "Philips" and it.price and it.availability == Availability.IN_STOCK


def test_goodguys_parse_url(tmp_path, monkeypatch):
    a = _adapter("goodguys", tmp_path, monkeypatch)
    assert a.parse_url("https://www.thegoodguys.com.au/philips-kettle-hd939690")["kind"] == "item"
    assert a.parse_url("https://www.thegoodguys.com.au/fridges-and-freezers") is None  # pure-word category


# ----------------------------------------------------------------- PC Case Gear (DOM + HTML)
@pytest.mark.asyncio
async def test_pccasegear_search_dom(tmp_path, monkeypatch):
    a = _adapter("pccasegear", tmp_path, monkeypatch)
    cards = [
        {"href": "/products/73263/amd-ryzen-7-5800x3d", "title": "AMD Ryzen 7 5800X3D In Stock Now! $549", "price": "549", "image": "x"},
        {"href": "/products/80001/asus-rtx-5070?v=1", "title": "ASUS RTX 5070", "price": "1099", "image": None},
    ]
    async def fake_dom(url, js, **kw):
        return _result(json_data=cards, url=url)
    monkeypatch.setattr(a.fetcher, "fetch_dom", fake_dom)
    sp = await a.search("rtx 5070")
    assert [i.id for i in sp.items] == ["73263", "80001"]
    assert sp.items[0].name == "AMD Ryzen 7 5800X3D" and sp.items[0].price == 549.0


@pytest.mark.asyncio
async def test_pccasegear_get_product_html(tmp_path, monkeypatch):
    a = _adapter("pccasegear", tmp_path, monkeypatch)
    html = open(f"{FIX}/pccasegear/product.html", encoding="utf-8").read()
    _stub_fetch(monkeypatch, a, body=html)
    it = await a.get_item("/products/73263/amd-ryzen-7-5800x3d-gaming-processor-10th-anniversary-edition")
    assert "Ryzen 7 5800X3D" in it.name and it.price == 549.0
    assert a.parse_url("https://www.pccasegear.com/products/73263/x")["id"] == "73263"
