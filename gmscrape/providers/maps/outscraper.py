"""Outscraper Google Maps Search API (https://outscraper.com)."""

from __future__ import annotations

import time
from typing import Any, Iterator

from ...models import Place, QuerySpec
from ..base import MapsProvider, ProviderError

ENDPOINT = "https://api.app.outscraper.com/maps/search-v3"
POLL_INTERVAL = 5.0
POLL_MAX_WAIT = 600.0


class OutscraperMaps(MapsProvider):
    name = "outscraper"

    def search(self, spec: QuerySpec, limit: int) -> Iterator[Place]:
        key = self.settings.outscraper_key or self.settings.maps_api_key
        if not key:
            raise ProviderError("OUTSCRAPER_KEY is not set")
        params = {
            "query": spec.search_string,
            "limit": limit,
            "language": self.settings.language,
            "region": self.settings.country.upper(),
            "async": "false",
        }
        data = self.client.get_json(ENDPOINT, params=params, headers={"X-API-KEY": key})
        rows = self._extract_rows(data, key)
        seen = 0
        for raw in rows:
            place = self._place(raw, spec)
            if place is None:
                continue
            yield place
            seen += 1
            if seen >= limit:
                return

    def _extract_rows(self, data: Any, key: str) -> list[dict[str, Any]]:
        """Outscraper returns either inline data or a results_location to poll."""
        if isinstance(data, dict) and data.get("results_location") and not data.get("data"):
            data = self._poll(str(data["results_location"]), key)
        payload = data.get("data") if isinstance(data, dict) else data
        rows: list[dict[str, Any]] = []
        for item in payload or []:
            if isinstance(item, list):       # one nested list per query
                rows.extend(x for x in item if isinstance(x, dict))
            elif isinstance(item, dict):
                rows.append(item)
        return rows

    def _poll(self, location: str, key: str) -> Any:
        deadline = time.time() + POLL_MAX_WAIT
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            data = self.client.get_json(location, headers={"X-API-KEY": key})
            status = str(data.get("status", "")).lower()
            if status in {"success", "finished", "completed"}:
                return data
            if status in {"error", "failed", "canceled", "cancelled"}:
                raise ProviderError(f"outscraper job failed: {data}")
        raise ProviderError("outscraper job timed out")
