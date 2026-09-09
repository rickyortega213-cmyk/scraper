"""Supabase live sink, against a fake PostgREST server.

The interesting behaviour is the live part: rows must appear as soon as the
businesses are known and then be updated in place as each stage completes, and
a broken database must never take a scrape down with it.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import pytest

from gmscrape.core.pipeline import Pipeline
from gmscrape.models import BusinessResult, EmailCandidate, Place, SOURCE_MAILTO
from gmscrape.providers.maps.file_provider import FileMaps
from gmscrape.store.db import Store
from gmscrape.store.sinks import STATUS_DONE, STATUS_QUEUED, NullSink, lead_record
from gmscrape.store.supabase import (
    SupabaseConfig,
    SupabaseError,
    SupabaseSink,
    check_connection,
    schema_sql,
)

from conftest import StubVerifier

SERVICE_KEY = "service-role-test-key"


class _PostgREST(http.server.BaseHTTPRequestHandler):
    """Enough of PostgREST to be meaningful: upsert-by-key into memory."""

    tables: dict[str, dict[str, dict]] = {}
    history: list[tuple[str, str]] = []        # (table, id) write order
    status_history: dict[str, list[str]] = {}  # lead id -> statuses seen
    reject: dict[str, int] = {}               # table -> status code to return

    @classmethod
    def reset(cls) -> None:
        cls.tables = {"gmscrape_runs": {}, "gmscrape_leads": {}, "gmscrape_emails": {}}
        cls.history = []
        cls.status_history = {}
        cls.reject = {}

    def _table(self) -> str:
        return self.path.split("?")[0].rsplit("/", 1)[-1]

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        table = self._table()
        if self.headers.get("apikey") != SERVICE_KEY:
            return self._send(401, {"message": "invalid api key"})
        if table in self.reject:
            return self._send(self.reject[table], {"message": "boom"})
        if table not in self.tables:
            return self._send(404, {"message": f"relation {table} does not exist"})
        length = int(self.headers.get("Content-Length", 0))
        rows = json.loads(self.rfile.read(length) or b"[]")
        assert isinstance(rows, list), "PostgREST upserts are sent as arrays"
        keys = [r.get("id") or r.get("run_id") for r in rows]
        assert len(keys) == len(set(keys)), f"duplicate keys in one batch: {keys}"
        for row in rows:
            key = str(row.get("id") or row.get("run_id"))
            self.tables[table][key] = {**self.tables[table].get(key, {}), **row}
            self.history.append((table, key))
            if table == "gmscrape_leads":
                self.status_history.setdefault(key, []).append(row.get("status", ""))
        return self._send(201, [])

    def do_GET(self) -> None:  # noqa: N802
        table = self._table()
        if self.headers.get("apikey") != SERVICE_KEY:
            return self._send(401, {"message": "invalid api key"})
        if table not in self.tables:
            return self._send(404, {"message": f"relation {table} does not exist"})
        return self._send(200, list(self.tables[table].values())[:1])

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def supabase() -> str:
    _PostgREST.reset()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _PostgREST)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def config_for(url: str, key: str = SERVICE_KEY) -> SupabaseConfig:
    return SupabaseConfig(url=url, key=key)


def _result(name: str = "Joe's Plumbing", place_id: str = "p1") -> BusinessResult:
    return BusinessResult(
        place=Place(name=name, place_id=place_id, website="https://joes.com",
                    domain="joes.com", city="Austin", state="TX", reviews=87),
        emails=[EmailCandidate(email="info@joes.com", source=SOURCE_MAILTO, confidence=88)],
        website_status="ok",
    )


# --- sink mechanics --------------------------------------------------------
def test_writes_leads_and_emails(supabase):
    with SupabaseSink(config_for(supabase)) as sink:
        sink.start_run("run1", {"queries": ["plumber in austin tx"]})
        sink.upsert([_result()], STATUS_DONE)
        sink.flush()

        leads = _PostgREST.tables["gmscrape_leads"]
        assert list(leads) == ["pid:p1"]
        assert leads["pid:p1"]["name"] == "Joe's Plumbing"
        assert leads["pid:p1"]["best_email"] == "info@joes.com"
        assert leads["pid:p1"]["status"] == STATUS_DONE
        assert leads["pid:p1"]["updated_at"]

        assert list(_PostgREST.tables["gmscrape_emails"]) == ["pid:p1|info@joes.com"]
        assert _PostgREST.tables["gmscrape_runs"]["run1"]["queries"] == [
            "plumber in austin tx"
        ]
        assert sink.stats.leads_written == 1 and sink.stats.failures == 0


def test_flush_means_persisted(supabase):
    """flush() must not return before the rows have actually been written."""
    with SupabaseSink(config_for(supabase)) as sink:
        sink.start_run("run1", {})
        for index in range(5):
            sink.upsert([_result(f"Biz {index}", f"p{index}")], STATUS_QUEUED)
        sink.flush()
        assert len(_PostgREST.tables["gmscrape_leads"]) == 5


def test_rerunning_updates_the_same_row(supabase):
    with SupabaseSink(config_for(supabase)) as sink:
        sink.start_run("run1", {})
        sink.upsert([_result()], STATUS_QUEUED)
        sink.flush()
        sink.upsert([_result()], STATUS_DONE)
        sink.flush()
    assert len(_PostgREST.tables["gmscrape_leads"]) == 1
    assert _PostgREST.status_history["pid:p1"] == [STATUS_QUEUED, STATUS_DONE]


def test_duplicate_ids_in_one_batch_are_collapsed(supabase):
    """PostgREST rejects a batch containing the same key twice."""
    with SupabaseSink(config_for(supabase)) as sink:
        sink.start_run("run1", {})
        # Same business queued twice before any flush - the fake server asserts
        # on duplicate keys, so this passing is the guarantee.
        sink.upsert([_result(), _result()], STATUS_QUEUED)
        sink.flush()
    assert len(_PostgREST.tables["gmscrape_leads"]) == 1
    assert sink.stats.failures == 0


def test_a_broken_database_never_raises(supabase):
    _PostgREST.reject["gmscrape_leads"] = 500
    with SupabaseSink(config_for(supabase)) as sink:
        sink.start_run("run1", {})
        sink.upsert([_result()], STATUS_DONE)
        sink.flush()
    assert sink.stats.failures >= 1
    assert "HTTP 500" in sink.stats.last_error
    assert sink.stats.leads_written == 0


def test_an_unreachable_host_never_raises():
    sink = SupabaseSink(config_for("http://127.0.0.1:1"))   # nothing listening
    sink.start_run("run1", {})
    sink.upsert([_result()], STATUS_DONE)
    sink.flush()
    sink.close()
    assert sink.stats.failures >= 1 and sink.stats.leads_written == 0


# --- connectivity check ----------------------------------------------------
def test_check_connection_reports_ok(supabase):
    assert check_connection(config_for(supabase)) == {
        "gmscrape_runs": "ok", "gmscrape_leads": "ok", "gmscrape_emails": "ok",
    }


def test_check_connection_explains_a_bad_key(supabase):
    with pytest.raises(SupabaseError, match="service_role"):
        check_connection(config_for(supabase, key="wrong"))


def test_check_connection_explains_missing_tables(supabase):
    with pytest.raises(SupabaseError, match="supabase-init"):
        check_connection(SupabaseConfig(url=supabase, key=SERVICE_KEY, prefix="other_"))


def test_schema_sql_honours_the_prefix():
    sql = schema_sql("leads_")
    assert "create table if not exists leads_leads" in sql
    assert "create or replace view leads_table" in sql
    assert "enable row level security" in sql


class RecordingSink:
    """Captures every publish so stage order can be asserted without timing."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.flushes = 0
        self.finished = False

    def start_run(self, run_id: str, meta: dict) -> None:
        self.calls.append(("start_run", [run_id]))

    def upsert(self, results, status: str) -> None:
        self.calls.append((status, [r.place.dedupe_key() for r in results]))

    def flush(self) -> None:
        self.flushes += 1

    def finish_run(self, run_id: str, stats: dict) -> None:
        self.finished = True

    def close(self) -> None:
        return

    @property
    def statuses(self) -> list[str]:
        return [status for status, _ in self.calls if status != "start_run"]


