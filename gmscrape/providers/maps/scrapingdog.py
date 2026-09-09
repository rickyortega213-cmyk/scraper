"""ScrapingDog Google Maps API (https://docs.scrapingdog.com)."""

from __future__ import annotations

from typing import Iterator

from ...models import Place, QuerySpec
from ..base import MapsProvider, ProviderError

ENDPOINT = "https://api.scrapingdog.com/google_maps"
PAGE_SIZE = 20


class ScrapingDogMaps(MapsProvider):
    name = "scrapingdog"

    def search(self, spec: QuerySpec, limit: int) -> Iterator[Place]:
        key = self.settings.scrapingdog_key or self.settings.maps_api_key
        if not key:
            raise ProviderError("SCRAPINGDOG_KEY is not set")
        seen = 0
        for page in range(self.settings.maps_max_pages):
            params = {
                "api_key": key,
                "query": spec.search_string,
                "language": self.settings.language,
                "page": page,
            }
            data = self.client.get_json(ENDPOINT, params=params)
            results = data.get("search_results") or data.get("local_results") or []
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
            if len(results) < PAGE_SIZE:
                return
