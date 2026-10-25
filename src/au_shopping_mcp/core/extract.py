"""Site-agnostic extraction helpers. Adapters compose these; prefer JSON sources over selectors."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser


def parse(html: str) -> HTMLParser:
    return HTMLParser(html)


# ---------- embedded JSON ----------

def json_ld(tree: HTMLParser) -> list[dict[str, Any]]:
    """All <script type=application/ld+json> blobs, flattened (handles @graph and lists)."""
    out: list[dict[str, Any]] = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text(strip=True))
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                stack.extend(cur)
            elif isinstance(cur, dict):
                if "@graph" in cur:
                    stack.extend(cur["@graph"])
                out.append(cur)
    return out


def json_ld_of_type(tree: HTMLParser, type_name: str) -> dict[str, Any] | None:
    for blob in json_ld(tree):
        t = blob.get("@type")
        if t == type_name or (isinstance(t, list) and type_name in t):
            return blob
    return None


def next_data(tree: HTMLParser) -> dict[str, Any] | None:
    node = tree.css_first("script#__NEXT_DATA__")
    if not node:
        return None
    try:
        return json.loads(node.text())
    except json.JSONDecodeError:
        return None


_STATE_PATTERNS = [
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S),
    re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S),
    re.compile(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S),
]


def window_state(html: str) -> dict[str, Any] | None:
    """Common hydration globals. Cheap regex; returns first parseable match."""
    for pat in _STATE_PATTERNS:
        m = pat.search(html)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


def find_key(obj: Any, key: str, *, max_depth: int = 12) -> Any | None:
    """Depth-first search for the first occurrence of `key` in nested JSON. Handy when a
    hydration blob's shape is unknown but you know the field name you want."""
    if max_depth < 0:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key, max_depth=max_depth - 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key, max_depth=max_depth - 1)
            if r is not None:
                return r
    return None


# ---------- selectors ----------

def text(tree: HTMLParser, selector: str, default: str | None = None) -> str | None:
    node = tree.css_first(selector)
    return node.text(strip=True) if node else default


def attr(tree: HTMLParser, selector: str, name: str, default: str | None = None) -> str | None:
    node = tree.css_first(selector)
    return node.attributes.get(name, default) if node else default


def meta(tree: HTMLParser, prop: str) -> str | None:
    return attr(tree, f'meta[property="{prop}"]', "content") or attr(tree, f'meta[name="{prop}"]', "content")


_PRICE_RE = re.compile(r"(\d[\d,]*(?:\.\d{1,2})?)")


def parse_price(s: str | None) -> float | None:
    if not s:
        return None
    m = _PRICE_RE.search(s.replace(" ", ""))
    return float(m.group(1).replace(",", "")) if m else None


# ---------- generic page content ----------

def links(tree: HTMLParser, base_url: str, *, same_domain_only: bool = True) -> list[dict[str, str]]:
    from au_shopping_mcp.core.fetch import domain_of
    base_dom = domain_of(base_url)
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href).split("#", 1)[0]
        if same_domain_only and domain_of(url) != base_dom:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"text": a.text(strip=True)[:120], "url": url})
    return out


_NOISE = "script, style, noscript, nav, footer, header, iframe, svg, form, [role=navigation], [aria-hidden=true]"


def readable_text(tree: HTMLParser, *, max_chars: int = 8000) -> str:
    """Strip chrome and return main text. Prefers <main>/<article>; falls back to body."""
    root = tree.css_first("main") or tree.css_first("article") or tree.css_first("body") or tree.root
    for n in root.css(_NOISE):
        n.decompose()
    txt = root.text(separator="\n", strip=True)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt[:max_chars] + ("\n…[truncated]" if len(txt) > max_chars else "")
