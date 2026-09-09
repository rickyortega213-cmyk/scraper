"""Apify Google Maps scraper actors (run-sync-get-dataset-items)."""

from __future__ import annotations

from typing import Iterator

from ...models import Place, QuerySpec
from ..base import MapsProvider, ProviderError

BASE = "https://api.apify.com/v2/acts"


class ApifyMaps(MapsProvider):
    name = "apify"

    def search(self, spec: QuerySpec, limit: int) -> Iterator[Place]:
        token = self.settings.apify_token or self.settings.maps_api_key
        if not token:
            raise ProviderError("APIFY_TOKEN is not set")
        actor = self.settings.apify_actor.replace("/", "~")
        url = f"{BASE}/{actor}/run-sync-get-dataset-items"
        payload = {
            "searchStringsArray": [spec.search_string],
            "maxCrawledPlacesPerSearch": limit,
            "language": self.settings.language,
            "skipClosedPlaces": True,
            "scrapePlaceDetailPage": False,
        }
        if spec.location:
            payload["locationQuery"] = spec.location
        data = self.client.post_json(
            url,
            params={"token": token, "timeout": 900},
            json=payload,
            timeout=900.0,
        )
        rows = data if isinstance(data, list) else data.get("items", [])
        seen = 0
        for raw in rows:
            place = self._place(raw, spec)
            if place is None:
                continue
            yield place
            seen += 1
            if seen >= limit:
                return
