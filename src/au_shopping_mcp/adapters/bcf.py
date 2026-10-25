"""BCF (Boating Camping Fishing) adapter over DomStoreAdapter (verified live 2026-09-08)."""
from __future__ import annotations
from au_shopping_mcp.adapters._domstore import DomStoreAdapter


class Adapter(DomStoreAdapter):
    name = "bcf"
    domain = "bcf.com.au"
    base_url = "https://www.bcf.com.au"
    description = "BCF (Boating Camping Fishing): tents, camping, fishing and outdoor gear - product search and detail."
    SEARCH_URL = "{base}/search?q={q}"
    PRODUCT_LINK_RE = r"/p/[a-z0-9-]+/\d+"
    NAME_STRIP = r"\s*[|–-]\s*BCF.*$"
    WAIT_SELECTOR = 'a[href*="/p/"]'
