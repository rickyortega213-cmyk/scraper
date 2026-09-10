"""MCP maps provider against a fake MCP server (JSON and SSE replies)."""

from __future__ import annotations

import http.server
import json
import socket
import threading

import pytest

from gmscrape.config import Settings
from gmscrape.providers.base import ProviderError
from gmscrape.providers.maps.mcp_provider import MCPMaps, build_arguments, pick_maps_tool
from gmscrape.providers.mcp import MCPClient, MCPError, MCPTool
from gmscrape.query import parse_query

TOOLS = [
    {"name": "google_maps_reviews", "description": "Reviews for a place_id",
     "inputSchema": {"type": "object", "properties": {"place_id": {"type": "string"}}, "required": ["place_id"]}},
    {"name": "google_maps_search", "description": "Search Google Maps businesses",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"}, "limit": {"type": "integer"},
                                    "page": {"type": "integer"}, "lang": {"type": "string", "default": "en"}},
                     "required": ["query", "lang"]}},
]
LISTINGS = [
    {"title": "Austin Family Dental", "website": "austinfamilydental.com", "phone": "(512) 555-0142",
     "address": "220 Oak Ave, Austin, TX 78702", "rating": 4.6, "reviews": 412, "place_id": "m1"},
    {"title": "Smile Studio ATX", "website": "smilestudioatx.com", "phone": "(512) 555-0188",
     "address": "9 Elm St, Austin, TX 78701", "rating": 4.9, "reviews": 88, "place_id": "m2"},
]


class _MCP(http.server.BaseHTTPRequestHandler):
    sse = False
    calls: list[dict] = []
    pages: dict[int, list] = {1: LISTINGS}
    session = "sess-1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        message = json.loads(self.rfile.read(length))
        method = message.get("method")
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "fake-scraper-tech", "version": "1"},
                      "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            assert self.headers.get("Mcp-Session-Id") == self.session, "session id must be echoed back"
            self.calls.append(message["params"])
            page = int(message["params"]["arguments"].get("page", 1))
            rows = self.pages.get(page, [])
            result = {"content": [{"type": "text", "text": json.dumps({"status": "ok", "data": rows})}]}
        else:
            result = {}
        reply = {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
        if self.sse:
            body = f"event: message\ndata: {json.dumps(reply)}\n\n".encode()
            ctype = "text/event-stream"
        else:
            body = json.dumps(reply).encode()
            ctype = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Mcp-Session-Id", self.session)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture(params=[False, True], ids=["json", "sse"])
def mcp_url(request):
    _MCP.sse = request.param
    _MCP.calls = []
    _MCP.pages = {1: LISTINGS}
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _MCP)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}/a25e-fake-key"
    finally:
        server.shutdown()
        server.server_close()


def test_client_lists_tools_and_calls_one(mcp_url):
    client = MCPClient(mcp_url)
    assert client.initialize()["name"] == "fake-scraper-tech"
    tools = client.list_tools()
    assert [t.name for t in tools] == ["google_maps_reviews", "google_maps_search"]
    payload = client.call_tool("google_maps_search", {"query": "x", "lang": "en"})
    assert payload["data"][0]["title"] == "Austin Family Dental"
    client.close()


def test_picks_the_search_tool_not_the_reviews_tool():
    tools = [MCPTool(t["name"], t["description"], t["inputSchema"]) for t in TOOLS]
    assert pick_maps_tool(tools).name == "google_maps_search"
    assert pick_maps_tool(tools, "google_maps_reviews").name == "google_maps_reviews"
    assert pick_maps_tool([]) is None


def test_arguments_come_from_the_schema():
    tool = MCPTool(**{"name": "google_maps_search", "description": "", "input_schema": TOOLS[1]["inputSchema"]})
    args = build_arguments(tool, parse_query("dentist in austin tx"), limit=20, page=1)
    assert args == {"query": "dentist in austin tx", "limit": 20, "lang": "en"}   # required default filled
    assert build_arguments(tool, parse_query("x in y"), 20, 3)["page"] == 3
    template = {"q": "{query}", "max": "{limit}", "city": "{location}"}
    assert build_arguments(tool, parse_query("dentist in austin tx"), 20, 1, template) == {
        "q": "dentist in austin tx", "max": 20, "city": "austin tx"}


def test_provider_returns_places_and_paginates(mcp_url):
    _MCP.pages = {1: LISTINGS, 2: [{"title": "Third Dental", "place_id": "m3"}], 3: []}
    settings = Settings.from_env(mcp_maps_url=mcp_url, maps_max_pages=5)
    provider = MCPMaps(settings)
    places = list(provider.search(parse_query("dentist in austin tx"), limit=10))
    provider.close()
    assert [p.name for p in places] == ["Austin Family Dental", "Smile Studio ATX", "Third Dental"]
    assert places[0].domain == "austinfamilydental.com" and places[0].reviews == 412
    assert places[0].source == "mcp" and places[0].query == "dentist in austin tx"
    assert [c["arguments"].get("page", 1) for c in _MCP.calls] == [1, 2, 3]
    assert all(c["name"] == "google_maps_search" for c in _MCP.calls)


def test_provider_honours_limit(mcp_url):
    provider = MCPMaps(Settings.from_env(mcp_maps_url=mcp_url))
    assert len(list(provider.search(parse_query("dentist in austin tx"), limit=1))) == 1
    provider.close()


def test_missing_url_and_unreachable_server_are_clear():
    with pytest.raises(ProviderError, match="MCP_MAPS_URL"):
        list(MCPMaps(Settings.from_env(mcp_maps_url="")).search(parse_query("x in y"), 5))
    provider = MCPMaps(Settings.from_env(mcp_maps_url="http://127.0.0.1:1/key", http_timeout=1))
    with pytest.raises((ProviderError, MCPError, Exception)):
        list(provider.search(parse_query("x in y"), 5))


def test_mcp_url_is_the_auto_selected_maps_provider():
    from gmscrape.providers.registry import detect_maps_provider

    assert detect_maps_provider(Settings.from_env(mcp_maps_url="https://mcp.x/k", serpapi_key="s")) == "mcp"
