"""SerpApi google_maps engine (https://serpapi.com/google-maps-api)."""

from __future__ import annotations

from typing import Iterator

from ...models import Place, QuerySpec
from ..base import MapsProvider, ProviderError

ENDPOINT = "https://serpapi.com/search.json"
PAGE_SIZE = 20


class SerpApiMaps(MapsProvider):
    name = "serpapi"

    def search(self, spec: QuerySpec, limit: int) -> Iterator[Place]:
        key = self.settings.serpapi_key or self.settings.maps_api_key
        if not key:
            raise ProviderError("SERPAPI_KEY is not set")
        seen = 0
        for page in range(self.settings.maps_max_pages):
            params = {
                "engine": "google_maps",
                "type": "search",
                "q": spec.search_string,
                "api_key": key,
                "hl": self.settings.language,
                "start": page * PAGE_SIZE,
            }
            data = self.client.get_json(ENDPOINT, params=params)
            if data.get("error"):
                if page == 0:
                    raise ProviderError(f"serpapi: {data['error']}")
                return
            results = data.get("local_results") or data.get("place_results") or []
            if isinstance(results, dict):
                results = [results]
            if not results:
                return
            for raw in results:
                place = self._place(raw, spec)
                if place is None:
                    continue
                yield place
                seen += 1
                if seen >= limit:
                    return
