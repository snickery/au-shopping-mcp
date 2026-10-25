"""Bunnings react-query (dehydratedState) parsing - synthetic pages shaped like the live site
(verified 2026-09-06). The fixture-backed tests in test_bunnings.py cover the real pages."""
import json

import pytest

from au_shopping_mcp.adapters.bunnings import Adapter
from au_shopping_mcp.core.models import Availability


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    from au_shopping_mcp.core import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    return Adapter()


def _page(queries):
    nd = {"props": {"pageProps": {"dehydratedState": {"queries": queries}}}}
    return f'<html><head></head><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></body></html>'


def _coveo(results, total, first=0, n=36, key0="coveo-search", region="NSWMetro", store="7313"):
    return {"queryKey": [key0, "/search/products?q=x", region, store, {"body": {"firstResult": first, "numberOfResults": n}}],
            "state": {"data": {"results": [{"raw": r} for r in results], "totalCount": total,
                               "facets": [{"facetId": "@brandname", "values": [{"value": "Ozito"}, {"value": "Ryobi"}]}],
                               "correctionData": {"correctedQuery": ""}}}}


RAW = {"itemnumber": "0299323", "code": "0299323", "name": "Ozito PXC 18V Cordless Drill Driver Kit",
       "productroutingurl": "/ozito-pxc-18v-cordless-drill-driver-kit_p0299323", "brandname": "Ozito PXC",
       "price": 89.98, "currency": "AUD", "rating": 4.35, "ratingcount": 38, "isactive": True,
       "thumbnailimageurl": "https://media.bunnings.com.au/x"}


def test_listing_from_coveo(adapter):
    sp = adapter._parse_listing(_page([_coveo([RAW, {**RAW, "itemnumber": "0000002", "code": "0000002", "productroutingurl": "/b_p0000002"}], total=240)]),
                                "https://www.bunnings.com.au/search/products?q=x")
    assert [i.id for i in sp.items] == ["0299323", "0000002"]
    it = sp.items[0]
    assert it.url == "https://www.bunnings.com.au/ozito-pxc-18v-cordless-drill-driver-kit_p0299323"
    assert it.brand == "Ozito PXC" and it.price == 89.98 and it.rating == 4.35 and it.review_count == 38
    assert sp.total == 240 and sp.page_size == 36 and sp.has_more is True
    assert sp.facets["@brandname"] == ["Ozito", "Ryobi"]
    assert adapter._last_seen_store == ("NSWMetro", "7313")


def test_listing_last_page_has_no_more(adapter):
    sp = adapter._parse_listing(_page([_coveo([RAW], total=37, first=36)]), "https://www.bunnings.com.au/search/products?q=x&page=2")
    assert sp.has_more is False


def test_listing_category_search_key(adapter):
    sp = adapter._parse_listing(_page([_coveo([RAW], total=1, key0="category-search")]), "https://www.bunnings.com.au/products/tools")
    assert len(sp.items) == 1


def test_listing_zero_results_is_empty_not_error(adapter):
    sp = adapter._parse_listing(_page([_coveo([], total=0)]), "https://www.bunnings.com.au/search/products?q=zzz")
    assert sp.items == [] and sp.total == 0 and sp.has_more is False


def test_listing_without_result_set_falls_through_to_selectors(adapter):
    html = _page([]).replace("</body>", '<article class="product"><a href="/a_p0000001"><h3>A</h3></a><span class="price">$10</span></article></body>')
    sp = adapter._parse_listing(html, "https://www.bunnings.com.au/search/products?q=x")
    assert [i.id for i in sp.items] == ["0000001"]


