# au-shopping-mcp

MCP server for shopping at Australian online stores: product search, full product detail,
category browsing and price comparison across 26 sites, from Bunnings, JB Hi-Fi, Officeworks and
Costco to Chemist Warehouse and Supercheap Auto, plus `staticice` (an Australian price-comparison
site) and `aliexpress` (the one global marketplace). One process can serve every site
(`SITES=all`) or a chosen few (`SITES=bunnings,costco`); tools get the site's prefix
(`bunnings_search_products`, …), so tool names are the same however the sites are grouped.

It is polite by design: per-site rate limits and caching, public pages only, and member logins
only through a real browser window you sign in to yourself (`au-shopping-mcp login <site>`).
Retailers change their pages; when an adapter breaks, `<site>_probe_page` shows what moved.
Check each retailer's terms before relying on it.

## Install

```bash
uvx au-shopping-mcp --stdio                 # or: pip install au-shopping-mcp
uvx --from au-shopping-mcp playwright install chromium   # only for sites that need the browser tier
```

Claude Desktop / Claude Code (stdio):

```json
{"mcpServers": {"au-shopping": {"command": "uvx", "args": ["au-shopping-mcp", "--stdio"],
  "env": {"SITES": "all"}}}}
```

## Tools (per site)

| Tool | What it does |
|---|---|
| `<site>_search_products` | Keyword search → shortlist (name, brand, price, rating, id, URL), paginated |
| `<site>_get_product` | Full detail for one item by id or URL, incl. specs and site-specific extras |
| `<site>_browse_category` | Products in a category path/URL, same shape as search |
| `<site>_list_categories` | Category names and paths |
| `<site>_fetch_page` | Escape hatch: readable text + links (or raw HTML) for any page on the site |
| `<site>_probe_page` | Diagnostic: which structured-data sources a page exposes |
| `<site>_site_status` | Fetch tier, last block, request/cache counters, config |

URL-taking tools (`get_product`, `browse_category`, `list_categories`, `fetch_page`, `probe_page`) accept ids, site-relative paths, or absolute URLs on the adapter's own `domain` (and subdomains) only; any other host returns `{"error": "offsite_url"}`. This stops a prompt-injected agent from using the scraper as a generic fetcher (data exfiltration via query strings, SSRF into the LAN).

All read-only. Errors come back as `{"error": ..., "reason": ..., "guidance": ...}`.

## Architecture

```
src/au_shopping_mcp/
  server.py             generic tool surface, bearer auth, request log
  adapters/base.py      SiteAdapter contract - server.py only talks to this
  adapters/bunnings.py  Bunnings: URLs, endpoints, extraction order, store scoping
  adapters/_template.py copy this for a new site
  core/fetch.py         escalation ladder HTTP -> BROWSER (Playwright) -> STEALTH (persistent profile)
  core/extract.py       JSON-LD, __NEXT_DATA__, window state, selectors, links, readable text
  core/politeness.py    per-domain rate limiter + diskcache
  core/config.py        env settings, JSON logs to stderr (Loki-friendly)
  healthcheck.py        loopback liveness probe
```

**Fetch ladder.** Each page starts at the lowest tier that last worked for the domain. A block
(403/429/503, Cloudflare markers, `cf-mitigated`, or a JS shell missing `browser_wait_selector`)
escalates one tier and retries. After 30 quiet minutes the tier decays back down. `MAX_TIER` caps it
(`MAX_TIER=1` = httpx only, no Chromium). Cookies and the stealth browser profile persist in
`DATA_DIR` so a Cloudflare clearance survives restarts.

