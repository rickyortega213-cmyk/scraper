"""Config-driven Google Maps provider.

Point GENERIC_MAPS_CONFIG at a small JSON file describing any REST API and it
becomes a first-class provider - no code changes needed. Example:

    {
      "name": "myapi",
      "method": "GET",
      "url": "https://api.example.com/v1/places/search",
      "headers": {"Authorization": "Bearer {api_key}"},
      "query": {"q": "{query}", "limit": "{page_size}", "page": "{page}"},
      "results_path": "data.results",
      "pagination": {"style": "page", "param": "page", "start": 1, "size": 20},
      "field_map": {"name": "businessName", "website": ["web.url", "site"]},
      "total_path": "data.total"
    }

Placeholders usable anywhere in url/headers/query/body:
  {api_key} {query} {business_type} {location} {limit} {page} {offset}
  {page_size} {language} {country}

Pagination styles: "page" (increment a page number), "offset" (page * size),
"cursor" (follow `cursor_path` into the next request) or "none".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Optional

from ...models import Place, QuerySpec
from ...util import dig
from ..base import MapsProvider, ProviderError, place_from_mapping


def _substitute(node: Any, values: dict[str, Any]) -> Any:
    """Recursively format {placeholders} inside strings of a JSON structure."""
    if isinstance(node, str):
        out = node
        for key, value in values.items():
            token = "{" + key + "}"
            if token in out:
                out = out.replace(token, "" if value is None else str(value))
        return out
    if isinstance(node, dict):
        return {k: _substitute(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, values) for v in node]
    return node


def _drop_empty(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v not in (None, "", "None")}


class GenericMaps(MapsProvider):
    """Any REST maps API, described by a JSON config file."""

    name = "generic"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.config = self._load_config(settings.generic_maps_config)
        self.name = str(self.config.get("name") or "generic")

    @staticmethod
    def _load_config(path: str) -> dict[str, Any]:
        if not path:
            raise ProviderError(
                "GENERIC_MAPS_CONFIG is not set - point it at a JSON file "
                "describing your maps API (see examples/maps_api.example.json)"
            )
        p = Path(path).expanduser()
        if not p.exists():
            raise ProviderError(f"generic maps config not found: {p}")
        try:
            config = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ProviderError(f"invalid JSON in {p}: {exc}") from exc
        if not config.get("url"):
            raise ProviderError(f"{p} must define a 'url'")
        return config

    def search(self, spec: QuerySpec, limit: int) -> Iterator[Place]:
        cfg = self.config
        api_key = (
            cfg.get("api_key")
            or self.settings.maps_api_key
            or self.settings.extra.get("maps_api_key", "")
        )
        pagination = cfg.get("pagination") or {}
        style = str(pagination.get("style", "page")).lower()
        page_size = int(pagination.get("size") or min(limit, 20) or 20)
        start = int(pagination.get("start", 1 if style == "page" else 0))
        max_pages = 1 if style == "none" else self.settings.maps_max_pages

        method = str(cfg.get("method", "GET")).upper()
        results_path = str(cfg.get("results_path", ""))
        field_map = cfg.get("field_map") or None
        cursor_path = cfg.get("cursor_path", "")
        cursor_param = cfg.get("cursor_param", "cursor")
        cursor: Optional[str] = None
        seen = 0

        for index in range(max_pages):
            page_number = start + index
            values = {
                "api_key": api_key,
                "query": spec.search_string,
                "business_type": spec.business_type,
                "location": spec.location,
                "limit": limit,
                "page": page_number,
                "offset": index * page_size,
                "page_size": page_size,
                "language": self.settings.language,
                "country": self.settings.country,
                "cursor": cursor or "",
            }
            url = _substitute(cfg["url"], values)
            headers = _drop_empty(_substitute(cfg.get("headers") or {}, values))
            params = _drop_empty(_substitute(cfg.get("query") or {}, values))
            body = _substitute(cfg.get("body"), values) if cfg.get("body") else None
            if style == "cursor":
                if cursor:
                    params[cursor_param] = cursor
                elif index > 0:
                    return

            kwargs: dict[str, Any] = {"headers": headers, "params": params}
            if body is not None:
                kwargs["json"] = body
            data = (
                self.client.get_json(url, **kwargs)
                if method == "GET"
                else self.client.post_json(url, **kwargs)
            )

            error = cfg.get("error_path") and dig(data, str(cfg["error_path"]))
            if error:
                raise ProviderError(f"{self.name}: {error}")

            rows = dig(data, results_path) if results_path else data
            if isinstance(rows, dict):
                rows = [rows]
            if not rows:
                return
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                place = place_from_mapping(
                    raw, query=spec.search_string, source=self.name, field_map=field_map
                )
                if place is None:
                    continue
                yield place
                seen += 1
                if seen >= limit:
                    return

            if style == "cursor":
                cursor = dig(data, str(cursor_path)) if cursor_path else None
                if not cursor:
                    return
            elif len(rows) < page_size:
                return
