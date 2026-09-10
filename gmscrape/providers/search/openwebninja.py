"""OpenWeb Ninja Real-Time Web Search (https://www.openwebninja.com).

    GET https://api.openwebninja.com/realtime-web-search/search?q=...&limit=10
    x-api-key: <key>

Returns Google organic results plus, when Google shows one, the AI Overview as
structured text parts (`has_ai_overviews` at the top level), along with answer
box / knowledge panel data. The parser below is deliberately tolerant about
where those live so a schema revision degrades to "fewer signals", never to a
crash - and `gmscrape search "<query>"` prints the raw top-level keys so the
shape can be checked on a live call.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from ...util import dig, squeeze
from ..base import ProviderError
from .base import SearchHit, SearchResponse, WebSearchProvider

log = logging.getLogger(__name__)

ENDPOINT = "https://api.openwebninja.com/realtime-web-search/search"

ORGANIC_PATHS = (
    "data.organic_results", "data.organic", "data.results", "data.web_results",
    "organic_results", "results", "data",
)
AI_OVERVIEW_PATHS = ("data.ai_overview", "ai_overview", "data.ai_overviews", "ai_overviews")
ANSWER_PATHS = ("data.answer_box", "answer_box", "data.featured_snippet", "featured_snippet")
KNOWLEDGE_PATHS = ("data.knowledge_graph", "knowledge_graph", "data.knowledge_panel",
                   "knowledge_panel")
PAA_PATHS = ("data.people_also_ask", "people_also_ask", "data.related_questions")


def _text_of(node: Any) -> str:
    """Flatten whatever an AI-overview / answer node looks like into one string."""
    if node is None or isinstance(node, bool):
        return ""
    if isinstance(node, str):
        return squeeze(node)
    if isinstance(node, (int, float)):
        return str(node)
    if isinstance(node, list):
        return squeeze(" ".join(_text_of(item) for item in node))
    if isinstance(node, dict):
        parts: list[str] = []
        for key in ("text", "snippet", "answer", "content", "description", "title",
                    "text_parts", "parts", "blocks", "items", "list", "paragraph"):
            if key in node:
                parts.append(_text_of(node[key]))
        if not parts:
            parts = [_text_of(v) for k, v in node.items()
                     if k not in ("references", "links", "sources", "url", "thumbnail")]
        return squeeze(" ".join(p for p in parts if p))
    return ""


def _first_list(payload: Any, paths: Iterable[str]) -> list[Any]:
    for path in paths:
        value = dig(payload, path)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


def _first_node(payload: Any, paths: Iterable[str]) -> Any:
    for path in paths:
        value = dig(payload, path)
        if value not in (None, "", [], {}):
            return value
    return None


def parse_response(query: str, payload: Any) -> SearchResponse:
    response = SearchResponse(query=query, raw=payload if isinstance(payload, dict) else {})
    if not isinstance(payload, dict):
        response.error = "non-object response"
        return response
    status = str(payload.get("status") or "").upper()
    if status and status not in {"OK", "SUCCESS", "200"}:
        response.error = str(payload.get("error") or payload.get("message") or status)
        return response

    for index, raw in enumerate(_first_list(payload, ORGANIC_PATHS)):
        url = str(raw.get("url") or raw.get("link") or raw.get("href") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        response.hits.append(SearchHit(
            url=url,
            title=squeeze(str(raw.get("title") or "")),
            snippet=squeeze(str(raw.get("snippet") or raw.get("description") or "")),
            position=int(raw.get("position") or raw.get("rank") or index + 1),
        ))

    overview = _first_node(payload, AI_OVERVIEW_PATHS)
    if overview is not None:
        response.ai_overview = _text_of(overview)
    answer = _first_node(payload, ANSWER_PATHS)
    if answer is not None:
        response.answer = _text_of(answer)
    knowledge = _first_node(payload, KNOWLEDGE_PATHS)
    if isinstance(knowledge, dict):
        response.knowledge = _flatten_knowledge(knowledge)
    for item in _first_list(payload, PAA_PATHS):
        text = squeeze(f"{item.get('question', '')} {_text_of(item.get('answer') or item.get('snippet'))}")
        if text:
            response.extra_text.append(text)
    return response


def _flatten_knowledge(node: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Knowledge panels carry attributes like Founder / Owner / Founded - keep
    them as flat key -> text so the owner extractor can read them directly."""
    flat: dict[str, Any] = {}
    for key, value in node.items():
        name = f"{prefix}{key}".lower()
        if isinstance(value, dict):
            flat.update(_flatten_knowledge(value, f"{name}."))
        elif isinstance(value, list):
            texts = [_text_of(v) for v in value]
            flat[name] = "; ".join(t for t in texts if t)
        elif value not in (None, ""):
            flat[name] = value
    return flat


class OpenWebNinjaSearch(WebSearchProvider):
    name = "openwebninja"

    def search(self, query: str, limit: int = 10) -> SearchResponse:
        key = self.settings.openwebninja_key
        if not key:
            raise ProviderError("OPENWEBNINJA_KEY is not set")
        params: dict[str, Any] = {"q": query, "limit": max(1, min(limit, 20))}
        if self.settings.country:
            params["gl"] = self.settings.country
        if self.settings.language:
            params["hl"] = self.settings.language
        try:
            response = self.client.request(
                "GET", ENDPOINT, params=params, headers={"x-api-key": key}
            )
        except ProviderError as exc:
            return SearchResponse(query=query, error=str(exc))
        if response.status_code in (401, 403):
            raise ProviderError(
                f"OpenWeb Ninja rejected the key (HTTP {response.status_code}): "
                f"{response.text[:160]}"
            )
        if response.status_code >= 400:
            return SearchResponse(
                query=query, error=f"HTTP {response.status_code}: {response.text[:160]}"
            )
        try:
            payload = response.json()
        except ValueError:
            return SearchResponse(query=query, error="non-JSON response")
        parsed = parse_response(query, payload)
        if not parsed.hits and not parsed.ai_overview and not parsed.error:
            log.debug("openwebninja: no organic results for %r; keys=%s",
                      query, list(payload)[:10])
        return parsed
