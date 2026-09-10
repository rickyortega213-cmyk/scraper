"""Crash safety: cached maps results, batch checkpoints, resume, partial exports."""

from __future__ import annotations

import csv
import os

import pytest

from gmscrape.config import Settings
from gmscrape.core.pipeline import Pipeline, RunStopped
from gmscrape.models import Place, QuerySpec
from gmscrape.providers.base import MapsProvider
from gmscrape.store.db import Store
from gmscrape.store.supabase import run_table_name

from conftest import StubVerifier


class CountingMaps(MapsProvider):
    """A maps provider that counts how often it is really called."""

    name = "counting"
    requires_key = False

    def __init__(self, settings, places: list[Place]) -> None:
        super().__init__(settings)
        self.places = places
        self.calls: list[str] = []

    def search(self, spec: QuerySpec, limit: int):
        self.calls.append(spec.search_string)
        for place in self.places:
            place.query = spec.search_string
            yield place


def _places(site_server: str, n: int = 5) -> list[Place]:
    return [
        Place(name=f"Business {i}", place_id=f"b{i}", website=f"{site_server}/site1/",
              domain="joesplumbing.com", city="Austin", state="TX", phone=f"(512) 555-01{i:02d}",
              category="Plumber")
        for i in range(n)
    ]


def test_maps_results_are_cached_so_a_rerun_is_free(settings, site_server):
    maps = CountingMaps(settings, _places(site_server, 2))
    for _ in range(2):
        with Pipeline(settings, store=Store(settings.db_path), maps=maps,
                      verifier=StubVerifier(settings)) as pipeline:
            report = pipeline.run(["plumber in austin tx"])
    assert maps.calls == ["plumber in austin tx"]            # second run served from cache
    assert report.maps_cache_hits == 1 and len(report.results) == 2


def test_batches_checkpoint_and_report_progress(settings, site_server):
    settings.batch_size = 2
    events: list[dict] = []
    store = Store(settings.db_path)
    with Pipeline(settings, store=store, maps=CountingMaps(settings, _places(site_server, 5)),
                  verifier=StubVerifier(settings),
                  progress=lambda e, d: events.append({"event": e, **d})) as pipeline:
        report = pipeline.run(["plumber in austin tx"])
    batches = [e for e in events if e["event"] == "batch_done"]
    assert [(b["done"], b["total"]) for b in batches] == [(2, 5), (4, 5), (5, 5)]
    assert all("eta" in b and "elapsed" in b and "rate_per_hour" in b for b in batches)
    chunks = [e for e in events if e["event"] == "chunk_done"]
    assert [(c["queries_done"], c["queries"]) for c in chunks] == [(1, 1)]
    assert len(report.results) == 5 and report.status == "done"
    assert store.done_business_keys(report.run_id) == {f"pid:b{i}" for i in range(5)}
    assert store.list_runs()[0]["status"] == "done"


def test_interrupted_run_keeps_finished_work_and_resumes(settings, site_server):
    """Ctrl-C after the first batch: those businesses are saved; resume does the rest
    without re-crawling them or re-calling the maps API."""
    settings.batch_size = 2
    places = _places(site_server, 5)
    maps = CountingMaps(settings, places)
    verifier = StubVerifier(settings)
    seen_batches = {"n": 0}

    def interrupt_after_first_batch(event: str, data: dict) -> None:
        if event == "batch_done":
            seen_batches["n"] += 1
            if seen_batches["n"] == 1:
                raise KeyboardInterrupt

    store = Store(settings.db_path)
    with Pipeline(settings, store=store, maps=maps, verifier=verifier,
                  progress=interrupt_after_first_batch) as pipeline:
        with pytest.raises(RunStopped) as stopped:
            pipeline.run(["plumber in austin tx"])
    partial = stopped.value.report
    assert partial.status == "interrupted"
    assert len(partial.results) == 2 and partial.total == 5
    assert isinstance(stopped.value.cause, KeyboardInterrupt)
    assert store.latest_unfinished_run()["run_id"] == partial.run_id
    assert store.done_business_keys(partial.run_id) == {"pid:b0", "pid:b1"}

    crawled_before = len(verifier.calls)
    store2 = Store(settings.db_path)
    with Pipeline(settings, store=store2, maps=maps, verifier=StubVerifier(settings)) as pipeline:
        report = pipeline.run(["plumber in austin tx"], run_id=partial.run_id, resume=True)
    assert report.status == "done" and report.resumed == 2
    assert len(report.results) == 5 and report.total == 5
    assert maps.calls == ["plumber in austin tx"]              # maps served from cache on resume
    restored = [r for r in report.results if r.place.place_id in ("b0", "b1")]
    assert all(r.place.source == "db" and r.best_email for r in restored)   # loaded, not re-crawled
    assert store2.latest_unfinished_run() is None
    assert crawled_before > 0


