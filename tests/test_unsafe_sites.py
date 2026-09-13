"""A run cut off by the computer never visits the same website again."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from gmscrape import cli
from gmscrape.core.pipeline import Pipeline
from gmscrape.providers.maps.file_provider import FileMaps
from gmscrape.web import unsafe
from gmscrape.web.fetch import Fetcher
from gmscrape.web.unsafe import Inflight, add_blocked, load_blocked, quarantine_leftovers

from conftest import StubVerifier


def test_inflight_mirrors_the_sites_on_the_wire_and_vanishes_on_a_clean_exit(tmp_path):
    tracker = Inflight(tmp_path / "inflight.json", interval=0.05)
    a = tracker.enter("https://shop.acme.co.uk/contact")
    b = tracker.enter("http://127.0.0.1:8080/x")
    assert (a, b) == ("acme.co.uk", "127.0.0.1")
    tracker.enter("https://www.acme.co.uk/")          # same site twice: one entry
    tracker.flush()
    assert json.loads((tmp_path / "inflight.json").read_text())["domains"] == ["127.0.0.1", "acme.co.uk"]
    tracker.leave(a)
    tracker.leave(a)
    tracker.flush()
    assert json.loads((tmp_path / "inflight.json").read_text())["domains"] == ["127.0.0.1"]
    tracker.close()
    assert not (tmp_path / "inflight.json").exists()


def test_leftover_inflight_file_becomes_the_skip_list(tmp_path):
    assert quarantine_leftovers(tmp_path) == []                      # nothing left behind: nothing to do
    (tmp_path / "inflight.json").write_text(json.dumps({"at": 1, "domains": ["bad-site.com", "Other.NET"]}))
    assert quarantine_leftovers(tmp_path) == ["bad-site.com", "other.net"]
    assert not (tmp_path / "inflight.json").exists()
    assert load_blocked(tmp_path) == {"bad-site.com", "other.net"}
    text = (tmp_path / "blocked_sites.txt").read_text()
    assert text.startswith("#") and "bad-site.com\n" in text
    # A second cut-off appends only what is new; comments and blanks are ignored.
    assert add_blocked(tmp_path, ["other.net", "https://www.third.org/page"]) == ["third.org"]
    assert load_blocked(tmp_path) == {"bad-site.com", "other.net", "third.org"}


def test_fetcher_reports_what_it_is_visiting(settings, site_server, monkeypatch):
    tracker = Inflight(None)
    monkeypatch.setattr(unsafe, "current", tracker)
    seen: list[list[str]] = []

    async def go():
        async with Fetcher(settings) as fetcher:
            original = fetcher._get_with_retries

            async def spy(url):
                seen.append(tracker.domains())
                return await original(url)

            fetcher._get_with_retries = spy
            page = await fetcher.get(f"{site_server}/site1/")
            assert page.ok
    asyncio.run(go())
    assert seen == [["127.0.0.1"]]
    assert tracker.domains() == []                                   # left once the page was back


def test_blocked_sites_are_not_crawled_but_the_business_is_still_processed(tmp_path, settings, site_server):
    from test_pipeline_e2e import _places_file

    settings.places_file = str(_places_file(tmp_path, site_server))
    settings.crawl_websites = True
    out_dir = Path(settings.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    add_blocked(out_dir, [site_server])                              # every fake site lives on this host
    pipeline = Pipeline(settings, maps=FileMaps(settings), verifier=StubVerifier(settings))
    with pipeline:
        run = pipeline.run(["*"])
    joe = next(r for r in run.results if "Joe's Plumbing" in r.place.name)
    assert joe.website_status == "skipped:unsafe_site"
    assert "site_skipped:flagged_unsafe" in joe.notes
    assert not joe.pages_crawled and not joe.found_emails
    assert run.done == run.total                                     # the run still finished


def test_supervisor_quarantines_before_a_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "echo", lambda *a, **k: None)
    (tmp_path / "inflight.json").write_text(json.dumps({"at": 1, "domains": ["bad-site.com"]}))
    codes = iter([-9, 0])
    blocked_at_restart: list[set[str]] = []

    def child(argv, console_path=None):
        code = next(codes)
        if argv[0] == "resume":
            blocked_at_restart.append(load_blocked(tmp_path))
        return code

    args = argparse.Namespace(_argv=["run", "-y", "x"], env_file=None, db_path=None, log_level=None,
                              out_dir=str(tmp_path), basename="leads", no_supervise=False)
    assert cli.supervise(args, run_child=child, pause=0, console_path=str(tmp_path / "console.txt")) == 0
    assert blocked_at_restart == [{"bad-site.com"}]
    assert not (tmp_path / "inflight.json").exists()


def test_buddy_resume_is_supervised(monkeypatch, tmp_path):
    import os

    from gmscrape import buddy
    from gmscrape.store.db import Store

    snapshot = dict(os.environ)
    monkeypatch.setenv("GMSCRAPE_CONFIG", str(tmp_path / "cfg" / "config.env"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "b.sqlite"))
    monkeypatch.chdir(tmp_path)
    try:
        with Store(str(tmp_path / "b.sqlite")) as store:
            store.start_run("r1", ["plumber in austin tx"], {})
            store.set_run_state("r1", "interrupted", total=10, done=3)
        captured = {}

        def fake_resume(args):
            captured["argv"] = getattr(args, "_argv", None)
            return 0
        monkeypatch.setattr("gmscrape.cli.cmd_resume", fake_resume)
        monkeypatch.setattr("gmscrape.banner.print_banner", lambda console=None: None)
        assert buddy.buddy(prompt=lambda q: "y", echo=lambda *a: None) == 0
        assert captured["argv"] == ["resume", "-y", "r1"]
    finally:
        os.environ.clear()
        os.environ.update(snapshot)
