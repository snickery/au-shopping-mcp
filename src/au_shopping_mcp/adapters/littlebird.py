"""Little Bird Electronics: maker boards, sensors, Raspberry Pi and hobby electronics - search and detail.

Adapter over au_shopping_mcp.adapters._domstore.DomStoreAdapter (verified live 2026-09-06).
"""
from __future__ import annotations

from au_shopping_mcp.adapters._domstore import DomStoreAdapter


class Adapter(DomStoreAdapter):
    name = 'littlebird'
    domain = 'littlebirdelectronics.com.au'
    base_url = 'https://littlebirdelectronics.com.au'
    description = 'Little Bird Electronics: maker boards, sensors, Raspberry Pi and hobby electronics - search and detail.'
    SEARCH_URL = '{base}/search?q={q}'
    PRODUCT_LINK_RE = r'^/products/'
    NAME_STRIP = r'\\s*[|–-]\\s*Little Bird.*$'
    WAIT_SELECTOR = None
