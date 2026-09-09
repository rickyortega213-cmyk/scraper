"""ScraperAPI structured Google Maps endpoint.

    GET https://api.scraperapi.com/structured/google/mapssearch
        ?api_key=<KEY>&query=<QUERY>[&latitude=..&longitude=..]

The response is a JSON envelope whose listings live under `local_results`
(older/other variants use `results`, `places` or a bare array), and each
listing is normalized through the shared field mapper, so unusual field names
are picked up without bespoke parsing.

Paging is defensive: the endpoint is asked for successive pages, and the
provider stops as soon as a page returns nothing new. That way it collects
everything when `page` is honoured, and does not loop forever when it isn't.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from ...models import Place, QuerySpec
from ...util import dig
from ..base import MapsProvider, ProviderError

log = logging.getLogger(__name__)

ENDPOINT = "https://api.scraperapi.com/structured/google/mapssearch"
RESULT_PATHS = ("local_results", "results", "places", "local_results.places", "data")


class ScraperApiMaps(MapsProvider):
    name = "scraperapi"

    def search(self, spec: QuerySpec, limit: int) -> Iterator[Place]:
        key = self.settings.scraperapi_key or self.settings.maps_api_key
        if not key:
            raise ProviderError("SCRAPERAPI_KEY is not set")

        seen_keys: set[str] = set()
        yielded = 0
        for page in range(1, self.settings.maps_max_pages + 1):
            params: dict[str, Any] = {
                "api_key": key,
                "query": spec.search_string,
                "country_code": self.settings.country,
                "page": page,
            }
            data = self.client.get_json(ENDPOINT, params=params)
            rows = self._rows(data)
            if not rows:
                if page == 1:
                    log.warning(
                        "scraperapi returned no listings for %r; top-level keys were %s",
                        spec.search_string,
                        list(data)[:12] if isinstance(data, dict) else type(data).__name__,
                    )
                return

            new_on_page = 0
            for raw in rows:
                place = self._place(raw, spec)
                if place is None:
                    continue
                key_ = place.dedupe_key()
                if key_ in seen_keys:
                    continue
                seen_keys.add(key_)
                new_on_page += 1
                yield place
                yielded += 1
                if yielded >= limit:
                    return
            # The endpoint ignored `page` (or ran out of listings) - stop.
            if new_on_page == 0:
                return

    @staticmethod
    def _rows(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if not isinstance(data, dict):
            return []
        for message_key in ("error", "message", "detail"):
            message = data.get(message_key)
            if message and not any(dig(data, path) for path in RESULT_PATHS):
                raise ProviderError(f"scraperapi: {message}")
        for path in RESULT_PATHS:
            rows = dig(data, path)
            if isinstance(rows, dict):
                rows = [rows]
            if isinstance(rows, list) and rows:
                return [row for row in rows if isinstance(row, dict)]
        return []
