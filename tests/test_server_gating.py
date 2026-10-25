"""Server boots, registers prefixed tools, and the bearer middleware gates HTTP."""
import asyncio
import json

import pytest
from mcp_server_kit import BearerAuthMiddleware


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import os
    os.environ["DATA_DIR"] = str(tmp_path_factory.mktemp("data"))
    os.environ["SITE"] = "bunnings"
    os.environ.pop("SITES", None)
    from au_shopping_mcp.core import config
    config.DATA_DIR = os.environ["DATA_DIR"]
    config.CACHE_DIR = os.path.join(config.DATA_DIR, "cache")
    from au_shopping_mcp import server
    return server


def test_tools_are_prefixed(server):
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    expected = {f"bunnings_{n}" for n in (
        "search_products", "get_product", "browse_category", "list_categories",
        "fetch_page", "probe_page", "site_status")}
    assert expected <= names


def test_site_status_shape(server):
    st = asyncio.run(server.sites["bunnings"].site_status())
    assert st["site"] == "bunnings" and st["fetch"]["current_tier"] == "HTTP"
    assert "sort_options" in st


def test_fetch_page_bad_mode_is_structured_error(server):
    out = asyncio.run(server.sites["bunnings"].fetch_page("/x", mode="nope"))
    assert out["error"] == "bad_argument"


async def _call(app, path, auth=None):
    headers = [(b"host", b"t")] + ([(b"authorization", auth.encode())] if auth else [])
    scope = {"type": "http", "method": "GET", "path": path, "headers": headers, "client": ("10.0.0.1", 1)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)
    return sent


def test_bearer_middleware(server):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = BearerAuthMiddleware(inner, "secret")  # the gate serve() puts in front of /mcp
    denied = asyncio.run(_call(app, "/mcp"))
    assert denied[0]["status"] == 401 and json.loads(denied[1]["body"])["error"] == "unauthorized"
    assert asyncio.run(_call(app, "/mcp", "Bearer wrong"))[0]["status"] == 401
    assert asyncio.run(_call(app, "/mcp", "Bearer secret"))[0]["status"] == 200
    assert asyncio.run(_call(app, "/.well-known/x"))[0]["status"] == 200


def test_every_tool_has_a_parameter(server):
    """Claude.ai's remote MCP client rejects tools whose input schema has no properties."""
    for t in asyncio.run(server.mcp.list_tools()):
        assert t.inputSchema.get("properties"), f"{t.name} has an empty input schema"


@pytest.mark.parametrize("url", [
    "https://evil.example/?d=secret",
    "http://10.1.2.3:3100/loki/api/v1/labels",
    "https://bunnings.com.au.evil.example/",
    "https://evilbunnings.com.au/",
    "//evil.example/x",
    "file:///etc/passwd",
    "https://user:pw@www.bunnings.com.au/",
])
def test_url_tools_refuse_offsite(server, url):
    for call in (server.sites["bunnings"].fetch_page(url), server.sites["bunnings"].probe_page(url), server.sites["bunnings"].get_product(url),
                 server.sites["bunnings"].browse_category(url)):
        out = asyncio.run(call)
        assert out["error"] == "offsite_url", (url, out)
    assert asyncio.run(server.sites["bunnings"].list_categories(url))[0]["error"] == "offsite_url"


@pytest.mark.parametrize("url", [
    "/products/tools", "0158411", "https://www.bunnings.com.au/x", "https://bunnings.com.au/x",
    "https://m.www.bunnings.com.au/x",
])
def test_onsite_urls_pass(server, url):
    assert server.sites["bunnings"].adapter.offsite_reason(url) is None


def test_every_adapter_base_url_is_onsite():
    from au_shopping_mcp.adapters import base
    import pkgutil, au_shopping_mcp.adapters as pkg
    for m in pkgutil.iter_modules(pkg.__path__):
        if m.name.startswith("_") or m.name == "base":
            continue
        a = base.load_adapter(m.name)
        assert a.offsite_reason(a.base_url + "/x") is None, m.name
