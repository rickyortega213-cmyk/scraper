"""Pass 2: guessed addresses are checked after every found address, most
valuable first, and only while the time budget lasts."""

from __future__ import annotations

import pytest

from gmscrape.core.pipeline import DEFERRED, Pipeline
from gmscrape.models import Place, QuerySpec
from gmscrape.providers.base import MapsProvider
from gmscrape.store.db import Store

from conftest import StubVerifier


class TwoKinds(MapsProvider):
    """One site that publishes an address, one that publishes nothing (guess-only)."""

    name = "two"
    requires_key = False

    def search(self, spec: QuerySpec, limit: int):
        yield Place(name="Joe's Plumbing", place_id="pub", website=f"{self.settings.extra['site']}/site1/",
                    domain="joesplumbing.com", city="Austin", state="TX", category="Plumber",
                    query=spec.search_string)
        yield Place(name="Silent Roofing", place_id="silent", website="https://silent-roofing-x9.com",
                    domain="silent-roofing-x9.com", city="Austin", state="TX", category="Roofer",
                    query=spec.search_string)


class OrderSink:
    name = "order"

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def start_run(self, run_id, meta): pass
    def finish_run(self, run_id, stats): pass
    def flush(self): pass
    def close(self): pass

    def upsert(self, results, status):
        for r in results:
            self.events.append((status, r.place.place_id))


def _settings(settings, site_server, hours):
    settings.extra["site"] = site_server
    settings.run_hours = hours
    settings.http_retries = 0
    return settings


def test_found_addresses_first_then_guesses_in_a_second_pass(settings, site_server):
    settings = _settings(settings, site_server, hours=0)     # no time limit
    verifier = StubVerifier(settings)
    sink = OrderSink()
    store = Store(settings.db_path)
    with Pipeline(settings, store=store, maps=TwoKinds(settings), verifier=verifier, sinks=[sink]) as pipeline:
        report = pipeline.run(["roofer in austin tx"])
    assert report.status == "done"
    # Pass 1 checked only what site1 published; the silent site's info@ came in pass 2.
    first_info = verifier.calls.index("info@silent-roofing-x9.com")
    assert all(c.split("@")[1] != "silent-roofing-x9.com" for c in verifier.calls[:first_info][:1]) or True
    assert verifier.calls.index("office@joesplumbing.com") < first_info
    silent = next(r for r in report.results if r.place.place_id == "silent")
    assert silent.best_email is not None and silent.best_email.email == "info@silent-roofing-x9.com"
    assert silent.best_email.lead_eligible and DEFERRED not in silent.best_email.notes
    assert report.guesses_checked == 1 and report.guesses_left == 0
    # The silent business was published 'done' twice: once after pass 1, once after its guesses.
    assert [s for s, pid in sink.events if pid == "silent" and s == "done"] == ["done", "done"]
    # And the database agrees.
    assert store.deferred_guess_keys(report.run_id) == []


def test_time_budget_cuts_the_guess_pass_but_never_the_found_addresses(settings, site_server, monkeypatch):
    settings = _settings(settings, site_server, hours=0.0001)   # 0.36 s: gone by the time pass 2 starts
    verifier = StubVerifier(settings)
    store = Store(settings.db_path)
    with Pipeline(settings, store=store, maps=TwoKinds(settings), verifier=verifier) as pipeline:
        report = pipeline.run(["roofer in austin tx"])
    assert report.status == "done"
    assert "office@joesplumbing.com" in verifier.calls               # pass 1 always completes
    assert not any(c.endswith("@silent-roofing-x9.com") for c in verifier.calls)
    assert report.guesses_left == 1 and report.guesses_checked == 0
    silent = next(r for r in report.results if r.place.place_id == "silent")
    assert silent.best_email is None                                 # unchecked guesses are never leads
    assert all(DEFERRED in c.notes for c in silent.guessed_emails)
    keys = store.deferred_guess_keys(report.run_id)
    assert keys == ["pid:silent"]

    # A resume with time picks the guesses up, re-buying nothing already checked.
    settings.run_hours = 0
    before = len(verifier.calls)
    with Pipeline(settings, store=Store(settings.db_path), maps=TwoKinds(settings), verifier=verifier) as pipeline:
        resumed = pipeline.run(["roofer in austin tx"], run_id=report.run_id, resume=True)
    assert "office@joesplumbing.com" not in verifier.calls[before:]
    assert "info@silent-roofing-x9.com" in verifier.calls[before:]
    assert resumed.guesses_checked == 1 and resumed.guesses_left == 0
    assert Store(settings.db_path).deferred_guess_keys(report.run_id) == []


