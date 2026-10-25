"""Supercheap Auto: car parts, oil, batteries, tools and accessories - product search and detail.

Adapter over au_shopping_mcp.adapters._domstore.DomStoreAdapter (verified live 2026-09-06).
"""
from __future__ import annotations

from au_shopping_mcp.adapters._domstore import DomStoreAdapter


class Adapter(DomStoreAdapter):
    name = 'supercheap'
    domain = 'supercheapauto.com.au'
    base_url = 'https://www.supercheapauto.com.au'
    description = 'Supercheap Auto: car parts, oil, batteries, tools and accessories - product search and detail.'
    SEARCH_URL = '{base}/search?q={q}'
    PRODUCT_LINK_RE = r'/p/'
    NAME_STRIP = r'\\s*\\|\\s*Supercheap.*$'
    WAIT_SELECTOR = 'a[href*="/p/"]'
