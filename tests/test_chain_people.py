"""Chains go through enrichment too - with the right person for the kind of chain."""

from __future__ import annotations

import csv
import http.server
import json
import socket
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from gmscrape.core.pipeline import Pipeline
from gmscrape.emails.people import (
    OwnerCandidate,
    choose_owner,
    emails_for_person_in_text,
    owner_candidates_from_search,
)
from gmscrape.filters.chains import chain_profile, classify
from gmscrape.models import CONTACT_MANAGER, CONTACT_OWNER, Person, Place, V_VALID
from gmscrape.providers.maps.file_provider import FileMaps
from gmscrape.providers.search import openwebninja
from gmscrape.providers.search.openwebninja import OpenWebNinjaSearch
from gmscrape.query import parse_query
from gmscrape.store.db import Store
from gmscrape.store.export import export_results

from conftest import StubVerifier

SEARCH_KEY = "ak_test_key"


# --- classification --------------------------------------------------------
def test_chain_kinds_pick_the_right_person():
    walmart = Place(name="Walmart Supercenter #1234", category="Department store",
                    website="https://www.walmart.com/store/1234")
    profile = chain_profile(walmart, classify(walmart))
    assert profile.kind == "corporate_store" and profile.contact_type == CONTACT_MANAGER
    assert profile.target_titles[0] == "store manager"

    mcd = Place(name="McDonald's", category="Fast food restaurant")
    profile = chain_profile(mcd, classify(mcd))
    assert profile.kind == "franchise" and profile.contact_type == CONTACT_OWNER
    assert "franchisee" in profile.target_titles

    og = Place(name="Olive Garden Italian Restaurant", category="Italian restaurant")
    assert chain_profile(og, classify(og)).kind == "corporate_restaurant"

    # Unknown brand: the category decides.
    unknown = Place(name="Zappy Burger #77", category="Hamburger restaurant", reviews=3000,
                    website="https://zappyburger.com")
    verdict = classify(unknown)
    if verdict.is_chain:
        assert chain_profile(unknown, verdict).kind == "franchise"
    assert chain_profile(Place(name="Joe's Plumbing"), classify(Place(name="Joe's Plumbing"))) is None


def test_two_locations_of_one_chain_are_not_deduplicated():
    a = Place(name="Walmart", domain="walmart.com", phone="(512) 555-1111")
    b = Place(name="Walmart", domain="walmart.com", phone="(512) 555-2222")
    assert a.dedupe_key() != b.dedupe_key()
    c = Place(name="Walmart", domain="walmart.com", address="1 Main St")
    d = Place(name="Walmart", domain="walmart.com", address="2 Elm St")
    assert c.dedupe_key() != d.dedupe_key()


def test_business_type_containing_in_still_splits_correctly():
    spec = parse_query("walk in clinic in austin tx")
    assert (spec.business_type, spec.location) == ("walk in clinic", "austin tx")
    spec = parse_query("drive in theater near San Antonio")
    assert (spec.business_type, spec.location) == ("drive in theater", "San Antonio")
    # ...without regressing phrasings that end in a second separator word.
    spec = parse_query("dentists in Austin near me")
    assert (spec.business_type, spec.location) == ("dentists", "Austin")
    spec = parse_query("restaurants in Austin within 5 miles")
    assert (spec.business_type, spec.location) == ("restaurants", "Austin")
    spec = parse_query("dentists in Austin - TX")
    assert spec.business_type == "dentists" and spec.location.startswith("Austin")


def test_city_is_derived_from_the_address_when_missing():
    from gmscrape.util import city_from_address

    assert city_from_address("220 Oak Ave, Austin, TX 78702") == "Austin"
    assert city_from_address("1200 S Lamar Blvd, Austin, TX 78704, USA") == "Austin"
    assert city_from_address("5 Rue Cler, Paris, France") == "Paris"
    assert city_from_address("Austin, TX") == ""            # no street -> not confident
    assert city_from_address("") == ""


# --- extraction ------------------------------------------------------------
def test_manager_titles_and_franchise_phrasing():
    blocks = [
        ("search_snippet", "Dana Whitfield - Store Manager - Walmart | LinkedIn. Austin, Texas."),
        ("search_snippet", "Rosa Delgado owns 12 McDonald's restaurants across Austin, TX."),
    ]
    walmart = owner_candidates_from_search(blocks, "Walmart", "Austin", require_location=True)
    assert [(c.name, c.title) for c in walmart] == [("Dana Whitfield", "store manager")]
    mcd = owner_candidates_from_search(blocks, "McDonald's", "Austin", require_location=True)
    assert [(c.name, c.title) for c in mcd] == [("Rosa Delgado", "owner")]


def test_location_gate_ignores_the_same_brand_elsewhere():
    blocks = [("search_snippet", "Bob Ray, Store Manager at Walmart in Dallas, Texas.")]
    assert owner_candidates_from_search(blocks, "Walmart", "Austin", require_location=True) == []
    assert owner_candidates_from_search(blocks, "Walmart", "Austin") != []   # gate off


