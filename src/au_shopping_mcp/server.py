"""au-shopping MCP server. `SITES=all au-shopping-mcp` (or `--stdio`); `SITE=bunnings` for one site.

Generic tool surface over the SiteAdapters SITES selects. Tool names are
prefixed with the adapter name (bunnings_search_products, ...), so one process
can serve every site, and single-site instances can still sit side by side in
one client without clashing.
"""
import functools
import inspect
import logging
import sys
import time
from types import SimpleNamespace
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp_server_kit import serve

from au_shopping_mcp.adapters.base import SiteAdapter, load_adapters
from au_shopping_mcp.core import extract as ex
from au_shopping_mcp.core.config import MCP_BEARER_TOKEN, PORT, SITE, SITES, setup_logging
from au_shopping_mcp.core.models import BlockedError, ExtractionError

setup_logging()
logger = logging.getLogger(__name__)

# --- Per-call request log + uniform error shaping ---------------------------
# Tools here are async (httpx/Playwright are async), so no thread hop is needed;
# the wrapper only adds the tool=/outcome=/duration_ms= line the rest of the
# collection emits, and turns fetch/extract failures into a structured
# {"error": ...} the agent can act on instead of a raw traceback.
req_logger = logging.getLogger("au_shopping_mcp.requests")


def _instructions(adapters: list[SiteAdapter]) -> str:
    if len(adapters) == 1:
        a = adapters[0]
        p = a.name
        return (
            f"{a.description} "
            f"Start with {p}_search_products for a shortlist, then {p}_get_product for full detail on one "
            f"item; every result carries a URL you can give the user. Results are cached and rate-limited "
            f"per site. If a tool reports the site blocked us, wait and retry rather than hammering it — "
            f"{p}_site_status shows the current fetch tier and the last block. {p}_probe_page is a "
            f"diagnostic for when a tool says the page layout changed."
        )
    shops = "\n".join(f"- {a.name}: {a.description}" for a in adapters)
    return (
        f"Product search across {len(adapters)} retail sites, with one tool set per site. Every tool name "
        f"starts with the site's name: <site>_search_products, <site>_get_product, <site>_browse_category, "
        f"<site>_list_categories, <site>_fetch_page, <site>_probe_page, <site>_site_status.\n\n"
        f"Sites:\n{shops}\n\n"
        f"Start with <site>_search_products for a shortlist, then <site>_get_product for full detail on one "
        f"item; every result carries a URL you can give the user. To compare prices, search the relevant "
        f"sites (staticice already compares many AU stores in one search). Results are cached and "
        f"rate-limited per site. If a tool reports the site blocked us, wait and retry rather than "
        f"hammering it — <site>_site_status shows the current fetch tier and the last block. "
        f"<site>_probe_page is a diagnostic for when a tool says the page layout changed. URL-taking "
        f"tools only accept URLs on their own site."
    )


def _error_payload(adapter: SiteAdapter, e: Exception) -> dict[str, Any]:
    p = adapter.name
    if isinstance(e, BlockedError):
        return {"error": "blocked", "tier": e.tier.name, "reason": e.reason, "status": e.status,
                "guidance": (f"{adapter.domain} refused the request. The server escalates to a higher fetch tier "
                             f"automatically on the next call if one is allowed; otherwise wait a few minutes. "
                             f"See {p}_site_status.")}
    if isinstance(e, ExtractionError):
        return {"error": "extraction", "reason": str(e), "guidance": f"Run {p}_probe_page on the same URL."}
    if isinstance(e, NotImplementedError):
        return {"error": "unsupported", "reason": str(e)}
    logger.exception("tool failed")
    return {"error": type(e).__name__, "reason": str(e)}


def _offsite(adapter: SiteAdapter, url: str | None) -> dict[str, Any] | None:
    """Structured refusal for a caller-supplied URL that is not on this site."""
    if url and (reason := adapter.offsite_reason(url)):
        return {"error": "offsite_url", "reason": reason,
                "guidance": f"Pass an id, a site-relative path, or a {adapter.domain} URL."}
    return None


def _logged_tool(mcp: FastMCP, adapter: SiteAdapter, name: str):
    """mcp.tool(name=...) plus the request log line and error shaping for one site's tool."""

    def decorator(fn):
        # FastMCP publishes __doc__ verbatim. These tools are nested inside register_site, one
        # level deeper than the module-level functions they replaced, so drop the extra indent
        # to keep every description byte-identical to what clients already have.
        register = mcp.tool(name=name, description=fn.__doc__.replace("\n        ", "\n    "))

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            outcome = "ok"
            try:
                return await fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - shaped for the agent, logged with traceback
                outcome = "error"
                return _error_payload(adapter, e)
            finally:
                req_logger.info("tool=%s outcome=%s duration_ms=%d",
                                name, outcome, int((time.perf_counter() - start) * 1000))

        wrapper.__signature__ = inspect.signature(fn)
        register(wrapper)
        return fn

    return decorator


# --- Tools -------------------------------------------------------------------

