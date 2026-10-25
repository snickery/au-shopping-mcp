"""Priceline (pharmacy/beauty) adapter over DomStoreAdapter (verified live 2026-09-08)."""
from __future__ import annotations
from au_shopping_mcp.adapters._domstore import DomStoreAdapter


class Adapter(DomStoreAdapter):
    name = "priceline"
    domain = "priceline.com.au"
    base_url = "https://www.priceline.com.au"
    description = "Priceline: pharmacy, health, beauty and wellbeing products - product search and detail."
    SEARCH_URL = "{base}/search/{q}"
    SEARCH_QUOTE_PLUS = False
    PRODUCT_LINK_RE = r"/product/\d+/"
    NAME_STRIP = r"\s*\|\s*Priceline.*$"
    WAIT_SELECTOR = 'a[href*="/product/"]'
