"""Google Maps listings through an MCP server (e.g. https://mcp.scraper.tech/<key>).

The server describes its own tools, so this needs no endpoint or field
configuration: the maps-search tool is picked by name, the query / limit /
page arguments are matched against its input schema, and the listings are
located in whatever the tool returns.

Override the guesses when a server is unusual:
    MCP_MAPS_TOOL=google_maps_search
    MCP_MAPS_ARGS={"query": "{query}", "limit": "{limit}"}
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator, Optional

from ...models import Place, QuerySpec
from ..base import MapsProvider, ProviderError, place_from_mapping
from ..mcp import MCPClient, MCPError, MCPTool

log = logging.getLogger(__name__)

_TOOL_HINTS = ("maps", "places", "local", "business", "poi")
_SEARCH_HINTS = ("search", "find", "query", "lookup", "list")
_QUERY_PARAMS = ("query", "q", "search", "keyword", "keywords", "term", "text", "search_query")
_LIMIT_PARAMS = ("limit", "max_results", "maxresults", "num", "count", "results", "page_size", "per_page")
_PAGE_PARAMS = ("page", "page_number", "start", "offset")
_LOCATION_PARAMS = ("location", "city", "near", "area", "region_name", "place")


def pick_maps_tool(tools: list[MCPTool], preferred: str = "") -> Optional[MCPTool]:
    if preferred:
        return next((t for t in tools if t.name == preferred), None)
    scored: list[tuple[int, MCPTool]] = []
    for tool in tools:
        haystack = f"{tool.name} {tool.description}".lower()
        score = 0
        score += 5 * sum(1 for hint in _TOOL_HINTS if hint in haystack)
        score += 3 * sum(1 for hint in _SEARCH_HINTS if hint in tool.name.lower())
        if "google" in haystack:
            score += 2
        if any(p in tool.properties for p in _QUERY_PARAMS):
            score += 4
        if re.search(r"review|photo|detail|place_id|reverse|geocod", tool.name.lower()):
            score -= 6          # a detail/reviews tool is not the search
        if score > 0:
            scored.append((score, tool))
    scored.sort(key=lambda t: -t[0])
    return scored[0][1] if scored else None


def build_arguments(tool: MCPTool, spec: QuerySpec, limit: int, page: int,
                    template: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Arguments for the tool from its schema (or an explicit template)."""
    values = {"query": spec.search_string, "business_type": spec.business_type,
              "location": spec.location, "limit": limit, "page": page,
              "offset": (page - 1) * limit}
    if template:
        return {k: _fill(v, values) for k, v in template.items()}
    props = tool.properties
    args: dict[str, Any] = {}
    for name in _QUERY_PARAMS:
        if name in props:
            args[name] = spec.search_string
            break
    else:
        # No query field: maybe separate what/where fields.
        for name in ("business_type", "category", "type", "what"):
            if name in props:
                args[name] = spec.business_type
                break
        for name in _LOCATION_PARAMS:
            if name in props:
                args[name] = spec.location
                break
    for name in _LIMIT_PARAMS:
        if name in props:
            args[name] = limit
            break
    if page > 1:
        for name in _PAGE_PARAMS:
            if name in props:
                args[name] = (page - 1) * limit if name in ("start", "offset") else page
                break
    # Fill any other required field that has a sensible default in the schema.
    for name in tool.required:
        if name not in args and "default" in props.get(name, {}):
            args[name] = props[name]["default"]
    return args


def _fill(value: Any, values: dict[str, Any]) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in ("{limit}", "{page}", "{offset}"):
            return values[stripped[1:-1]]
        for key, val in values.items():
            value = value.replace("{" + key + "}", str(val))
        return value
    return value


class MCPMaps(MapsProvider):
    name = "mcp"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.url = settings.mcp_maps_url
        self._mcp: Optional[MCPClient] = None
        self._tool: Optional[MCPTool] = None

    def _connect(self) -> tuple[MCPClient, MCPTool]:
        if not self.url:
            raise ProviderError("MCP_MAPS_URL is not set (your scraper.tech MCP link)")
        if self._mcp is None:
            self._mcp = MCPClient(self.url, timeout=max(60.0, self.settings.http_timeout * 6))
            try:
                tools = self._mcp.list_tools()
            except MCPError as exc:
                raise ProviderError(f"MCP server at {_redact(self.url)}: {exc}") from exc
            tool = pick_maps_tool(tools, self.settings.mcp_maps_tool)
            if tool is None:
                names = ", ".join(t.name for t in tools) or "(none)"
                raise ProviderError(
                    f"no maps-search tool found on the MCP server; tools offered: {names}. "
                    "Set MCP_MAPS_TOOL to the right one."
                )
            self._tool = tool
            log.info("mcp maps tool: %s", tool.name)
        assert self._tool is not None
        return self._mcp, self._tool

    def search(self, spec: QuerySpec, limit: int) -> Iterator[Place]:
        mcp, tool = self._connect()
        template = None
        if self.settings.mcp_maps_args:
            try:
                template = json.loads(self.settings.mcp_maps_args)
            except ValueError as exc:
                raise ProviderError(f"MCP_MAPS_ARGS is not valid JSON: {exc}") from exc
        from ...probe import find_result_rows

        seen: set[str] = set()
        yielded = 0
        per_page = min(limit, 20)
        for page in range(1, self.settings.maps_max_pages + 1):
            args = build_arguments(tool, spec, per_page, page, template)
            try:
                payload = mcp.call_tool(tool.name, args)
            except MCPError as exc:
                if page == 1:
                    raise ProviderError(f"{tool.name}: {exc}") from exc
                return
            _, rows = find_result_rows(payload)
            if not rows and isinstance(payload, str):
                log.warning("mcp tool %s returned text, not listings: %s", tool.name, payload[:160])
            if not rows:
                return
            new = 0
            for raw in rows:
                place = place_from_mapping(raw, query=spec.search_string, source=self.name)
                if place is None:
                    continue
                key = place.dedupe_key()
                if key in seen:
                    continue
                seen.add(key)
                new += 1
                yield place
                yielded += 1
                if yielded >= limit:
                    return
            if new == 0 or not any(p in tool.properties for p in _PAGE_PARAMS) and not template:
                return

    def close(self) -> None:
        if self._mcp is not None:
            self._mcp.close()
        super().close()


def _redact(url: str) -> str:
    return re.sub(r"/([A-Za-z0-9_\-]{12,})(?=/|$)", "/…", url)
