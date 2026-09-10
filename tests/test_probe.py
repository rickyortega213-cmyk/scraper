"""API shape discovery, against a real local HTTP server.

The probe drives its own HTTP client, so these tests serve an actual API that
behaves like a typical scraper service: it rejects calls with no key, rejects
calls with no search term, and otherwise returns listings in a nested envelope.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
from pathlib import Path

import pytest

from gmscrape.config import Settings
from gmscrape.probe import (
    build_generic_config,
    describe_mapping,
    find_result_rows,
    probe_maps_api,
    redact,
)
from gmscrape.providers.maps.generic import GenericMaps
from gmscrape.query import parse_query

API_KEY = "0123456789abcdef0123456789abcdef"   # 32 hex chars, like a real key

LISTINGS = [
    {
        "business_name": "Austin Family Dental",
        "web": "https://austinfamilydental.com",
        "phone_number": "(512) 555-0142",
        "full_address": "220 Oak Ave, Austin, TX 78702",
        "avg_rating": 4.6,
        "review_count": 412,
        "gmaps_id": "demo-1",
        "primary_category": "Dentist",
    },
    {
        "business_name": "Smile Studio ATX",
        "web": "smilestudioatx.com",
        "phone_number": "(512) 555-0188",
        "full_address": "9 Elm St, Austin, TX 78701",
        "avg_rating": 4.9,
        "review_count": 88,
        "gmaps_id": "demo-2",
        "primary_category": "Dentist",
    },
]


class _Api(http.server.BaseHTTPRequestHandler):
    """Key as the `apikey` query param, search term as `q`, listings at data.results."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        from urllib.parse import parse_qs, urlsplit

        parts = urlsplit(self.path)
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if parts.path != "/maps":
            return self._send(404, {"error": "not found"})
        if params.get("apikey") != API_KEY:
            return self._send(401, {"error": "invalid or missing api key"})
        if not params.get("q"):
            return self._send(400, {"error": "parameter 'q' is required"})
        return self._send(200, {"status": "ok", "data": {"results": LISTINGS}})

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture(scope="module")
def api() -> str:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_discovers_auth_style_and_search_parameter(api):
    winner, attempts = probe_maps_api(f"{api}/maps", API_KEY, "dentist in austin tx")

    assert winner is not None
    assert winner.auth_style == "query:apikey"
    assert winner.query_param == "q"
    assert winner.results_path == "data.results"
    assert winner.rows == 2
    # Frugal: one attempt establishes auth (400), the next finds the param.
    assert len(attempts) == 2


def test_reports_failure_without_burning_the_budget(api):
    winner, attempts = probe_maps_api(f"{api}/maps", "wrong-key", "dentist", max_requests=5)
    assert winner is None
    assert len(attempts) == 5
    assert all(a.status == 401 for a in attempts)


def test_finds_the_endpoint_path_from_a_bare_host(api):
    winner, _ = probe_maps_api(api, API_KEY, "dentist in austin tx")
    assert winner is not None and winner.url.endswith("/maps")


def test_discovered_config_actually_drives_a_search(api, tmp_path: Path):
    """The probe's output must be usable as-is by the generic provider."""
    winner, _ = probe_maps_api(f"{api}/maps", API_KEY, "dentist in austin tx")
    config = build_generic_config(winner, name="probed")

    config_path = tmp_path / "probed.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    settings = Settings.from_env(
        generic_maps_config=str(config_path), maps_api_key=API_KEY, maps_max_pages=1
    )
    provider = GenericMaps(settings)
    places = list(provider.search(parse_query("dentist in austin tx"), limit=10))

    assert [p.name for p in places] == ["Austin Family Dental", "Smile Studio ATX"]
    first = places[0]
    assert first.domain == "austinfamilydental.com"      # from "web"
    assert first.phone == "(512) 555-0142"               # from "phone_number"
    assert first.reviews == 412 and first.rating == 4.6  # review_count / avg_rating
    assert first.place_id == "demo-1"                    # gmaps_id
    assert first.category == "Dentist"
    assert first.source == "probed"


def test_config_records_auth_placement_correctly(api):
    winner, _ = probe_maps_api(f"{api}/maps", API_KEY, "dentist in austin tx")
    config = build_generic_config(winner)
    assert config["query"]["apikey"] == "{api_key}"
    assert config["query"]["q"] == "{query}"
    assert config["results_path"] == "data.results"
    assert config["headers"] == {}


def test_result_row_detection_prefers_business_shaped_lists():
    payload = {
        "meta": {"tags": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]},
        "data": {"results": [{"title": "A", "address": "x", "phone": "1", "web": "a.com"}]},
    }
    path, rows = find_result_rows(payload)
    assert path == "data.results" and len(rows) == 1


def test_describe_mapping_flags_unmapped_fields():
    resolved, unresolved = describe_mapping({"business_name": "X", "web": "x.com"})
    assert resolved["name"] == "X" and resolved["website"] == "x.com"
    assert "phone" in unresolved


def test_keys_are_redacted_in_output():
    assert redact(f"https://x/?apikey={API_KEY}", API_KEY) == "https://x/?apikey=a25e…00"
    assert redact("no key here", "short") == "no key here"