def test_unexpected_error_is_reported_as_failed(settings, site_server, monkeypatch):
    store = Store(settings.db_path)
    pipeline = Pipeline(settings, store=store, maps=CountingMaps(settings, _places(site_server, 2)),
                        verifier=StubVerifier(settings))
    monkeypatch.setattr(pipeline, "_plan_permutations", lambda batch: (_ for _ in ()).throw(RuntimeError("boom")))
    with pipeline:
        with pytest.raises(RunStopped) as stopped:
            pipeline.run(["plumber in austin tx"])
    assert stopped.value.report.status == "failed"
    assert store.list_runs()[0]["status"] == "failed"


def test_cli_exports_partial_results_and_says_how_to_resume(settings, site_server, tmp_path, monkeypatch):
    from gmscrape import cli

    places = tmp_path / "places.csv"
    places.write_text("name,website,place_id,city,state\n" + "".join(
        f"Biz {i},{site_server}/site1/,c{i},Austin,TX\n" for i in range(4)), encoding="utf-8")
    seen = {"n": 0}
    real_make = cli._make_progress

    def make_progress(exporter=None):
        hook = real_make(exporter)

        def wrapped(event, data):
            hook(event, data)
            if event == "batch_done":
                seen["n"] += 1
                if seen["n"] == 1:
                    raise KeyboardInterrupt
        return wrapped

    monkeypatch.setattr(cli, "_make_progress", make_progress)
    monkeypatch.setattr("gmscrape.keys.interactive", lambda: False)
    args = ["run", "-y", "plumber in austin tx", "--maps-provider", "file", "--places-file", str(places),
            "--verify-provider", "local", "--batch-size", "2", "--db", str(tmp_path / "t.sqlite"),
            "-o", str(tmp_path / "out"), "--no-owners", "--no-discover"]
    monkeypatch.chdir(tmp_path)
    assert cli.main(args) == 130
    rows = list(csv.DictReader((tmp_path / "out" / "leads.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 2                                   # the finished batch is on disk

    # `scraper resume` finishes the job and the CSV now has everything.
    monkeypatch.setattr(cli, "_make_progress", real_make)
    assert cli.main(["resume", "-y", "--db", str(tmp_path / "t.sqlite"), "-o", str(tmp_path / "out")]) == 0
    rows = list(csv.DictReader((tmp_path / "out" / "leads.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 4
    assert cli.main(["runs", "--db", str(tmp_path / "t.sqlite")]) == 0


def test_table_names_typed_by_a_person():
    assert run_table_name("Austin Dentists Sept") == "austin_dentists_sept"
    assert run_table_name("2026 leads") == "run_2026_leads"
    assert run_table_name("run_2026_09_10_dentist_in_austin_tx") == "run_2026_09_10_dentist_in_austin_tx"
    assert run_table_name("Med Spa in Scottsdale, AZ · 2026-09-10") == "run_2026_09_10_med_spa_in_scottsdale_az"


def test_buddy_asks_for_the_table_name_after_the_searches(tmp_path, monkeypatch):
    from gmscrape import buddy as B
    from gmscrape import keys as K

    snapshot = dict(os.environ)
    monkeypatch.setenv("GMSCRAPE_CONFIG", str(tmp_path / "cfg" / "config.env"))
    for key in B.BUDDY_KEYS:
        monkeypatch.delenv(key.env, raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.sqlite"))
    K.save_keys({"MCP_MAPS_URL": "https://mcp.example/key", "MAILTESTER_KEY": "sub_x",
                 "SUPABASE_ACCESS_TOKEN": "sbp_x"})
    captured: dict = {}
    monkeypatch.setattr("gmscrape.cli.cmd_run", lambda args: captured.update(vars(args)) or 0)
    monkeypatch.setattr("gmscrape.banner.print_banner", lambda console=None: None)
    answers = iter(["", "", "", "", "",                 # keep every key
                    "dentist in austin tx", "",         # searches
                    "",                                 # businesses per search -> 40
                    "",                                 # time budget -> 2 h
                    "Austin Dentists Sept",             # table name
                    ""])                                # Start
    asked: list[str] = []
    try:
        assert B.buddy(lambda t: asked.append(t) or next(answers), lambda m: None) == 0
    finally:
        os.environ.clear()
        os.environ.update(snapshot)
    assert any("Supabase table" in t and "run_" in t for t in asked)   # default offered
    assert captured["supabase_table_name"] == "austin_dentists_sept"
    assert captured["queries"] == ["dentist in austin tx"]


def test_a_refused_verification_key_stops_the_run_resumably(settings, site_server):
    """A dead subscription must not burn Maps/search credits on unverifiable
    leads: the run stops, is marked failed, and can be resumed once fixed."""
    from gmscrape.core.pipeline import RunStopped
    from gmscrape.providers.base import ProviderAuthError

    class Refusing(StubVerifier):
        def verify(self, email):
            raise ProviderAuthError("MailTester Ninja rejected the API key")

    settings.batch_size = 2
    store = Store(settings.db_path)
    with Pipeline(settings, store=store, maps=CountingMaps(settings, _places(site_server, 3)),
                  verifier=Refusing(settings)) as pipeline:
        with pytest.raises(RunStopped) as stopped:
            pipeline.run(["plumber in austin tx"])
    assert isinstance(stopped.value.cause, ProviderAuthError)
    run = store.list_runs()[0]
    assert run["status"] == "failed" and store.latest_unfinished_run()["run_id"] == run["run_id"]


def test_cli_checks_the_verification_key_before_spending(monkeypatch, tmp_path, site_server):
    from gmscrape import cli
    from gmscrape.providers.base import ProviderAuthError

    class Refusing(StubVerifier):
        requires_key = True

        def preflight(self):
            raise ProviderAuthError("MailTester Ninja rejected the API key (HTTP 401)")

    maps = CountingMaps(Settings.from_env(), _places(site_server, 2))
    monkeypatch.setattr(cli, "get_verifier", lambda settings, name=None: Refusing(settings))
    monkeypatch.setattr("gmscrape.core.pipeline.get_verifier", lambda settings, name=None: Refusing(settings))
    monkeypatch.setattr("gmscrape.core.pipeline.get_maps_provider", lambda settings, name=None: maps)
    monkeypatch.setattr("gmscrape.keys.interactive", lambda: False)
    said: list[str] = []
    monkeypatch.setattr(cli, "echo", lambda text="", *a, **k: said.append(str(text)))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GMSCRAPE_CONFIG", str(tmp_path / "cfg.env"))
    monkeypatch.setenv("MAILTESTER_KEY", "sub_dead")
    places = tmp_path / "places.csv"
    places.write_text("name,website\n", encoding="utf-8")
    code = cli.main(["run", "plumber in austin tx", "-y", "--maps-provider", "file",
                     "--places-file", str(places),
                     "--db", str(tmp_path / "t.sqlite"), "-o", str(tmp_path / "out")])
    assert code == 2
    assert any("rejected the API key" in t for t in said) and any("Nothing was spent" in t for t in said)
    assert maps.calls == []                                   # not one Maps credit spent


def test_a_search_that_found_nothing_is_asked_again_next_run(settings, site_server):
    """An empty Maps answer (throttling, a hiccup) must not be remembered as
    final for a week: the next run asks again, and old cached zeros are dropped."""
    class Flaky(CountingMaps):
        def search(self, spec, limit):
            if not self.calls:
                self.calls.append(spec.search_string)
                return iter(())                      # first time: nothing
            return super().search(spec, limit)       # (the parent records the call)

    maps = Flaky(settings, _places(site_server, 2))
    store = Store(settings.db_path)
    with Pipeline(settings, store=store, maps=maps, verifier=StubVerifier(settings)) as pipeline:
        first = pipeline.run(["plumber in banning ca"])
    assert first.done == 0 and first.empty_queries == ["plumber in banning ca"]
    assert first.stats()["searches_with_no_results"] == 1
    with Pipeline(settings, store=Store(settings.db_path), maps=maps, verifier=StubVerifier(settings)) as pipeline:
        second = pipeline.run(["plumber in banning ca"])
    assert maps.calls == ["plumber in banning ca"] * 2 and second.done == 2

    # A zero remembered as final by an older version is forgotten on open.
    store.put_maps("counting", "hotels in banning ca", [], complete=True)
    store.conn.commit()
    reopened = Store(settings.db_path)
    assert reopened.get_maps("counting", "hotels in banning ca", 168) is None
