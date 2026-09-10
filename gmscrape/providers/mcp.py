"""Minimal MCP (Model Context Protocol) client over Streamable HTTP.

Enough to talk to a hosted MCP server such as https://mcp.scraper.tech/<key>:
initialize, list the tools, call one. Responses may arrive as plain JSON or as
an SSE stream (`text/event-stream`); both are handled.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"


class MCPError(RuntimeError):
    pass


@dataclass
class MCPTool:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def properties(self) -> dict[str, Any]:
        return dict(self.input_schema.get("properties") or {})

    @property
    def required(self) -> list[str]:
        return list(self.input_schema.get("required") or [])


class MCPClient:
    def __init__(self, url: str, *, timeout: float = 120.0, client: Optional[httpx.Client] = None) -> None:
        self.url = url
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._session_id: str = ""
        self._next_id = 0
        self._initialized = False
        self.server_info: dict[str, Any] = {}

    # --- transport -----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 522, 524}

    def _post(self, payload: dict[str, Any], *, expect_result: bool = True) -> Any:
        import random
        import time

        last_error: Optional[Exception] = None
        response = None
        for attempt in range(4):
            try:
                response = self._client.post(self.url, json=payload, headers=self._headers())
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                response = None
            if response is not None and response.status_code not in self.RETRY_STATUS:
                break
            if attempt < 3:
                retry_after = response.headers.get("Retry-After") if response is not None else None
                try:
                    delay = min(30.0, float(retry_after)) if retry_after else 1.5 * (2 ** attempt)
                except ValueError:
                    delay = 1.5 * (2 ** attempt)
                time.sleep(delay + random.uniform(0, 0.4))
        if response is None:
            raise MCPError(f"MCP server unreachable: {last_error}")
        session = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
        if session:
            self._session_id = session
        if response.status_code >= 400:
            raise MCPError(f"MCP server returned HTTP {response.status_code}: {response.text[:300]}")
        if not expect_result:
            return None
        message = self._parse_body(response, payload.get("id"))
        if message is None:
            raise MCPError(f"no JSON-RPC response for {payload.get('method')}: {response.text[:200]}")
        if "error" in message:
            error = message["error"]
            raise MCPError(f"{payload.get('method')} failed: {error.get('message', error)}")
        return message.get("result")

    @staticmethod
    def _parse_body(response: httpx.Response, request_id: Any) -> Optional[dict[str, Any]]:
        content_type = response.headers.get("content-type", "")
        text = response.text or ""
        if "text/event-stream" in content_type or text.lstrip().startswith(("event:", "data:")):
            matched: Optional[dict[str, Any]] = None
            for line in text.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    message = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == request_id:
                    matched = message
                elif isinstance(message, dict) and matched is None and "result" in message:
                    matched = message
            return matched
        try:
            message = response.json()
        except ValueError:
            return None
        if isinstance(message, list):
            message = next((m for m in message if m.get("id") == request_id), message[0] if message else None)
        return message if isinstance(message, dict) else None

    def _request(self, method: str, params: Optional[dict[str, Any]] = None) -> Any:
        self._next_id += 1
        return self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method,
                           "params": params or {}})

    # --- protocol --------------------------------------------------------------
    def initialize(self) -> dict[str, Any]:
        if self._initialized:
            return self.server_info
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "gmscrape", "version": "0.1"},
        }) or {}
        self.server_info = result.get("serverInfo") or {}
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_result=False)
        except MCPError:
            pass          # some servers answer notifications with 4xx; harmless
        self._initialized = True
        return self.server_info

    def list_tools(self) -> list[MCPTool]:
        self.initialize()
        tools: list[MCPTool] = []
        cursor: Optional[str] = None
        while True:
            result = self._request("tools/list", {"cursor": cursor} if cursor else {}) or {}
            for raw in result.get("tools") or []:
                tools.append(MCPTool(
                    name=str(raw.get("name") or ""),
                    description=str(raw.get("description") or ""),
                    input_schema=raw.get("inputSchema") or raw.get("input_schema") or {},
                ))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool; returns its structured result (parsed JSON when text)."""
        self.initialize()
        result = self._request("tools/call", {"name": name, "arguments": arguments}) or {}
        if result.get("isError"):
            raise MCPError(f"tool {name} reported an error: {_text_of(result)[:300]}")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        text = _text_of(result)
        try:
            return json.loads(text)
        except ValueError:
            return text

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _text_of(result: dict[str, Any]) -> str:
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(parts)
