"""One process serving several sites: same per-site tools, shared browsers, SITES parsing."""
import asyncio

import pytest

from au_shopping_mcp.adapters.base import available_adapters, load_adapter, load_adapters


def _tools(mcp):
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


@pytest.fixture(scope="module")
def server():
    from au_shopping_mcp import server
    return server


def test_multisite_tools_match_single_site_builds(server):
    """Merging sites must not change any tool's name, description or schema."""
    names = ["bunnings", "costco", "cutter"]
    multi, sites = server.build_server([load_adapter(n) for n in names])
    merged = _tools(multi)
    assert set(sites) == set(names)
    expected = {}
    for n in names:
        single, _ = server.build_server([load_adapter(n)])
        expected.update(_tools(single))
    assert merged.keys() == expected.keys()
    for name, t in expected.items():
        m = merged[name]
        assert (m.description, m.inputSchema, m.annotations) == (t.description, t.inputSchema, t.annotations), name


def test_descriptions_keep_the_published_indent(server):
    """Clients cached descriptions with 4-space continuation lines; nesting must not add more."""
    for t in _tools(server.build_server([load_adapter("bunnings")])[0]).values():
        assert "\n    " in t.description and "\n     " not in t.description, t.name


def test_multisite_server_name_and_instructions(server):
    adapters = [load_adapter(n) for n in ("bunnings", "staticice")]
    multi, _ = server.build_server(adapters)
    assert multi.name == "au-shopping"
    for a in adapters:
        assert f"- {a.name}: {a.description}" in multi.instructions
    single, _ = server.build_server(adapters[:1])
    assert single.name == "bunnings" and single.instructions.startswith(adapters[0].description)


def test_all_sites_load_and_every_tool_is_unique(server):
    multi, sites = server.build_server(load_adapters("all", ""))
    assert set(sites) == set(available_adapters())
    assert len(_tools(multi)) == 7 * len(sites)


def test_tools_call_their_own_site(server):
    _, sites = server.build_server([load_adapter("bunnings"), load_adapter("cutter")])
    assert asyncio.run(sites["bunnings"].site_status())["site"] == "bunnings"
    assert asyncio.run(sites["cutter"].site_status())["site"] == "cutter"
    # each site's URL guard is its own: a bunnings URL is off-site for cutter
    out = asyncio.run(sites["cutter"].fetch_page("https://www.bunnings.com.au/x"))
    assert out["error"] == "offsite_url"


def test_load_adapters_parsing():
    assert [a.name for a in load_adapters("", "costco")] == ["costco"]
    assert [a.name for a in load_adapters(" bunnings, costco ,bunnings", "x")] == ["bunnings", "costco"]
    with pytest.raises(SystemExit, match="nosuchshop"):
        load_adapters("bunnings,nosuchshop", "")


def test_fetchers_share_one_browser_pool():
    from au_shopping_mcp.core import fetch
    a, b = load_adapter("jbhifi"), load_adapter("scorptec")
    assert a.fetcher is not b.fetcher
    assert not hasattr(a.fetcher, "_stealth_ctx") and not hasattr(b.fetcher, "_browser")
    assert fetch._BROWSERS.lock is not None
