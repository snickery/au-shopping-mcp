"""Petbarn: pet food, supplies and accessories for dogs, cats and more - product search and detail.

Adapter over au_shopping_mcp.adapters._domstore.DomStoreAdapter (verified live 2026-09-08).
"""
from __future__ import annotations

from au_shopping_mcp.adapters._domstore import DomStoreAdapter


class Adapter(DomStoreAdapter):
    name = 'petbarn'
    domain = 'petbarn.com.au'
    base_url = 'https://www.petbarn.com.au'
    description = 'Petbarn: pet food, supplies and accessories for dogs, cats and more - product search and detail.'
    SEARCH_URL = '{base}/catalogsearch/result/?q={q}'
    PRODUCT_LINK_RE = r'^/p/'
    NAME_STRIP = r'\\s*[|–-]\\s*Petbarn.*$'
    WAIT_SELECTOR = 'a[href^="/p/"]'
