"""End-to-end run against a real (local) web server.

Covers the whole chain: places file -> chain classification -> crawl ->
extraction -> permutation fallback -> verification -> scoring -> export.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from gmscrape.core.pipeline import Pipeline
from gmscrape.models import SOURCE_PERMUTATION, V_VALID
from gmscrape.providers.maps.file_provider import FileMaps
from gmscrape.store.db import Store
from gmscrape.store.export import export_results

from conftest import StubVerifier


def _places_file(tmp_path: Path, base_url: str) -> Path:
    rows = [
        {"name": "Joe's Plumbing & Heating", "website": f"{base_url}/site1/",
         "category": "Plumber", "city": "Austin", "state": "TX", "reviews": "87",
         "place_id": "p1", "phone": "(512) 555-0100"},
        {"name": "Austin Family Dental", "website": f"{base_url}/site2/",
         "category": "Dentist", "city": "Austin", "state": "TX", "reviews": "412",
         "place_id": "p2"},
        {"name": "Riverside Taqueria", "website": f"{base_url}/site3/",
         "category": "Mexican restaurant", "city": "Austin", "state": "TX",
         "reviews": "2900", "place_id": "p3"},
        {"name": "Walmart Supercenter #1234", "website": "https://www.walmart.com/store/1234",
         "category": "Department store", "city": "Austin", "state": "TX",
         "reviews": "5200", "place_id": "p4"},
        {"name": "Bluebonnet Roofing", "website": "https://bluebonnet-roofing-9x7q2z.com",
         "category": "Roofing contractor", "city": "Austin", "state": "TX",
         "reviews": "35", "place_id": "p5"},
    ]
    path = tmp_path / "places.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def report(tmp_path, settings, site_server):
    settings.places_file = str(_places_file(tmp_path, site_server))
    settings.crawl_websites = True
    store = Store(settings.db_path)
    pipeline = Pipeline(
        settings, store=store, maps=FileMaps(settings), verifier=StubVerifier(settings)
    )
    with pipeline:
        yield pipeline.run(["*"]), pipeline, settings


def _by_name(results, needle):
    return next(r for r in results if needle in r.place.name)


def test_scrapes_emails_from_the_website(report):
    run, _, _ = report
    joe = _by_name(run.results, "Joe's Plumbing")
    emails = {c.email for c in joe.emails}
    assert "office@joesplumbing.com" in emails            # mailto on /contact
    assert "dispatch@joesplumbing.com" in emails          # "[at]" obfuscation
    assert joe.website_status == "ok"
    assert len(joe.pages_crawled) >= 2                    # followed the contact link


def test_keeps_personal_mailboxes_for_local_businesses(report):
    run, _, _ = report
    joe = _by_name(run.results, "Joe's Plumbing")
    personal = [c for c in joe.emails if c.is_personal_domain]
    assert [c.email for c in personal] == ["joe.plumber1972@gmail.com"]


def test_filters_analytics_and_asset_noise(report):
    run, _, _ = report
    all_emails = {c.email for r in run.results for c in r.emails}
    assert not any("sentry.io" in e or e.endswith(".png") for e in all_emails)
    # no-reply@ is dropped by the output filter
    assert "no-reply@austinfamilydental.com" not in all_emails


def test_decodes_cloudflare_and_jsonld(report):
    run, _, _ = report
    dental = _by_name(run.results, "Austin Family Dental")
    emails = {c.email for c in dental.emails}
    assert "frontdesk@austinfamilydental.com" in emails   # cloudflare-encoded
    assert "newpatients@austinfamilydental.com" in emails  # JSON-LD


def test_permutations_only_when_nothing_was_found(report):
    run, _, _ = report
    joe = _by_name(run.results, "Joe's Plumbing")
    assert not joe.guessed_emails, "should not guess when real addresses exist"

    roofing = _by_name(run.results, "Bluebonnet Roofing")
    guessed = [c.email for c in roofing.guessed_emails]
    assert "info@bluebonnet-roofing-9x7q2z.com" in guessed
    assert roofing.website_status.startswith("unreachable")


def test_chains_are_flagged_and_never_guessed(report):
    run, _, _ = report
    walmart = _by_name(run.results, "Walmart")
    assert walmart.is_chain and walmart.chain_reasons
    assert not walmart.guessed_emails
    assert walmart.permutations_skipped_reason == "national_chain"

    taqueria = _by_name(run.results, "Riverside Taqueria")
    assert not taqueria.is_chain, "a popular local restaurant is not a chain"


def test_verification_stops_at_the_first_valid_guess(report):
    run, pipeline, _ = report
    roofing = _by_name(run.results, "Bluebonnet Roofing")
    best = roofing.best_email
    assert best is not None
    assert best.email.startswith("info@") and best.status == V_VALID
    # info@ is first in tier 1 and verifies, so nothing after it is checked.
    guessed_calls = [c for c in pipeline.verifier.calls if "bluebonnet" in c]
    assert guessed_calls == ["info@bluebonnet-roofing-9x7q2z.com"]
    # ...and the guesses that were never checked are not shipped as results.
    assert [c.email for c in roofing.guessed_emails] == guessed_calls
    assert any(note.startswith("guesses_not_checked=") for note in roofing.notes)


def test_scraped_addresses_outrank_guesses(report):
    run, _, _ = report
    joe = _by_name(run.results, "Joe's Plumbing")
    assert joe.best_email.source != SOURCE_PERMUTATION
    assert joe.best_email.confidence > 60


def test_persists_and_exports(report, tmp_path):
    run, pipeline, settings = report
    stats = run.stats()
    assert stats["businesses"] == 5
    assert stats["with_any_email"] >= 3

    db_stats = pipeline.store.stats()
    assert db_stats["businesses"] == 5 and db_stats["emails"] > 0

    paths = export_results(run.results, settings.out_dir, formats=["csv", "json", "xlsx"])
    csv_path = next(p for p in paths if p.name.endswith("leads.csv"))
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    assert len(rows) == 5
    assert any(row["best_email"] for row in rows)
    assert {"is_chain", "website_status", "best_email_confidence"} <= set(rows[0])

    payload = json.loads(next(p for p in paths if p.suffix == ".json").read_text())
    assert len(payload) == 5 and "emails" in payload[0]


def test_rerun_uses_cached_verifications(tmp_path, settings, site_server):
    settings.places_file = str(_places_file(tmp_path, site_server))
    store = Store(settings.db_path)
    first = StubVerifier(settings)
    with Pipeline(settings, store=store, maps=FileMaps(settings), verifier=first) as pipeline:
        pipeline.run(["*"])
    assert first.calls

    second = StubVerifier(settings)
    store2 = Store(settings.db_path)
    with Pipeline(settings, store=store2, maps=FileMaps(settings), verifier=second) as pipeline:
        report = pipeline.run(["*"])
    assert second.calls == [], "second run should be served entirely from cache"
    assert report.verification_cache_hits > 0


def test_full_run_through_a_mocked_maps_api(tmp_path, settings, site_server):
    """Maps provider -> crawl -> extract -> verify, with nothing stubbed but HTTP."""
    import httpx

    from gmscrape.providers.maps.scraperapi import ScraperApiMaps

    payload = {
        "local_results": [
            {"title": "Joe's Plumbing & Heating", "place_id": "m1", "type": "Plumber",
             "website": f"{site_server}/site1/", "reviews": 87, "rating": 4.8,
             "address": "100 Main St, Austin, TX 78701", "phone": "(512) 555-0100"},
            {"title": "McDonald's", "place_id": "m2", "type": "Fast food restaurant",
             "website": "https://mcdonalds.com/us/en-us.html", "reviews": 2100},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["query"] == "plumber in austin tx"
        assert request.url.params["api_key"] == "test-key"
        return httpx.Response(200, json=payload if request.url.params["page"] == "1" else {})

    settings.scraperapi_key = "test-key"
    maps = ScraperApiMaps(settings)
    maps.client._client = httpx.Client(transport=httpx.MockTransport(handler))

    store = Store(settings.db_path)
    with Pipeline(settings, store=store, maps=maps, verifier=StubVerifier(settings)) as pipeline:
        run = pipeline.run(["plumber in austin tx"])

    joe = _by_name(run.results, "Joe's Plumbing")
    assert joe.place.query == "plumber in austin tx"
    assert joe.place.domain == "" or joe.place.website.startswith(site_server)
    assert "office@joesplumbing.com" in {c.email for c in joe.emails}

    mcd = _by_name(run.results, "McDonald's")
    assert mcd.is_chain and not mcd.guessed_emails
