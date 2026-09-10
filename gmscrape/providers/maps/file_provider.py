"""Read places from a local JSON / JSONL / CSV file.

Useful when you run the Maps API yourself (or want to re-run the email stage
against a saved list) - `--maps-provider file --places-file leads.csv`.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterator

from ...models import Place, QuerySpec
from ...util import dig
from ..base import MapsProvider, ProviderError, place_from_mapping


class FileMaps(MapsProvider):
    name = "file"
    requires_key = False

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self._rows: list[dict[str, Any]] | None = None

    def _load(self) -> list[dict[str, Any]]:
        if self._rows is not None:
            return self._rows
        path = self.settings.places_file
        if not path:
            raise ProviderError("PLACES_FILE is not set (use --places-file)")
        p = Path(path).expanduser()
        if not p.exists():
            raise ProviderError(f"places file not found: {p}")

        rows: list[dict[str, Any]] = []
        text = p.read_text(encoding="utf-8", errors="replace")
        if p.suffix.lower() in {".csv", ".tsv"}:
            delimiter = "\t" if p.suffix.lower() == ".tsv" else ","
            rows = list(csv.DictReader(text.splitlines(), delimiter=delimiter))
        elif p.suffix.lower() == ".jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            data = json.loads(text)
            if isinstance(data, dict):
                for key in ("results", "places", "data", "items"):
                    found = dig(data, key)
                    if isinstance(found, list):
                        data = found
                        break
            rows = [r for r in (data or []) if isinstance(r, dict)]
        self._rows = rows
        return rows

    def search(self, spec: QuerySpec, limit: int) -> Iterator[Place]:
        seen = 0
        needle = spec.search_string.lower()
        for raw in self._load():
            # When the file records which query produced each row, honour it.
            row_query = str(raw.get("query") or raw.get("search_query") or "").lower()
            if row_query and needle and row_query != needle:
                continue
            place = place_from_mapping(raw, query=spec.search_string, source=self.name)
            if place is None:
                continue
            yield place
            seen += 1
            if seen >= limit:
                return


class CacheOnlyMaps(MapsProvider):
    """Used when resuming: every query's listings are already in the maps
    cache, so no provider is needed. A query that somehow is not cached is a
    clear error rather than a silent empty result."""

    name = "cache"
    requires_key = False

    def search(self, spec: QuerySpec, limit: int) -> Iterator[Place]:
        raise ProviderError(
            f"{spec.search_string!r} is not in the maps cache and no maps provider is "
            "configured - save your Maps key (`scraper buddy`) and resume again"
        )
        yield  # pragma: no cover - makes this a generator