def test_preferred_titles_rerank_for_the_situation():
    cands = [
        OwnerCandidate("Pat Regional", "regional manager", 50, "search_snippet", weight=2),
        OwnerCandidate("Sam Store", "store manager", 58, "search_snippet", weight=2),
        OwnerCandidate("Vee President", "president", 85, "search_snippet", weight=2),
    ]
    store_first = ("store manager", "general manager", "district manager", "regional manager")
    assert choose_owner(cands, preferred_titles=store_first).name == "Sam Store"
    # No preference: plain seniority.
    assert choose_owner(cands).name == "Vee President"


def test_a_loose_sentence_alone_is_not_a_person():
    """'...the store manager of X is Currently Hiring' must not become a lead."""
    blocks = [("search_ai_overview",
               "The store manager of the Walmart in Austin, TX is Currently Hiring.")]
    cands = owner_candidates_from_search(blocks, "Walmart", "Austin", require_location=True)
    assert cands == []                                       # stopword rejects it outright
    blocks = [("search_ai_overview",
               "The store manager of the Walmart in Austin, TX is Zorbo Flenn.")]
    cands = owner_candidates_from_search(blocks, "Walmart", "Austin", require_location=True)
    store_first = ("store manager", "general manager")
    assert choose_owner(cands, preferred_titles=store_first) is None   # one loose sentence
    blocks.append(("search_snippet", "Zorbo Flenn - Store Manager - Walmart · Austin, Texas"))
    cands = owner_candidates_from_search(blocks, "Walmart", "Austin", require_location=True)
    assert choose_owner(cands, preferred_titles=store_first).name == "Zorbo Flenn"  # corroborated


def test_corporate_executives_are_never_the_store_contact():
    cands = [OwnerCandidate("Doug McMillon", "ceo", 90, "search_ai_overview", weight=3),
             OwnerCandidate("Doug McMillon", "president", 85, "site_text", weight=2)]
    store_first = ("store manager", "general manager", "district manager")
    assert choose_owner(cands, preferred_titles=store_first) is None


def test_operates_is_plain_ownership_for_a_local_business():
    from gmscrape.emails.people import _iter_text_matches

    found = [(c.name, c.title) for c in _iter_text_matches("Jane Doe operates Joe's Plumbing in Austin.", "s")]
    assert ("Jane Doe", "owner") in found
    found = [(c.name, c.title) for c in _iter_text_matches("Jane Doe franchises three Subway stores.", "s")]
    assert ("Jane Doe", "franchise owner") in found


def test_snippet_email_is_attributed_only_when_it_spells_the_name():
    person = Person("Rosa Delgado", "franchise owner")
    text = ("Franchisee Rosa Delgado can be reached at rosa.delgado@mcdfranchise.com; "
            "press contact media@mcdonalds.com")
    assert [e for e, _ in emails_for_person_in_text(text, person)] == ["rosa.delgado@mcdfranchise.com"]
    assert emails_for_person_in_text("contact info@x.com", person) == []


# --- end to end --------------------------------------------------------------
class _Ninja(http.server.BaseHTTPRequestHandler):
    queries: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        q = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
        self.queries.append(q)
        if q.startswith("who is the store manager of Walmart Supercenter"):
            payload = {"status": "OK", "has_ai_overviews": True, "data": {
                "ai_overview": {"text_parts": [{"text":
                    "The store manager of the Walmart Supercenter in Austin, TX is Dana Whitfield."}]},
                "organic_results": [
                    {"url": "https://www.linkedin.com/in/bob-ray", "title": "Bob Ray - Store Manager - Walmart",
                     "snippet": "Store Manager at Walmart · Dallas, Texas"},
                    {"url": "https://www.linkedin.com/in/dana", "title": "Dana Whitfield - Store Manager - Walmart",
                     "snippet": "Store Manager at Walmart Supercenter · Austin, Texas"},
                ]}}
        elif q.startswith("who owns the McDonald's franchise"):
            payload = {"status": "OK", "data": {"organic_results": [
                {"url": "https://www.bizjournals.com/austin/x", "title": "Austin McDonald's franchisee expands",
                 "snippet": "Rosa Delgado owns 12 McDonald's restaurants across Austin, TX. "
                            "Reach her at rosa.delgado@mcdfranchise.com."}]}}
        elif q.startswith("who is the general manager of Olive Garden"):
            payload = {"status": "OK", "data": {"organic_results": []}}
        elif "Olive Garden" in q:
            payload = {"status": "OK", "data": {"organic_results": [
                {"url": "https://x.com", "title": "Olive Garden Austin",
                 "snippet": "Olive Garden in Austin, TX: general manager Lee Park and general manager Kim Ng."}]}}
        else:
            payload = {"status": "OK", "data": {"organic_results": []}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def ninja(monkeypatch):
    _Ninja.queries = []
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Ninja)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(openwebninja, "ENDPOINT", f"http://127.0.0.1:{port}/search")
    try:
        yield _Ninja
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def run(tmp_path, settings, ninja):
    rows = [
        {"name": "Walmart Supercenter #1234", "website": "https://www.walmart.com/store/1234",
         "category": "Department store", "city": "Austin", "state": "TX",
         "phone": "(512) 555-0001", "reviews": "5200", "place_id": "c1"},
        {"name": "McDonald's", "website": "https://www.mcdonalds.com/us/en-us.html",
         "category": "Fast food restaurant", "city": "Austin", "state": "TX",
         "phone": "(512) 555-0002", "reviews": "1800", "place_id": "c2"},
        {"name": "Olive Garden Italian Restaurant", "website": "https://www.olivegarden.com/locations/tx/austin",
         "category": "Italian restaurant", "city": "Austin", "state": "TX",
         "phone": "(512) 555-0003", "reviews": "2100", "place_id": "c3"},
    ]
    path = tmp_path / "places.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    settings.places_file = str(path)
    settings.openwebninja_key = SEARCH_KEY
    settings.http_timeout = 4.0
    store = Store(settings.db_path)
    verifier = StubVerifier(settings)
    with Pipeline(settings, store=store, maps=FileMaps(settings), verifier=verifier,
                  web_search=OpenWebNinjaSearch(settings)) as pipeline:
        report = pipeline.run(["*"])
    return report, verifier, settings


