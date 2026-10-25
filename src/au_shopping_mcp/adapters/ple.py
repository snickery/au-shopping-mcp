"""PLE Computers adapter over DomStoreAdapter (verified live 2026-09-08)."""
from __future__ import annotations
from au_shopping_mcp.adapters._domstore import DomStoreAdapter


class Adapter(DomStoreAdapter):
    name = "ple"
    domain = "ple.com.au"
    base_url = "https://www.ple.com.au"
    description = "PLE Computers: PC components, systems and peripherals - product search and detail."
    SEARCH_URL = "{base}/Search/?SearchText={q}"
    PRODUCT_LINK_RE = r"/products/\d+/"
    NAME_STRIP = r"\s*[-|]\s*PLE Computers.*$"
    WAIT_SELECTOR = 'a[href*="/products/"]'
