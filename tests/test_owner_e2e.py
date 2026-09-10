"""Website discovery + owner lookup + owner mailbox guessing, end to end.

A fake OpenWeb Ninja answers the two kinds of query the pipeline sends, real
local sites get crawled, and the stub verifier decides which guesses are real.
"""

from __future__ import annotations

import csv
import http.server
import json
import socket
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from gmscrape.core.pipeline import Pipeline
from gmscrape.models import CONTACT_GENERAL, CONTACT_OWNER, V_VALID
from gmscrape.providers.maps.file_provider import FileMaps
from gmscrape.providers.search import openwebninja
from gmscrape.providers.search.openwebninja import OpenWebNinjaSearch
from gmscrape.store.db import Store
from gmscrape.store.export import export_results

from conftest import StubVerifier

SEARCH_KEY = "ak_test_key"


class _Ninja(http.server.BaseHTTPRequestHandler):
    """Answers discovery and owner queries the way Google would."""

    site_base = ""
    queries: list[str] = []

    accepted_keys = {SEARCH_KEY}
    keys_seen: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("x-api-key") not in self.accepted_keys:
            return self._send(401, {"status": "ERROR", "message": "invalid key"})
        self.keys_seen.append(self.headers.get("x-api-key", ""))
        q = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
        self.queries.append(q)
        base = self.site_base
        if q.startswith("who is the owner of Hill Country Landscaping"):
            payload = {"status": "OK", "has_ai_overviews": True, "data": {
                "organic_results": [
                    {"url": f"{base}/site5/", "title": "Hill Country Landscaping",
                     "snippet": "Hill Country Landscaping is owned by Maria Lopez, who started it in 2012."},
                    {"url": "https://www.yelp.com/biz/hcl", "title": "Hill Country Landscaping - Yelp",
                     "snippet": "Maria Lopez, owner, replied to this review."},
                ],
                "ai_overview": {"text_parts": [
                    {"text": "Hill Country Landscaping in Austin is owned by Maria Lopez."}]},
            }}
        elif q.startswith("who is the owner of Zilker Tacos"):
            payload = {"status": "OK", "data": {"organic_results": [
                {"url": "https://www.eater.com/austin", "title": "Best tacos in Austin",
                 "snippet": "Owner Carlos Vega of Vega's Taqueria talks tacos."}]}}
        elif '"Bluebonnet Roofing"' in q:
            payload = {"status": "OK", "data": {"organic_results": [
                {"url": "https://www.yelp.com/biz/bluebonnet-roofing-austin",
                 "title": "Bluebonnet Roofing - Yelp", "snippet": "reviews", "position": 1},
                {"url": f"{base}/site4/", "title": "Bluebonnet Roofing | Austin TX Roofers",
                 "snippet": "Austin's roofing specialists. Call (512) 555-0177. Austin, TX",
                 "position": 2},
            ]}}
        elif '"Zilker Tacos"' in q:
            # The best hit is somebody else's site - confirmation must reject it.
            payload = {"status": "OK", "data": {"organic_results": [
                {"url": f"{base}/site3/", "title": "Zilker Tacos - Austin",
                 "snippet": "Zilker Tacos, Austin TX (512) 555-0123", "position": 1}]}}
        else:
            payload = {"status": "OK", "data": {"organic_results": []}}
        self._send(200, payload)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def ninja(site_server, monkeypatch):
    _Ninja.site_base = site_server
    _Ninja.queries = []
    _Ninja.keys_seen = []
    _Ninja.accepted_keys = {SEARCH_KEY}
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Ninja)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(openwebninja, "ENDPOINT", f"http://127.0.0.1:{port}/realtime-web-search/search")
    try:
        yield _Ninja
    finally:
        server.shutdown()
        server.server_close()