def test_publishes_every_stage(tmp_path, settings, site_server):
    """The pipeline must report progress at each stage, in order."""
    places = tmp_path / "places.csv"
    places.write_text(
        "name,website,place_id,category\n"
        f"Joe's Plumbing,{site_server}/site1/,s1,Plumber\n"
        "Bluebonnet Roofing,https://bluebonnet-roofing-9x7q2z.com,s2,Roofing contractor\n",
        encoding="utf-8",
    )
    settings.places_file = str(places)
    sink = RecordingSink()
    store = Store(settings.db_path)
    with Pipeline(settings, store=store, maps=FileMaps(settings),
                  verifier=StubVerifier(settings), sinks=[sink]) as pipeline:
        pipeline.run(["*"])

    statuses = sink.statuses
    assert statuses[0] == "queued", "leads must be published before the crawl"
    assert statuses[-1] == "done"
    for stage in ("crawled", "guessed", "verified"):
        assert stage in statuses, f"{stage} was never published"
    # queued is flushed straight away so the table is not empty while crawling.
    assert sink.flushes >= 1
    assert sink.finished


# --- the live behaviour, through a real pipeline run -----------------------
def test_pipeline_streams_status_progression(tmp_path, settings, site_server, supabase):
    """Rows appear before the crawl and are updated as each stage completes."""
    import csv

    rows = [
        {"name": "Joe's Plumbing & Heating", "website": f"{site_server}/site1/",
         "category": "Plumber", "place_id": "s1", "reviews": "87"},
        {"name": "Bluebonnet Roofing", "website": "https://bluebonnet-roofing-9x7q2z.com",
         "category": "Roofing contractor", "place_id": "s2", "reviews": "35"},
    ]
    places = tmp_path / "places.csv"
    with places.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    settings.places_file = str(places)
    sink = SupabaseSink(config_for(supabase))
    store = Store(settings.db_path)
    with Pipeline(settings, store=store, maps=FileMaps(settings),
                  verifier=StubVerifier(settings), sinks=[sink]) as pipeline:
        report = pipeline.run(["*"])

    leads = _PostgREST.tables["gmscrape_leads"]
    assert set(leads) == {"pid:s1", "pid:s2"}

    # Rows land as `queued` before any slow work and finish as `done`. Stages
    # in between may coalesce into one write on a fast run, which is fine - the
    # publish contract itself is asserted in test_publishes_every_stage.
    for history in _PostgREST.status_history.values():
        assert history[0] == STATUS_QUEUED
        assert history[-1] == STATUS_DONE

    joe = leads["pid:s1"]
    assert joe["best_email"] == "office@joesplumbing.com"
    assert joe["website_status"] == "ok"
    assert joe["emails_found"] >= 1

    roofing = leads["pid:s2"]
    assert roofing["best_email"].startswith("info@")
    assert roofing["emails_guessed"] >= 1

    emails = _PostgREST.tables["gmscrape_emails"]
    assert any(e["email"] == "office@joesplumbing.com" for e in emails.values())
    assert all(e["lead_id"] in leads for e in emails.values())

    run_row = _PostgREST.tables["gmscrape_runs"][report.run_id]
    assert run_row["finished_at"] and run_row["stats"]["businesses"] == 2
    assert report.sinks and report.sinks[0].stats.failures == 0


