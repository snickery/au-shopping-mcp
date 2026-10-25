"""Chemist Warehouse (chemistwarehouse.com.au) adapter - pharmacy, health & beauty.

Verified live 2026-09-06. Cloudflare 403s plain HTTP but clears for headless Chromium, so search
runs through the browser tier (DomStoreAdapter's adaptive fallback handles this). Product pages
carry a schema.org Product JSON-LD.

  search:   /search?query=<q>       (browser DOM extraction of /buy/<id>/<slug> cards)
  product:  /buy/<id>/<slug>         (JSON-LD Product; get_item escalates HTTP->browser if blocked)
"""
from __future__ import annotations

from au_shopping_mcp.adapters._domstore import DomStoreAdapter


class Adapter(DomStoreAdapter):
    name = "chemistwarehouse"
    domain = "chemistwarehouse.com.au"
    base_url = "https://www.chemistwarehouse.com.au"
    description = "Chemist Warehouse: pharmacy, vitamins, health, beauty and personal care - product search and detail."
    SEARCH_URL = "{base}/search?query={q}"
    PRODUCT_LINK_RE = r"/buy/\d+/"
    NAME_STRIP = r"\s*\|\s*Chemist Warehouse.*$"
    WAIT_SELECTOR = 'a[href*="/buy/"]'
    PRODUCT_BROWSER = True
