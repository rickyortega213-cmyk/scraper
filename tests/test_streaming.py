"""Large runs: queries stream through in chunks, memory stays flat, resume skips whole queries."""

from __future__ import annotations

import csv

import pytest

from gmscrape.core.pipeline import Pipeline, RunStopped
from gmscrape.models import Place, QuerySpec
from gmscrape.providers.base import MapsProvider
from gmscrape.store.db import Store

from conftest import StubVerifier


class ManyQueriesMaps(MapsProvider):
    """Two distinct businesses per query, one of them shared with the next query."""

    name = "many"
    requires_key = False

    def __init__(self, settings, site: str) -> None:
        super().__init__(settings)
        self.site = site
        self.calls: list[str] = []

    def search(self, spec: QuerySpec, limit: int):
        self.calls.append(spec.search_string)
        n = int(spec.location.split()[-1]) if spec.location.split()[-1].isdigit() else 0
        for i in (n, n + 1):                      # query n and n+1 overlap on business n+1
            yield Place(name=f"Biz {i}", place_id=f"p{i}", website=f"{self.site}/site1/",
                        domain="joesplumbing.com", city="Austin", state="TX", category="Plumber",
                        query=spec.search_string)


QUERIES = [f"plumber in area {i}" for i in range(6)]


def test_queries_stream_in_chunks_and_dedupe_across_chunks(settings, site_server):
    settings.query_chunk_size = 2
    settings.batch_size = 2
    events: list[dict] = []
    maps = ManyQueriesMaps(settings, site_server)
    with Pipeline(settings, store=Store(settings.db_path), maps=maps, verifier=StubVerifier(settings),
                  progress=lambda e, d: events.append({"event": e, **d})) as pipeline:
        report = pipeline.run(QUERIES)
    assert report.status == "done"
    assert report.done == 7                                   # Biz 0..6, overlaps removed
    assert len(maps.calls) == 6
    chunks = [e for e in events if e["event"] == "chunk_done"]
    assert [c["queries_done"] for c in chunks] == [2, 4, 6]
    first_batch = next(e for e in events if e["event"] == "batch_done")
    assert first_batch["queries_done"] == 0 and first_batch["done"] == 2   # leads before all queries ran
    assert report.stats()["businesses"] == 7 and report.stats()["queries_done"] == 6


def test_lean_mode_keeps_a_sample_but_counts_everything(settings, site_server):
    settings.lean_memory = True
    settings.batch_size = 2
    settings.query_chunk_size = 3
    maps = ManyQueriesMaps(settings, site_server)
    with Pipeline(settings, store=Store(settings.db_path), maps=maps, verifier=StubVerifier(settings)) as pipeline:
        pipeline.settings.cache_http = True
        report = pipeline.run(QUERIES)
    assert report.lean and pipeline.settings.cache_http is False    # page cache off for big runs
    assert report.done == 7 and report.stats()["businesses"] == 7
    assert all(r.place.raw == {} for r in report.results)          # heavy payloads dropped
    assert 0 < len(report.results) <= report.sample_size


def test_counters_match_between_modes(settings, site_server):
    settings.batch_size = 3
    with Pipeline(settings, store=Store(settings.db_path), maps=ManyQueriesMaps(settings, site_server),
                  verifier=StubVerifier(settings)) as pipeline:
        normal = pipeline.run(QUERIES).stats()
    settings.lean_memory = True
    settings.db_path = settings.db_path + ".lean"
    with Pipeline(settings, store=Store(settings.db_path), maps=ManyQueriesMaps(settings, site_server),
                  verifier=StubVerifier(settings)) as pipeline:
        lean = pipeline.run(QUERIES).stats()
    for key in ("businesses", "with_any_email", "lead_rows", "best_email_verified_valid", "total_emails"):
        assert normal[key] == lean[key], key


def test_resume_skips_finished_queries_and_businesses(settings, site_server):
    settings.query_chunk_size = 2
    settings.batch_size = 10
    maps = ManyQueriesMaps(settings, site_server)
    chunks = {"n": 0}

    def stop_after_first_chunk(event: str, data: dict) -> None:
        if event == "chunk_done":
            chunks["n"] += 1
            if chunks["n"] == 1:
                raise KeyboardInterrupt

    store = Store(settings.db_path)
    with Pipeline(settings, store=store, maps=maps, verifier=StubVerifier(settings),
                  progress=stop_after_first_chunk) as pipeline:
        with pytest.raises(RunStopped) as stopped:
            pipeline.run(QUERIES)
    partial = stopped.value.report
    assert partial.queries_done == 2 and partial.done == 3          # Biz 0,1,2
    assert store.done_queries(partial.run_id) == set(QUERIES[:2])

    maps.calls.clear()
    with Pipeline(settings, store=Store(settings.db_path), maps=maps, verifier=StubVerifier(settings)) as pipeline:
        report = pipeline.run(QUERIES, run_id=partial.run_id, resume=True)
    assert report.status == "done"
    assert maps.calls == QUERIES[2:]                                  # finished queries not re-fetched
    assert report.resumed == 3 and report.done == 7
    assert report.stats()["with_any_email"] == 7


def test_lean_run_exports_by_appending(settings, site_server, tmp_path, monkeypatch):
    """The CSV is written once per batch and never rewritten; resume rebuilds it from the DB."""
    from gmscrape import cli

    places = tmp_path / "places.csv"
    places.write_text("name,website,place_id,city,state\n" + "".join(
        f"Biz {i},{site_server}/site1/,c{i},Austin,TX\n" for i in range(5)), encoding="utf-8")
    monkeypatch.setattr("gmscrape.keys.interactive", lambda: False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LEAN_MEMORY", "true")
    args = ["run", "-y", "plumber in austin tx", "--maps-provider", "file", "--places-file", str(places),
            "--verify-provider", "local", "--batch-size", "2", "--db", str(tmp_path / "t.sqlite"),
            "-o", str(tmp_path / "out"), "--no-owners", "--no-discover", "--format", "csv,json"]
    assert cli.main(args) == 0
    rows = list(csv.DictReader((tmp_path / "out" / "leads.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 5 and list(rows[0])[:3] == ["company_name", "city", "state"]
    assert not (tmp_path / "out" / "leads.json").exists()          # no full-memory formats in lean mode
