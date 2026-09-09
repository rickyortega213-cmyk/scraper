"""ScraperAPI maps provider, driven through a mocked HTTP transport."""

from __future__ import annotations

import httpx
import pytest

from gmscrape.config import Settings
from gmscrape.providers.base import ProviderError
from gmscrape.providers.maps.scraperapi import ScraperApiMaps
from gmscrape.query import parse_query

SPEC = parse_query("plumber in austin tx")


def make_provider(handler, **overrides) -> ScraperApiMaps:
    settings = Settings.from_env(scraperapi_key="deadbeef", **overrides)
    provider = ScraperApiMaps(settings)
    provider.client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return provider


def test_normalizes_listings_from_local_results():
    payload = {
        "search_information": {"query": "plumber in austin tx"},
        "local_results": [{
            "position": 1,
            "title": "Joe's Plumbing & Heating",
            "place_id": "abc123",
            "rating": 4.8,
            "reviews": "1,204",
            "type": "Plumber",
            "address": "100 Main St, Austin, TX 78701",
            "phone": "(512) 555-0100",
            "website": "joesplumbing.com",
            "gps_coordinates": {"latitude": 30.2672, "longitude": -97.7431},
        }],
    }
    provider = make_provider(lambda r: httpx.Response(200, json=payload))
    places = list(provider.search(SPEC, limit=10))

    assert len(places) == 1
    place = places[0]
    assert place.name == "Joe's Plumbing & Heating"
    assert place.place_id == "abc123"
    assert place.website == "https://joesplumbing.com/"
    assert place.domain == "joesplumbing.com"
    assert place.reviews == 1204 and place.rating == 4.8
    assert place.category == "Plumber"
    assert (place.latitude, place.longitude) == (30.2672, -97.7431)
    assert place.source == "scraperapi" and place.query == "plumber in austin tx"


@pytest.mark.parametrize("envelope", ["results", "places", "data"])
def test_accepts_alternative_envelope_keys(envelope):
    payload = {envelope: [{"name": "Austin Family Dental", "site": "afd.com"}]}
    provider = make_provider(lambda r: httpx.Response(200, json=payload))
    places = list(provider.search(SPEC, limit=5))
    assert [p.name for p in places] == ["Austin Family Dental"]
    assert places[0].domain == "afd.com"


def test_accepts_a_bare_array_response():
    provider = make_provider(
        lambda r: httpx.Response(200, json=[{"title": "Taco Spot"}])
    )
    assert [p.name for p in provider.search(SPEC, limit=5)] == ["Taco Spot"]


def test_paginates_and_stops_when_a_page_repeats():
    pages = {
        "1": [{"title": "A", "place_id": "a"}, {"title": "B", "place_id": "b"}],
        "2": [{"title": "C", "place_id": "c"}],
        "3": [{"title": "C", "place_id": "c"}],   # endpoint ignored `page`
    }
    seen_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        seen_pages.append(page)
        return httpx.Response(200, json={"local_results": pages.get(page, [])})

    provider = make_provider(handler, maps_max_pages=5)
    names = [p.name for p in provider.search(SPEC, limit=50)]
    assert names == ["A", "B", "C"]
    assert seen_pages == ["1", "2", "3"]          # stopped at the repeat


def test_honours_the_limit_mid_page():
    payload = {"local_results": [{"title": f"Biz {i}", "place_id": str(i)} for i in range(10)]}
    provider = make_provider(lambda r: httpx.Response(200, json=payload))
    assert len(list(provider.search(SPEC, limit=3))) == 3


def test_surfaces_api_errors():
    provider = make_provider(
        lambda r: httpx.Response(200, json={"error": "Invalid API key"})
    )
    with pytest.raises(ProviderError, match="Invalid API key"):
        list(provider.search(SPEC, limit=5))


def test_empty_response_is_not_an_error():
    provider = make_provider(lambda r: httpx.Response(200, json={"local_results": []}))
    assert list(provider.search(SPEC, limit=5)) == []


def test_missing_key_is_a_clear_error():
    provider = ScraperApiMaps(Settings.from_env(scraperapi_key="", maps_api_key=""))
    with pytest.raises(ProviderError, match="SCRAPERAPI_KEY"):
        list(provider.search(SPEC, limit=5))
