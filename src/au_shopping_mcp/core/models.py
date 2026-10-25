"""Normalised models shared by every site adapter.

Adapters map site-specific data into these; tools only ever see these.
Site-specific extras go in `extras`, never as new top-level fields.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class FetchTier(int, Enum):
    """Escalation ladder. Higher = more expensive, more likely to get through."""

    API = 0        # site's own JSON/XHR endpoint
    HTTP = 1       # httpx with browser-like headers + cookie jar
    BROWSER = 2    # headless Playwright, wait for content
    STEALTH = 3    # Playwright with persistent profile / stealth tweaks
    # 4 = external proxy / solver: not implemented, hook only


class Availability(str, Enum):
    IN_STOCK = "in_stock"
    LOW_STOCK = "low_stock"
    OUT_OF_STOCK = "out_of_stock"
    SPECIAL_ORDER = "special_order"
    ONLINE_ONLY = "online_only"
    UNKNOWN = "unknown"


class Item(BaseModel):
    """A product / listing / article — the thing a site is 'about'."""

    id: str = Field(description="Site's stable identifier, e.g. Bunnings item number 0158411")
    name: str
    url: str
    brand: str | None = None
    price: float | None = None
    currency: str | None = None
    availability: Availability = Availability.UNKNOWN
    rating: float | None = None
    review_count: int | None = None
    image: str | None = None
    description: str | None = None
    specs: dict[str, str] = Field(default_factory=dict)
    category_path: list[str] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict, description="Site-specific fields")


class ItemRef(BaseModel):
    """Lightweight result-list entry; call get_item for the full Item."""

    id: str
    name: str
    url: str
    brand: str | None = None
    price: float | None = None
    currency: str | None = None
    availability: Availability = Availability.UNKNOWN
    rating: float | None = None
    review_count: int | None = None
    image: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict, description="Site-specific listing fields (e.g. units sold)")


class SearchPage(BaseModel):
    query: str | None = None
    category: str | None = None
    page: int = 1
    page_size: int | None = None
    total: int | None = None
    items: list[ItemRef] = Field(default_factory=list)
    has_more: bool = False
    facets: dict[str, list[str]] = Field(default_factory=dict, description="Filter options seen on the page")
    source_url: str | None = None


class Category(BaseModel):
    name: str
    slug: str
    url: str
    children: list["Category"] = Field(default_factory=list)


class FetchResult(BaseModel):
    """What the fetcher hands back to an adapter."""

    url: str
    final_url: str
    status: int
    tier: FetchTier
    body: str
    headers: dict[str, str] = Field(default_factory=dict)
    cache_hit: bool = False
    elapsed_ms: int = 0
    json_data: Any | None = Field(default=None, description="Parsed body when content-type was JSON")


class FetchPolicy(BaseModel):
    """Per-domain politeness + strategy. Each adapter declares one."""

    start_tier: FetchTier = FetchTier.HTTP
    max_tier: FetchTier = FetchTier.STEALTH
    min_delay_s: float = 1.0
    jitter_s: float = 0.5
    max_concurrency: int = 2
    timeout_s: float = 20.0
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
    browser_wait_selector: str | None = Field(
        default=None, description="CSS selector Playwright waits for before returning HTML"
    )
    cache_ttl_search_s: int = 15 * 60
    cache_ttl_item_s: int = 60 * 60


class BlockedError(Exception):
    """Raised by a tier when the site refused us; fetcher uses this to escalate."""

    def __init__(self, reason: str, tier: FetchTier, status: int | None = None):
        super().__init__(f"[{tier.name}] blocked: {reason} (status={status})")
        self.reason = reason
        self.tier = tier
        self.status = status


class ExtractionError(Exception):
    """Page fetched fine but the adapter couldn't find what it expected — site probably changed."""
