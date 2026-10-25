"""Mwave: PC hardware, laptops and tech - product search and detail.

Adapter over au_shopping_mcp.adapters._domstore.DomStoreAdapter (verified live 2026-09-06).
"""
from __future__ import annotations

from au_shopping_mcp.adapters._domstore import DomStoreAdapter


class Adapter(DomStoreAdapter):
    name = 'mwave'
    domain = 'mwave.com.au'
    base_url = 'https://www.mwave.com.au'
    description = 'Mwave: PC hardware, laptops and tech - product search and detail.'
    SEARCH_URL = '{base}/searchresult?w={q}'
    PRODUCT_LINK_RE = r'^/products/'
    NAME_STRIP = r'\\s*\\|\\s*Mwave.*$'
    WAIT_SELECTOR = 'a[href^="/products/"]'