def test_pipeline_completes_when_supabase_is_down(tmp_path, settings, site_server):
    """A dead live table must not cost you the run's results."""
    places = tmp_path / "places.csv"
    places.write_text(
        "name,website,place_id\n"
        f"Joe's Plumbing,{site_server}/site1/,s1\n",
        encoding="utf-8",
    )
    settings.places_file = str(places)
    sink = SupabaseSink(config_for("http://127.0.0.1:1"))
    store = Store(settings.db_path)
    with Pipeline(settings, store=store, maps=FileMaps(settings),
                  verifier=StubVerifier(settings), sinks=[sink]) as pipeline:
        report = pipeline.run(["*"])

    assert len(report.results) == 1
    assert report.results[0].best_email is not None      # the scrape still worked
    assert sink.stats.failures >= 1


def test_null_sink_is_the_default(tmp_path, settings):
    store = Store(settings.db_path)
    settings.places_file = str(tmp_path / "empty.csv")
    (tmp_path / "empty.csv").write_text("name,website\n", encoding="utf-8")
    with Pipeline(settings, store=store, maps=FileMaps(settings)) as pipeline:
        assert isinstance(pipeline.sinks[0], NullSink)
        report = pipeline.run(["*"])
    assert report.sinks == []


def test_lead_record_blanks_become_nulls():
    record = lead_record(_result(), "run1", STATUS_QUEUED)
    assert record["postal_code"] is None      # empty string would break numeric/text nulls
    assert record["reviews"] == 87
    assert record["is_chain"] is False