def _by_name(results, needle):
    return next(r for r in results if needle in r.place.name)


def test_corporate_store_gets_its_store_manager(run):
    report, verifier, _ = run
    walmart = _by_name(report.results, "Walmart")
    assert walmart.is_chain and walmart.chain_kind == "corporate_store"
    assert walmart.target_role == "store / district manager"
    assert walmart.owner is not None and walmart.owner.name == "Dana Whitfield"
    assert walmart.owner.title == "store manager"
    # Bob Ray manages a Walmart in Dallas - the location gate keeps him out.
    manager = walmart.best_owner_email
    assert manager is not None
    assert manager.email == "dana.whitfield@walmart.com"
    assert manager.contact_type == CONTACT_MANAGER and manager.status == V_VALID
    calls = [c for c in verifier.calls if c.endswith("@walmart.com")]
    assert calls == ["dana@walmart.com", "dana.whitfield@walmart.com"]   # stopped at the hit
    assert not any(c.email.startswith("info@") for c in walmart.emails)  # no generic guesses on chains


def test_franchise_gets_the_franchisee_and_her_own_address(run):
    report, verifier, _ = run
    mcd = _by_name(report.results, "McDonald's")
    assert mcd.chain_kind == "franchise" and mcd.owner.name == "Rosa Delgado"
    owner = mcd.best_owner_email
    assert owner.email == "rosa.delgado@mcdfranchise.com"
    assert owner.source == "search_snippet" and owner.contact_type == CONTACT_OWNER
    assert owner.status == V_VALID
    # Her address was found, so nothing was guessed on mcdonalds.com.
    assert not any(c.endswith("@mcdonalds.com") for c in verifier.calls)


def test_chain_without_a_city_is_not_searched(tmp_path, settings, ninja):
    rows = [{"name": "Walmart Supercenter #99", "website": "", "category": "Department store",
             "city": "", "state": "TX", "address": "", "phone": "(512) 555-0009", "place_id": "c9"}]
    path = tmp_path / "p.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    settings.places_file = str(path)
    settings.openwebninja_key = SEARCH_KEY
    settings.discover_websites = False          # the chain branch must not depend on this
    with Pipeline(settings, store=Store(settings.db_path), maps=FileMaps(settings),
                  verifier=StubVerifier(settings), web_search=OpenWebNinjaSearch(settings)) as pipeline:
        report = pipeline.run(["*"])
    result = report.results[0]
    assert result.owner is None and "chain_person:no_city" in result.notes
    assert ninja.queries == []                  # no credit spent on an ungated search


def test_two_managers_with_equal_support_means_none(run):
    report, _, _ = run
    og = _by_name(report.results, "Olive Garden")
    assert og.chain_kind == "corporate_restaurant"
    assert og.owner is None
    assert any(n.startswith("chain_person:no_confident_match") for n in og.notes)
    assert og.lead_contacts() == []


def test_chain_rows_are_labelled_by_contact_type(run, tmp_path):
    report, _, settings = run
    paths = export_results(report.results, settings.out_dir, formats=["csv"])
    rows = list(csv.DictReader(next(p for p in paths if p.name == "leads_detailed.csv").open(encoding="utf-8-sig")))
    by_name = {(r["name"], r["contact_type"]): r for r in rows}
    walmart = by_name[("Walmart Supercenter #1234", "manager")]
    assert walmart["is_chain"] == "yes" and walmart["contact_name"] == "Dana Whitfield"
    assert walmart["contact_title"] == "store manager"
    mcd = by_name[("McDonald's", "owner")]
    assert mcd["email"] == "rosa.delgado@mcdfranchise.com"
    assert len(rows) == 3                     # one row each; Olive Garden with no email
