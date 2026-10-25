"""QBD Books adapter over DomStoreAdapter (verified live 2026-09-08)."""
from __future__ import annotations
from au_shopping_mcp.adapters._domstore import DomStoreAdapter


class Adapter(DomStoreAdapter):
    name = "qbd"
    domain = "qbd.com.au"
    base_url = "https://www.qbd.com.au"
    description = "QBD Books: fiction, non-fiction and children's books - product search and detail."
    SEARCH_URL = "{base}/search/?q={q}"
    PRODUCT_LINK_RE = r"/\d{13}/?$"      # book URLs end in an ISBN-13
    NAME_STRIP = r"\s*\|\s*QBD.*$"
    WAIT_SELECTOR = 'a[href$="/"]'
