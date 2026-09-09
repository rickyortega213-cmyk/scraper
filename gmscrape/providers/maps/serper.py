"""Serper.dev Maps API (https://serper.dev)."""

from __future__ import annotations

from typing import Iterator

from ...models import Place, QuerySpec
from ..base import MapsProvider, ProviderError

ENDPOINT = "https://google.serper.dev/maps"
PAGE_SIZE = 20


class SerperMaps(MapsProvider):
    name = "serper"

    def search(self, spec: QuerySpec, limit: int) -> Iterator[Place]:
        key = self.settings.serper_key or self.settings.maps_api_key
        if not key:
            raise ProviderError("SERPER_KEY is not set")
        headers = {"X-API-KEY": key, "Content-Type": "application/json"}
        seen = 0
        for page in range(1, self.settings.maps_max_pages + 1):
            payload = {
                "q": spec.search_string,
                "gl": self.settings.country,
                "hl": self.settings.language,
                "page": page,
            }
            data = self.client.post_json(ENDPOINT, json=payload, headers=headers)
            results = data.get("places") or []
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
