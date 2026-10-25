"""Anaconda: camping, hiking, outdoor and adventure gear - product search and detail.

Adapter over au_shopping_mcp.adapters._domstore.DomStoreAdapter (verified live 2026-09-06).
"""
from __future__ import annotations

from au_shopping_mcp.adapters._domstore import DomStoreAdapter


class Adapter(DomStoreAdapter):
    name = 'anaconda'
    domain = 'anacondastores.com'
    base_url = 'https://www.anacondastores.com'
    description = 'Anaconda: camping, hiking, outdoor and adventure gear - product search and detail.'
    SEARCH_URL = '{base}/search?q={q}'
    PRODUCT_LINK_RE = r'/en-au/p/'
    NAME_STRIP = r'\\s*\\|\\s*Anaconda.*$'
    WAIT_SELECTOR = 'a[href*="/p/"]'
