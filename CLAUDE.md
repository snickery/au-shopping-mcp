# CLAUDE.md

Shopping MCP server for Australian online stores, built from per-site adapters. `SITES=all` (or
`SITE=<adapter>`) selects the sites; tools are prefixed with the adapter
name (`bunnings_search_products`). Bunnings is the first adapter; the architecture is meant to be
reused for other, possibly less scraping-friendly, sites. Same conventions as the rest of the
`*-mcp` collection: `src/` layout, hatchling + uv, FastMCP (mcp<2), bearer middleware, `--stdio`.

## Layout

- `src/au_shopping_mcp/server.py` — generic tool surface. No site-specific logic here.
- `src/au_shopping_mcp/adapters/base.py` — `SiteAdapter` contract. `server.py` only talks to this.
- `src/au_shopping_mcp/adapters/bunnings.py` — Bunnings adapter, verified live 2026-09-06 (react-query cache in `__NEXT_DATA__`; see the module docstring for the query keys and the store cookies).
- `src/au_shopping_mcp/adapters/aliexpress.py` — AliExpress, verified live 2026-09-21. The only non-AU, marketplace-shaped site: listings from the `_dida_config_._init_data_` JS literal over HTTP, product detail from the rendered DOM (`fetch_dom`), currency/region via the `aep_usuc_f` cookie. Read its docstring before touching it.
- `src/au_shopping_mcp/adapters/cutter.py` — Cutter Electronics, verified live 2026-09-21. Tier-0 WooCommerce Store API (`/wp-json/wc/store/v1`): paging totals live in `x-wp-*` headers, prices are integer minor units, `orderby` is a fixed enum, category filters need the numeric term id.
- `src/au_shopping_mcp/adapters/_domstore.py` — `DomStoreAdapter`, the ~12-line-subclass base most retail adapters use.
- `src/au_shopping_mcp/adapters/_template.py` — copy this for a new site.
- `src/au_shopping_mcp/core/fetch.py` — escalation ladder HTTP → BROWSER → STEALTH with block detection.
- `src/au_shopping_mcp/core/extract.py` — JSON-LD / `__NEXT_DATA__` / window state / selectors / links / text.
- `src/au_shopping_mcp/core/politeness.py` — per-domain rate limiter + diskcache.
- `tests/` — synthetic tests always run; fixture tests use the real saved pages in `tests/fixtures/<site>/`.

## Rules

- Extraction order in every adapter: hydration state → JSON-LD → CSS selectors. Selectors go in the
  adapter's `SELECTORS` dict, nowhere else.
- Site-scoping cookies (e.g. Bunnings store, AliExpress `aep_usuc_f` currency/region) go through
  `fetcher.fetch(cookies=...)` / `fetch_dom(cookies=...)`, never a raw `Cookie:` header — httpx's jar
  overwrites a hand-built header once the jar has cookies for the domain, and the browser tier needs
  them in the context. Getting this wrong is silent: you get another store's stock, or another
  currency's prices.
- Every tool needs at least one parameter (Claude.ai's remote MCP client rejects empty input schemas);
  `test_every_tool_has_a_parameter` enforces it.
- New site-specific fields go in `Item.extras` (or `ItemRef.extras` for listing-only signals like
  units sold), not new top-level model fields.
- Never bypass `self.fetcher` / `self.cached()` — that's where rate limiting, caching and tier memory live.
- Tools return dicts/lists (JSON), never markdown strings. Errors are `{"error", "reason", "guidance"}`.
- Keep `mcp<2` until the rest of the collection migrates (SDK 2.x renamed FastMCP → MCPServer).
- Tool docstrings are the descriptions the LLM sees; keep the first line a complete summary.
- `playwright==` pin must match the Containerfile base image tag.

## Workflow for verifying / fixing an adapter

1. `SITE=bunnings uv run au-shopping-mcp --stdio`, or drive the adapter from a Python REPL.
2. Call `<site>_probe_page` on a search URL and a product URL. Look at `json_ld_types`,
   `next_data.page_props_keys`, `window_state_top_keys`, `item_links_sample`.
3. DevTools → Network → Fetch/XHR for a JSON search endpoint. If one exists, implement
   `search()` with `self.fetcher.fetch_json()` (tier 0) and remove the HTML parsing path.
4. Save real pages to `tests/fixtures/<site>/search.html` and `product.html`; run `uv run pytest`.
   **Scrub them first**: retailers echo the visitor's IP (`clientIP`, `ip`) and session/visitor ids
   into the page. Replace IPs with `203.0.113.10` and blank the ids before committing.
5. Resolve each `PROBE` comment; delete the comment once confirmed.

## Commands

```bash
uv sync --extra test          # .python-version pins 3.12
uv run pytest -q
uv run au-shopping-mcp --stdio
podman build -f Containerfile -t au-shopping-mcp .
```
