"""Core extractor tests on synthetic HTML - no network."""
from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.fetch import looks_blocked

HTML = """
<html><head><title>T</title>
<meta property="og:title" content="OG">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"Product","name":"Drill","sku":"0158411","brand":{"name":"Makita"},
  "offers":{"price":"199.00","priceCurrency":"AUD","availability":"https://schema.org/InStock"},
  "aggregateRating":{"ratingValue":"4.6","reviewCount":"120"}},
 {"@type":"BreadcrumbList","itemListElement":[{"name":"Home"},{"name":"Tools"},{"name":"Drills"}]}]}
</script>
<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"products":[{"itemNumber":"1","url":"/x_p0000001","name":"A","price":{"value":"9.5"}}],"totalResults":1}}}</script>
</head><body><main><p>hello</p><a href="/x_p0000001">A</a><a href="https://other.com/z">ext</a></main></body></html>
"""


def test_json_ld_product():
    tree = ex.parse(HTML)
    p = ex.json_ld_of_type(tree, "Product")
    assert p and p["sku"] == "0158411"
    assert ex.json_ld_of_type(tree, "BreadcrumbList")


def test_next_data_and_find_key():
    tree = ex.parse(HTML)
    nd = ex.next_data(tree)
    assert ex.find_key(nd, "totalResults") == 1
    assert ex.find_key(nd, "products")[0]["itemNumber"] == "1"


def test_links_same_domain_and_price():
    tree = ex.parse(HTML)
    ls = ex.links(tree, "https://www.bunnings.com.au/search")
    assert [l["url"] for l in ls] == ["https://www.bunnings.com.au/x_p0000001"]
    assert ex.parse_price("$1,299.95") == 1299.95
    assert ex.meta(tree, "og:title") == "OG"


def test_block_detection():
    assert looks_blocked(403, "", {})
    assert looks_blocked(200, "<html><head><title>Just a moment...</title>", {})
    assert looks_blocked(200, "<html>ok", {"cf-mitigated": "challenge"})
    assert looks_blocked(200, "<html><title>Drills</title>", {}) is None
