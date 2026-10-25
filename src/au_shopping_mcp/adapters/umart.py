"""Umart: PC components, systems and peripherals - product search and detail.

Adapter over au_shopping_mcp.adapters._domstore.DomStoreAdapter (verified live 2026-09-06).
"""
from __future__ import annotations

from au_shopping_mcp.adapters._domstore import DomStoreAdapter


class Adapter(DomStoreAdapter):
    name = 'umart'
    domain = 'umart.com.au'
    base_url = 'https://www.umart.com.au'
    description = 'Umart: PC components, systems and peripherals - product search and detail.'
    SEARCH_URL = '{base}/umart1/pro/index.php?keywords={q}'
    PRODUCT_LINK_RE = r'^/product/'
    NAME_STRIP = r'\\s*\\|\\s*Umart.*$'
    WAIT_SELECTOR = 'a[href^="/product/"]'
