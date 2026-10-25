"""MSY: PC parts, laptops and peripherals - product search and detail.

Adapter over au_shopping_mcp.adapters._domstore.DomStoreAdapter (verified live 2026-09-06).
"""
from __future__ import annotations

from au_shopping_mcp.adapters._domstore import DomStoreAdapter


class Adapter(DomStoreAdapter):
    name = 'msy'
    domain = 'msy.com.au'
    base_url = 'https://www.msy.com.au'
    description = 'MSY: PC parts, laptops and peripherals - product search and detail.'
    SEARCH_URL = '{base}/search.php?search_query={q}'
    PRODUCT_LINK_RE = r'^/product/'
    NAME_STRIP = r'\\s*\\|\\s*msy.*$'
    WAIT_SELECTOR = 'a[href^="/product/"]'