**Extraction order.** Hydration state → JSON-LD → CSS selectors. Selectors live in one dict per
adapter. **Tier 0** (a site's own JSON endpoint) is `fetcher.fetch_json()`; an adapter opts into it
once you've found the endpoint in DevTools.

**Bunnings (verified 2026-09-06).** Every page is a fully server-rendered Next.js page whose
`__NEXT_DATA__` carries a react-query cache (`props.pageProps.dehydratedState.queries`). Listings
come from the `coveo-search` / `category-search` entry (`results[].raw` = Coveo product document,
`totalCount`, 36 per page); product pages from `retail-product` (catalogue), `product-retail-price`
(store price), `product-fulfilment` (store stock, click & collect, delivery) and
`product-aisle-item-location`. Bunnings emits no JSON-LD. Plain HTTP (tier 1) is enough — the
Chromium tiers are never reached in normal use.

**AliExpress (verified 2026-09-21).** Listings are server-rendered, but not into any standard
container: the whole result set is a JS object literal `window._dida_config_._init_data_=
{ data: {...} }` whose `data:` key is unquoted, so it is brace-matched with
`json.JSONDecoder().raw_decode` (`extract.window_state` can't read it). Items are
`data.root.fields.mods.itemList.content[]` (60/page) with `pageInfo.totalResults` and
`searchRefineFilters` for facets; search URL is `/w/wholesale-<query>.html?page=&SortType=`
(`SortType` ∈ `total_tranpro_desc`, `price_asc`, `price_desc`), with `selectedSwitches`,
`shpf_co` and `pr` for filters. Product pages are client-rendered (`window.runParams` is empty,
no JSON-LD), so `get_product` renders the page and extracts from the `data-pl` anchors
(`product-title`, `product-reviewer`, `product-specs`) plus module class *prefixes*
(`price-default--current--<hash>` — the hash rotates on every deploy, the prefix doesn't).
**Currency, ship-to region and language are the `aep_usuc_f` cookie**
(`ALIEXPRESS_CURRENCY`/`_REGION`/`_LOCALE`, default `AUD`/`AU`/`en_US`); without it the site
prices in whatever the exit IP geolocates to. `aliexpress_site_status.locale.effective_currency`
shows what the site actually applied. `list_categories` is unsupported on purpose (the legacy
`/category/<id>` pages return zero results) — `browse_category` takes a listing URL instead.

**Cutter Electronics (verified 2026-09-21).** WooCommerce with the **Store API left open**, so
this is a tier-0 JSON adapter with no HTML parsing: `/wp-json/wc/store/v1/products?search=&page=`
for listings, `products/<id>` (or `?slug=`) for detail, `products/categories` for the tree.
Two traps: paging totals are in the `x-wp-total` / `x-wp-totalpages` **headers**, not the body,
and prices are integer **minor units** scaled by `prices.currency_minor_unit` (`"5295"` → $52.95).
`orderby` is a fixed WooCommerce enum — anything else is a 400. Category filtering needs the
numeric term id and the categories endpoint ignores `?slug=`, so slugs/names are resolved against
the cached full term list. WooCommerce search is literal (title/SKU), so a generic word can return
nothing while the matching category is full — `browse_category` accepts a category name.

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `SITES` | *(unset)* | Comma-separated adapter modules under `adapters/`, or `all`. One process serves them all, sharing one Playwright browser and stealth profile |
| `SITE` | `bunnings` | Single adapter, used only when `SITES` is unset (the older one-process-per-site form) |
| `ALIEXPRESS_CURRENCY` / `ALIEXPRESS_REGION` / `ALIEXPRESS_LOCALE` | `AUD` / `AU` / `en_US` | AliExpress pricing currency, ship-to region and language (the `aep_usuc_f` cookie) |
| `PORT` | `8347` | Streamable-HTTP listen port |
| `MCP_BEARER_TOKEN` | *(required for HTTP)* | Bearer token; server refuses to start without it |
| `DATA_DIR` | `~/.local/state/au-shopping-mcp` (`/data` in container) | Cache, cookie jars, browser profile |
| `MAX_TIER` | `3` | Ceiling on the fetch ladder |
| `LOG_LEVEL` | `INFO` | |
| `BUNNINGS_STORE_ID` | *(unset)* | 4-digit store code to scope prices/stock to, e.g. `7313` (Alexandria NSW). It's the `name` field on `bunnings.com.au/stores/<state>/<suburb>` |
| `BUNNINGS_REGION` | *(unset)* | Pricing region for that store, e.g. `NSWMetro`. **Required together with the store id** — the site ignores a bare `storeId` cookie and falls back to its default (6400 Mentone, VICMetro). `<site>_site_status` shows the store the site actually applied |

## Running

```bash
uv sync --extra test          # Python 3.12 or 3.13
uv run playwright install chromium   # only needed if the fetch ladder ever reaches tiers 2-3
uv run pytest -q
SITES=all uv run au-shopping-mcp --stdio                             # local, stdio, every site
SITE=bunnings uv run au-shopping-mcp --stdio                          # one site only
MCP_BEARER_TOKEN=… SITES=all uv run au-shopping-mcp                   # HTTP on :8347
```

Client registration (stdio, Claude Desktop / Claude Code): one server, `uv run --env-file .env
au-shopping-mcp --stdio`, with the env file holding `SITES=all` plus the site knobs (`BUNNINGS_STORE_ID`, `BUNNINGS_REGION`,
`ALIEXPRESS_*`). Knob names are site-prefixed, so they never clash. Registering one server per
site (`SITE=<site>` in each env file) still works, but costs a process per site per client
session.

Container: build from `Containerfile`; run with `--shm-size=512m` and a volume on `/data`.
MCP clients cache tool schemas, so restart them after an upgrade that changes tools.

## Bunnings: what was verified (2026-09-06)

The original `PROBE` guesses were resolved against the live site:

- Search URL `/search/products?q=<q>&page=<n>&sort=<Key>`; category `/products/<path>?page=<n>&sort=<Key>`.
  Sort keys: `BoostOrder` (relevance, default), `PriceAscending`, `PriceDescending`, `RatingDescending`,
  `RatingAscending`, `NameAscending`, `NameDescending` — exposed as `relevance`, `price_asc`, `price_desc`,
  `rating_desc`, `rating_asc`, `name`, `name_desc`.
- Product URL `/<slug>_p<itemNumber>`; `/_p<itemNumber>` returns 200 with the canonical page.
- Store scoping = cookies `storeId` + `regionCode` (both). Set via `BUNNINGS_STORE_ID` / `BUNNINGS_REGION`.
  The fetcher puts them in the cookie jar / browser context (a raw `Cookie:` header gets clobbered by httpx's jar).
- Zero-result searches return an empty `items` list (not an error); a page with no result set at all is an
  `extraction` error pointing at `probe_page`.
- `tests/fixtures/bunnings/{search,product}.html` are real saved pages; `tests/test_bunnings_hydration.py`
  covers the parsing with synthetic pages of the same shape. Re-save the fixtures when the site changes.

If the site changes, run `bunnings_probe_page` on a search and a product URL and compare
`next_data.page_props_keys` and the react-query keys against the list above.

## Adding a site

Copy `adapters/_template.py` to `adapters/<site>.py`, fill the `PROBE` items using
`<site>_probe_page`, run it alone with `SITE=<site>` (or add it to `SITES`), save fixtures, write tests. With
`SITES=all` a new adapter appears in every client after its next restart.
Less friendly sites: raise `start_tier` to `BROWSER`, lengthen `min_delay_s`, and consider a
FlareSolverr sidecar as a tier-4 hook in `fetch.py` if STEALTH isn't enough.

## Notes

- Defaults are ~1 req/s, concurrency 2, everything cached. This is for personal lookups, not
  bulk crawling — check a site's terms before scaling any of that up.
