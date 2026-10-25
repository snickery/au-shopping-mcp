"""Bunnings adapter tests. Synthetic pages now; drop real saved pages into tests/fixtures/bunnings/
(search.html, product.html) and the fixture-backed tests will run against them."""
import os

import pytest

from au_shopping_mcp.adapters.bunnings import Adapter
from au_shopping_mcp.core.models import Availability, ExtractionError

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "bunnings")


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    from au_shopping_mcp.core import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    return Adapter()


def test_parse_url(adapter):
    assert adapter.parse_url("https://www.bunnings.com.au/makita-drill_p0158411") == {"kind": "item", "id": "0158411"}
    assert adapter.parse_url("https://www.bunnings.com.au/products/tools")["kind"] == "category"
    assert adapter.parse_url("https://www.bunnings.com.au/about") is None


def test_product_from_json_ld(adapter):
    html = """<html><head><script type="application/ld+json">
    {"@type":"Product","name":"Makita Drill","sku":"0158411","brand":{"name":"Makita"},
     "offers":{"price":"199","priceCurrency":"AUD","availability":"http://schema.org/InStock"}}
    </script></head><body><table><tr><th>Voltage</th><td>18V</td></tr></table></body></html>"""
    it = adapter._parse_product(html, "https://www.bunnings.com.au/makita-drill_p0158411")
    assert it.id == "0158411" and it.brand == "Makita" and it.price == 199.0
    assert it.availability == Availability.IN_STOCK
    assert it.specs == {"Voltage": "18V"}


def test_listing_selector_fallback(adapter):
    html = """<html><body>
    <article class="product"><a href="/a_p0000001"><h3>A</h3></a><span class="price">$10.00</span></article>
    <article class="product"><a href="/b_p0000002"><h3>B</h3></a><span class="price">$20.50</span></article>
    </body></html>"""
    sp = adapter._parse_listing(html, "https://www.bunnings.com.au/search/products?q=x")
    assert [i.id for i in sp.items] == ["0000001", "0000002"]
    assert sp.items[1].price == 20.5


def test_listing_empty_raises(adapter):
    with pytest.raises(ExtractionError):
        adapter._parse_listing("<html><body>nothing</body></html>", "https://www.bunnings.com.au/x")


@pytest.mark.skipif(not os.path.exists(os.path.join(FIX, "product.html")), reason="save a real product page as fixtures/bunnings/product.html")
def test_real_product_fixture(adapter):
    html = open(os.path.join(FIX, "product.html"), encoding="utf-8").read()
    it = adapter._parse_product(html, "https://www.bunnings.com.au/x_p0000000")
    assert it.name and it.price is not None


@pytest.mark.skipif(not os.path.exists(os.path.join(FIX, "search.html")), reason="save a real search page as fixtures/bunnings/search.html")
def test_real_search_fixture(adapter):
    html = open(os.path.join(FIX, "search.html"), encoding="utf-8").read()
    sp = adapter._parse_listing(html, "https://www.bunnings.com.au/search/products?q=drill")
    assert len(sp.items) >= 5 and all(i.url and i.id for i in sp.items)


def test_real_search_fixture_prefetch_key(adapter):
    """Live page 2026-10-25: the listing query key gained a leading "server-prefetch"."""
    with open(os.path.join(FIX, "search-prefetch.html"), encoding="utf-8") as f:
        html = f.read()
    assert '"server-prefetch","coveo-search"' in html.replace(" ", "")
    sp = adapter._parse_listing(html, "https://www.bunnings.com.au/search/products?q=cordless+drill")
    assert len(sp.items) >= 5 and all(i.url and i.id for i in sp.items)
    assert sp.total and sp.total > len(sp.items) and sp.has_more
    assert adapter._last_seen_store == ("NSWMetro", "7313")