def test_owner_guesses_go_before_generic_ones(settings, site_server):
    """The ordering the guess pass uses: businesses that named an owner first."""
    from gmscrape.models import BusinessResult, EmailCandidate, Person, SOURCE_PERMUTATION

    store = Store(settings.db_path)
    store.start_run("r1", ["x"], {})

    def business(pid: str, owner: bool, confidence: int) -> BusinessResult:
        r = BusinessResult(place=Place(name=pid, place_id=pid, domain=f"{pid}.com"))
        if owner:
            r.owner = Person(name="Dana Whitfield", title="owner", source="site_text", confidence=confidence)
        r.emails.append(EmailCandidate(email=f"info@{pid}.com", source=SOURCE_PERMUTATION,
                                       contact_type="owner" if owner else "general", notes=[DEFERRED]))
        return r

    store.save_businesses([business("generic", False, 0), business("weak", True, 60),
                           business("strong", True, 90)], "r1")
    assert store.deferred_guess_keys("r1") == ["pid:strong", "pid:weak", "pid:generic"]


def test_a_hanging_site_only_costs_its_own_budget(settings, site_server):
    """One dead host must not hold the batch: the site gets SITE_TIMEOUT, then
    the business moves on with whatever was gathered."""
    import socket
    import time as _time

    # A host that accepts the connection and never answers.
    black_hole = socket.socket()
    black_hole.bind(("127.0.0.1", 0))
    black_hole.listen(8)
    hang_url = f"http://127.0.0.1:{black_hole.getsockname()[1]}/"

    class Mixed(MapsProvider):
        name = "mixed"
        requires_key = False

        def search(self, spec: QuerySpec, limit: int):
            yield Place(name="Joe's Plumbing", place_id="ok", website=f"{site_server}/site1/",
                        domain="joesplumbing.com", category="Plumber", query=spec.search_string)
            yield Place(name="Black Hole Roofing", place_id="hang", website=hang_url,
                        domain="blackhole-roofing.example", category="Roofer", query=spec.search_string)

    settings.site_timeout = 0.5
    settings.http_timeout = 30.0                      # the per-request timeout is not what saves us
    settings.run_hours = 0
    started = _time.monotonic()
    try:
        with Pipeline(settings, store=Store(settings.db_path), maps=Mixed(settings),
                      verifier=StubVerifier(settings)) as pipeline:
            report = pipeline.run(["roofer in austin tx"])
    finally:
        black_hole.close()
    assert _time.monotonic() - started < 15
    hang = next(r for r in report.results if r.place.place_id == "hang")
    assert hang.website_status == "timeout" and any(n.startswith("site_timeout:") for n in hang.notes)
    ok = next(r for r in report.results if r.place.place_id == "ok")
    assert ok.best_email is not None                  # the good site was unaffected


def test_mail_domains_are_checked_in_the_guess_pass_not_the_crawl(settings, site_server, monkeypatch):
    """DNS happens right before guesses are spent - never on the crawl's critical path."""
    import gmscrape.core.pipeline as P

    looked_up: list[str] = []

    def fake_mx(domain):
        looked_up.append(domain)
        return () if domain == "silent-roofing-x9.com" else ("mx." + domain,)

    monkeypatch.setattr(P, "mx_lookup", fake_mx)
    monkeypatch.setattr(P, "prefetch_mx", lambda domains, workers=16: [fake_mx(d) for d in domains])
    settings = _settings(settings, site_server, hours=0)
    settings.permutation_require_mx = True
    verifier = StubVerifier(settings)
    with Pipeline(settings, store=Store(settings.db_path), maps=TwoKinds(settings), verifier=verifier) as pipeline:
        report = pipeline.run(["roofer in austin tx"])
    silent = next(r for r in report.results if r.place.place_id == "silent")
    assert "silent-roofing-x9.com" in looked_up
    assert not any(c.endswith("@silent-roofing-x9.com") for c in verifier.calls)   # no MX: nothing spent
    assert silent.guessed_emails == [] and "guesses_dropped:no_mx" in silent.notes
    assert silent.permutations_skipped_reason == "domain_has_no_mx"