def register_site(mcp: FastMCP, adapter: SiteAdapter) -> SimpleNamespace:
    """Register one site's seven tools. Returns them (plus the adapter) for direct calls."""
    P = adapter.name  # tool prefix

    def tool(suffix: str):
        return _logged_tool(mcp, adapter, f"{P}_{suffix}")

    @tool("search_products")
    async def search_products(query: str, page: int = 1, sort: str | None = None, max_results: int = 20) -> dict:
        """Search the site and return a shortlist: name, brand, price, rating, item id and URL
        per result. Paginated - has_more/next_page tell you when to call again. Use get_product
        for full detail on one item. sort: see site_status().sort_options."""
        sp = await adapter.search(query, page=page, sort=sort)
        sp.items = sp.items[: max(1, min(max_results, 60))]
        out = sp.model_dump(mode="json")
        out["next_page"] = page + 1 if sp.has_more else None
        return out

    @tool("get_product")
    async def get_product(id_or_url: str) -> dict:
        """Full detail for one product by item number (e.g. '0158411') or URL: price, availability,
        rating, description, specifications, category path, image, plus site-specific extras
        (e.g. store-level stock)."""
        if err := _offsite(adapter, id_or_url):
            return err
        return (await adapter.get_item(id_or_url)).model_dump(mode="json")

    @tool("browse_category")
    async def browse_category(category: str, page: int = 1, sort: str | None = None, max_results: int = 20) -> dict:
        """List products in a category (path or URL, e.g. '/products/tools/power-tools/drills').
        Same shape as search_products. Use list_categories to find paths."""
        if err := _offsite(adapter, category):
            return err
        sp = await adapter.browse(category, page=page, sort=sort)
        sp.items = sp.items[: max(1, min(max_results, 60))]
        out = sp.model_dump(mode="json")
        out["next_page"] = page + 1 if sp.has_more else None
        return out

    @tool("list_categories")
    async def list_categories(parent: str | None = None) -> list:
        """Category names and paths, top level or under a parent path/URL. Feed a path into
        browse_category."""
        if err := _offsite(adapter, parent):
            return [err]
        return [c.model_dump(mode="json") for c in (await adapter.categories(parent))[:120]]

    @tool("fetch_page")
    async def fetch_page(url: str, mode: str = "text") -> dict:
        """Escape hatch for pages the structured tools don't cover (guides, store pages, promos).
        url: site-relative, or absolute on this site only (other hosts are refused). mode: 'text' (readable text + top links), 'links' (all
        same-site links), 'raw' (full HTML, large). Same polite fetch ladder and cache."""
        if mode not in ("text", "links", "raw"):
            return {"error": "bad_argument", "reason": "mode must be text, links or raw"}
        if err := _offsite(adapter, url):
            return err
        return await adapter.fetch_page(url, mode)

    @tool("probe_page")
    async def probe_page(url: str) -> dict:
        """Diagnostic: fetch a page and report what structured data it exposes (JSON-LD types,
        Next.js data keys, window state keys, og meta, sample item links, script names). Use
        when a tool says the layout changed, or when onboarding a new site adapter."""
        if err := _offsite(adapter, url):
            return err
        if not url.startswith("http"):
            url = adapter.base_url.rstrip("/") + "/" + url.lstrip("/")
        res = await adapter.fetcher.fetch(url)
        tree = ex.parse(res.body)
        ld = ex.json_ld(tree)
        nd = ex.next_data(tree)
        ws = ex.window_state(res.body)
        item_links = [l["url"] for l in ex.links(tree, res.final_url)
                      if (adapter.parse_url(l["url"]) or {}).get("kind") == "item"]
        return {
            "final_url": res.final_url, "status": res.status, "tier": res.tier.name, "bytes": len(res.body),
            "json_ld_types": sorted({str(b.get("@type")) for b in ld}),
            "next_data": {"present": nd is not None, "top_keys": list((nd or {}).keys())[:15],
                          "page_props_keys": list(((nd or {}).get("props") or {}).get("pageProps", {}).keys())[:25]},
            "window_state_top_keys": list((ws or {}).keys())[:25] if ws else None,
            "og_meta": {k: ex.meta(tree, k) for k in ("og:title", "og:type", "og:url") if ex.meta(tree, k)},
            "item_links_sample": item_links[:5],
            "script_src_hints": sorted({s.attributes.get("src", "").split("?")[0].rsplit("/", 1)[-1]
                                        for s in tree.css("script[src]")})[:20],
        }

    @tool("site_status")
    async def site_status(include_policy: bool = False) -> dict:
        """One-shot health: current fetch tier vs ceiling, last block seen, request/cache counters,
        sort options, and site-specific config (e.g. selected store). include_policy=True adds the
        full fetch policy (delays, concurrency, UA, cache TTLs)."""
        # (Every tool needs at least one parameter: Claude.ai's remote MCP client rejects
        # tools with an empty input schema.)
        out = {
            "site": adapter.name, "domain": adapter.domain, "description": adapter.description,
            "fetch": adapter.fetcher.status(),
            "cache": {"hits": adapter.cache.hits, "misses": adapter.cache.misses, "hit_rate": adapter.cache.hit_rate},
            "sort_options": adapter.sort_options(),
            **adapter.site_specific_status(),
        }
        if include_policy:
            out["policy"] = adapter.policy.model_dump(mode="json")
        return out

    return SimpleNamespace(adapter=adapter, search_products=search_products, get_product=get_product,
                           browse_category=browse_category, list_categories=list_categories,
                           fetch_page=fetch_page, probe_page=probe_page, site_status=site_status)


def build_server(adapters: list[SiteAdapter]) -> tuple[FastMCP, dict[str, SimpleNamespace]]:
    """One FastMCP serving every given site. Named after the site when there is only one."""
    mcp = FastMCP(
        adapters[0].name if len(adapters) == 1 else "au-shopping",
        instructions=_instructions(adapters),
        host="0.0.0.0",
        port=PORT,
        stateless_http=True,
        json_response=True,
    )
    return mcp, {a.name: register_site(mcp, a) for a in adapters}


mcp, sites = build_server(load_adapters(SITES, SITE))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        from au_shopping_mcp import auth
        raise SystemExit(auth.main(sys.argv[2:]))
    serve(mcp, port=PORT, bearer_token=MCP_BEARER_TOKEN)


if __name__ == "__main__":
    main()
