"""Rate limiting and caching. Both keyed per domain so one server can front several sites."""
from __future__ import annotations

import asyncio
import hashlib
import random
import time
from typing import Any

import diskcache

from au_shopping_mcp.core import config
from au_shopping_mcp.core.models import FetchPolicy


class DomainLimiter:
    """min_delay + jitter between requests, bounded concurrency. One per domain."""

    def __init__(self, policy: FetchPolicy):
        self.policy = policy
        self._sem = asyncio.Semaphore(policy.max_concurrency)
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0
        self.total_requests = 0

    async def __aenter__(self):
        await self._sem.acquire()
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_allowed = time.monotonic() + self.policy.min_delay_s + random.uniform(
                0, self.policy.jitter_s
            )
            self.total_requests += 1
        return self

    async def __aexit__(self, *exc):
        self._sem.release()


class Cache:
    """Thin diskcache wrapper. Keys are hashed (url + variant), values are pickled models."""

    def __init__(self, directory: str | None = None):
        self._dc = diskcache.Cache(directory or config.CACHE_DIR)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(*parts: Any) -> str:
        raw = "|".join(str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> Any | None:
        val = self._dc.get(key)
        if val is None:
            self.misses += 1
        else:
            self.hits += 1
        return val

    def set(self, key: str, value: Any, ttl_s: int) -> None:
        self._dc.set(key, value, expire=ttl_s)

    def clear(self) -> None:
        self._dc.clear()

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 2) if total else 0.0