def _places(tmp_path: Path, site_server: str) -> Path:
    rows = [
        # no website on Maps -> discovered via search, owner named on its About page
        {"name": "Bluebonnet Roofing", "website": "", "domain": "", "phone": "(512) 555-0177",
         "city": "Austin", "state": "TX", "category": "Roofing contractor", "place_id": "o1"},
        # website known, general email on site, owner only findable via search
        {"name": "Hill Country Landscaping", "website": f"{site_server}/site5/",
         "domain": "hillcountrylandscaping.com", "phone": "(512) 555-0190",
         "city": "Austin", "state": "TX", "category": "Landscaper", "place_id": "o2"},
        # no website; search returns a wrong site; owner search names someone else's owner
        {"name": "Zilker Tacos", "website": "", "domain": "", "phone": "(512) 555-0123",
         "city": "Austin", "state": "TX", "category": "Taco restaurant", "place_id": "o3"},
        # website known and owner named on site (site1 has none) - control row
        {"name": "Joe's Plumbing & Heating", "website": f"{site_server}/site1/",
         "domain": "joesplumbing.com", "phone": "(512) 555-0100",
         "city": "Austin", "state": "TX", "category": "Plumber", "place_id": "o4"},
    ]
    path = tmp_path / "places.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def run(tmp_path, settings, site_server, ninja):
    settings.places_file = str(_places(tmp_path, site_server))
    settings.openwebninja_key = SEARCH_KEY
    settings.max_pages_per_site = 4
    store = Store(settings.db_path)
    verifier = StubVerifier(settings)
    pipeline = Pipeline(
        settings, store=store, maps=FileMaps(settings), verifier=verifier,
        web_search=OpenWebNinjaSearch(settings),
    )
    with pipeline:
        report = pipeline.run(["*"])
    return report, verifier, settings


def _by_name(results, needle):
    return next(r for r in results if needle in r.place.name)


def test_website_is_discovered_confirmed_and_crawled(run):
    report, _, _ = run
    roofing = _by_name(report.results, "Bluebonnet")
    assert roofing.website_source == "search"
    assert roofing.place.website.endswith("/site4/")
    assert roofing.website_status == "ok"
    assert any(n.startswith("site_confirmed:") and "phone_on_page" in n for n in roofing.notes)
    assert "hello@bluebonnetroofing.com" in {c.email for c in roofing.found_emails}


def test_owner_found_on_the_about_page(run):
    report, _, _ = run
    roofing = _by_name(report.results, "Bluebonnet")
    assert roofing.owner is not None
    assert roofing.owner.name == "John Kowalski" and roofing.owner.title == "owner"
    assert roofing.owner.source.startswith("site")
    assert not roofing.owner_search_done, "no search needed when the site names them"


def test_owner_found_by_search_and_mailbox_guessed(run):
    report, verifier, _ = run
    hcl = _by_name(report.results, "Hill Country")
    assert hcl.owner is not None and hcl.owner.name == "Maria Lopez"
    assert hcl.owner.source == "search_ai_overview"
    assert hcl.owner_search_done

    owner_calls = [c for c in verifier.calls if c.endswith("@hillcountrylandscaping.com")
                   and not c.startswith("info@")]
    # first@ is checked, fails; first.last@ verifies; nothing after it is spent.
    assert owner_calls == ["maria@hillcountrylandscaping.com",
                           "maria.lopez@hillcountrylandscaping.com"]

    owner_email = hcl.best_owner_email
    assert owner_email is not None
    assert owner_email.email == "maria.lopez@hillcountrylandscaping.com"
    assert owner_email.status == V_VALID and owner_email.contact_name == "Maria Lopez"
    assert hcl.best_general_email.email == "info@hillcountrylandscaping.com"


def test_two_rows_when_both_general_and_owner_exist(run, tmp_path):
    report, _, settings = run
    paths = export_results(report.results, settings.out_dir, formats=["csv"])
    rows = list(csv.DictReader(next(p for p in paths if p.name == "leads_detailed.csv").open(encoding="utf-8-sig")))
    hcl = [r for r in rows if r["name"] == "Hill Country Landscaping"]
    assert len(hcl) == 2
    by_type = {r["contact_type"]: r for r in hcl}
    assert by_type[CONTACT_OWNER]["email"] == "maria.lopez@hillcountrylandscaping.com"
    assert by_type[CONTACT_OWNER]["contact_name"] == "Maria Lopez"
    assert by_type[CONTACT_GENERAL]["email"] == "info@hillcountrylandscaping.com"
    # Everything but the contact columns is identical on both rows.
    for column in ("phone", "website", "city", "query", "address", "owner_name"):
        assert by_type[CONTACT_OWNER][column] == by_type[CONTACT_GENERAL][column]
    assert by_type[CONTACT_OWNER]["owner_name"] == "Maria Lopez"