def _product_page(stock=98, special=False, poa=False, delivery=True):
    q = [
        {"queryKey": ["product-retail-price", "0299323", "7313"],
         "state": {"data": {"currencyIso": "AUD", "value": 89.98, "priceType": "FROM", "hasMultipleOffers": False}}},
        {"queryKey": ["product-fulfilment", "0299323", "7313", {"body": {}}],
         "state": {"data": {"poa": poa, "isSpecialProduct": special, "driveNCollect": "NOT_AVAILABLE",
                            "clickNCollectData": {"stock": stock, "storeName": "Alexandria Warehouse", "isClicknCollectAvailable": True, "clickNCollectStockTxt": "inStock"},
                            "inStorePickUpData": {"stock": stock, "storeName": "Alexandria", "inStoreStockTxt": "inStock", "inStorePickUpAvailable": True},
                            "deliveryData": {"isDeliveryAvailable": delivery}}}},
        {"queryKey": ["retail-product", "0299323", "7313"],
         "state": {"data": {"code": "0299323", "name": "Ozito PXC 18V Cordless Drill Driver Kit PXDDK-250C",
                            "brand": {"name": "Ozito PXC", "code": "ozito-pxc"}, "averageRating": 4.34, "numberOfReviews": 38,
                            "images": [{"imageType": "PRIMARY", "url": "https://media.bunnings.com.au/p"}, {"imageType": "OTHERS", "url": "https://media.bunnings.com.au/o"}],
                            "feature": {"description": "A drill.", "keySellingPoints": ["13mm chuck"]},
                            "classifications": [{"features": [{"name": "Chuck Size", "featureValues": [{"value": "13"}]},
                                                              {"name": "Battery Type", "featureValues": [{"value": "18V"}, {"value": "Li-Ion"}]}]}],
                            "allCategories": [{"level": 2, "displayName": "Power Tools", "internalPath": "/tools/power-tools"},
                                              {"level": 1, "displayName": "Tools", "internalPath": "/tools"}],
                            "warrantyReturns": "5 Year Replacement Warranty", "isSpecialProduct": special,
                            "guideDocument": [{"docType": "Manual", "url": "https://media.bunnings.com.au/m.pdf"}]}}},
        {"queryKey": ["product-aisle-item-location", "0299323", "7313"],
         "state": {"data": [{"inStoreLocations": [{"aisle": "139", "bay": "2"}]}]}},
    ]
    return _page(q)


def test_product_from_hydration(adapter):
    it = adapter._parse_product(_product_page(), "https://www.bunnings.com.au/ozito_p0299323")
    assert it.id == "0299323" and it.brand == "Ozito PXC" and it.price == 89.98 and it.currency == "AUD"
    assert it.availability == Availability.IN_STOCK
    assert it.rating == 4.34 and it.review_count == 38
    assert it.image == "https://media.bunnings.com.au/p"
    assert it.description == "A drill."
    assert it.specs == {"Chuck Size": "13", "Battery Type": "18V, Li-Ion"}
    assert it.category_path == ["Tools", "Power Tools"]
    assert it.extras["category_url"].endswith("/products/tools/power-tools")
    assert it.extras["store_stock"]["store"] == "Alexandria" and it.extras["store_stock"]["stock"] == 98
    assert it.extras["aisle_locations"] == [{"aisle": "139", "bay": "2"}]
    assert it.extras["warranty"].startswith("5 Year") and it.extras["documents"][0]["type"] == "Manual"
    assert it.extras["price_type"] == "FROM"


@pytest.mark.parametrize("stock,special,poa,delivery,expected", [
    (98, False, False, True, Availability.IN_STOCK),
    (2, False, False, True, Availability.LOW_STOCK),
    (0, False, False, True, Availability.ONLINE_ONLY),
    (0, False, False, False, Availability.OUT_OF_STOCK),
    (5, True, False, True, Availability.IN_STOCK),      # special product but physically stocked
    (5, False, True, True, Availability.SPECIAL_ORDER),  # price on application
])
def test_product_availability_mapping(adapter, stock, special, poa, delivery, expected):
    it = adapter._parse_product(_product_page(stock=stock, special=special, poa=poa, delivery=delivery), "https://www.bunnings.com.au/x_p0299323")
    assert it.availability == expected


def test_item_url_forms(adapter):
    assert adapter._item_url("0299323") == "https://www.bunnings.com.au/_p0299323"
    assert adapter._item_url("/a_p0299323") == "https://www.bunnings.com.au/a_p0299323"
    assert adapter._item_url("https://www.bunnings.com.au/a_p1") == "https://www.bunnings.com.au/a_p1"


def test_search_url_sort(adapter):
    assert adapter._search_url("cordless drill", 2, "price_asc") == "https://www.bunnings.com.au/search/products?q=cordless+drill&page=2&sort=PriceAscending"
    assert "sort=" not in adapter._search_url("x", 1, None)
    with pytest.raises(ValueError):
        adapter._search_url("x", 1, "cheapest")


def test_store_cookies_require_both(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "STORE_ID", "7313")
    monkeypatch.setattr(adapter, "REGION", None)
    assert adapter._store_cookies() == {}
    monkeypatch.setattr(adapter, "REGION", "NSWMetro")
    assert adapter._store_cookies() == {"storeId": "7313", "regionCode": "NSWMetro"}