def test_wrong_discovered_site_is_rejected_and_wrong_owner_ignored(run):
    report, _, _ = run
    tacos = _by_name(report.results, "Zilker")
    assert tacos.place.website == ""                       # rejected, not kept
    assert tacos.website_status.startswith("discovered_unconfirmed")
    assert tacos.emails == []                              # nothing leaked from the wrong site
    assert tacos.owner is None                             # Carlos Vega owns a different taqueria


def test_failed_owner_guesses_are_dropped_from_leads(run):
    report, _, _ = run
    hcl = _by_name(report.results, "Hill Country")
    emails = {c.email for c in hcl.emails}
    assert "maria@hillcountrylandscaping.com" not in emails      # verified invalid -> gone
    assert all(c.status == V_VALID for c in hcl.lead_contacts())


def test_unverified_guesses_never_become_lead_rows(tmp_path, settings, site_server, ninja):
    """Even when invalid addresses are kept for the emails export, a guess that
    did not verify `valid` must not surface as a lead."""
    settings.places_file = str(_places(tmp_path, site_server))
    settings.openwebninja_key = SEARCH_KEY
    settings.keep_invalid = True
    store = Store(settings.db_path)
    with Pipeline(settings, store=store, maps=FileMaps(settings),
                  verifier=StubVerifier(settings),
                  web_search=OpenWebNinjaSearch(settings)) as pipeline:
        report = pipeline.run(["*"])
    hcl = _by_name(report.results, "Hill Country")
    ineligible = [c for c in hcl.emails if not c.lead_eligible]
    assert [c.email for c in ineligible] == ["maria@hillcountrylandscaping.com"]
    assert "not_lead_eligible:unverified_guess" in ineligible[0].notes
    assert all(c.lead_eligible and c.status == V_VALID for c in hcl.lead_contacts())
    assert len(hcl.lead_contacts()) == 2


def test_search_calls_are_cached_across_runs(tmp_path, settings, site_server, ninja):
    settings.places_file = str(_places(tmp_path, site_server))
    settings.openwebninja_key = SEARCH_KEY
    for _ in range(2):
        store = Store(settings.db_path)
        with Pipeline(settings, store=store, maps=FileMaps(settings),
                      verifier=StubVerifier(settings),
                      web_search=OpenWebNinjaSearch(settings)) as pipeline:
            report = pipeline.run(["*"])
    assert report.search_calls == 0 and report.search_cache_hits >= 3
    stats = report.stats()
    assert stats["websites_discovered"] == 1
    assert stats["owners_found"] == 2 and stats["owner_emails"] == 1
    assert stats["lead_rows"] == 5          # 4 businesses, one of them with two contacts


def test_several_search_keys_rotate_and_a_refused_one_is_dropped(ninja, settings):
    """OPENWEBNINJA_KEY=key1,key2: calls alternate; a dead key is dropped, not fatal."""
    ninja.accepted_keys = {SEARCH_KEY, "ak_second"}
    settings.openwebninja_key = f"{SEARCH_KEY}, ak_second"
    search = OpenWebNinjaSearch(settings)
    assert search.key_count == 2
    for i in range(4):
        assert search.search(f"query {i}").error == ""
    assert ninja.keys_seen == [SEARCH_KEY, "ak_second", SEARCH_KEY, "ak_second"]

    ninja.keys_seen = []
    settings.openwebninja_key = f"ak_dead, {SEARCH_KEY}"
    search = OpenWebNinjaSearch(settings)
    assert search.search("query a").error == ""          # dead key refused -> retried on the good one
    assert search.key_count == 1 and ninja.keys_seen == [SEARCH_KEY]
    for i in range(3):
        assert search.search(f"query {i}").error == ""
    assert set(ninja.keys_seen) == {SEARCH_KEY}

    from gmscrape.providers.base import ProviderError
    settings.openwebninja_key = "ak_dead"
    with pytest.raises(ProviderError, match="rejected"):
        OpenWebNinjaSearch(settings).search("query z")
