"""Supabase made automatic: on when keys exist, tables created on first use."""

from __future__ import annotations

import http.server
import json
import socket
import threading

import pytest

from gmscrape.config import Settings
from gmscrape.store.supabase import (
    SupabaseConfig, SupabaseError, ensure_schema, missing_tables, run_label,
)

SERVICE_KEY = "service-role-test-key"
ACCESS_TOKEN = "sbp_test_token"


class _Cloud(http.server.BaseHTTPRequestHandler):
    """One server playing both PostgREST (tables) and the Management API."""

    tables: set[str] = set()
    sql_received: list[str] = []
    token_required = ACCESS_TOKEN

    def do_GET(self) -> None:  # noqa: N802
        table = self.path.split("?")[0].rsplit("/", 1)[-1]
        if self.headers.get("apikey") != SERVICE_KEY:
            return self._send(401, {"message": "bad key"})
        if table in self.tables:
            return self._send(200, [])
        return self._send(404, {"message": f"relation {table} does not exist"})

    def do_POST(self) -> None:  # noqa: N802
        if "/database/query" in self.path:
            if self.headers.get("Authorization") != f"Bearer {self.token_required}":
                return self._send(401, {"message": "unauthorized"})
            length = int(self.headers.get("Content-Length", 0))
            sql = json.loads(self.rfile.read(length))["query"]
            self.sql_received.append(sql)
            for table in ("gmscrape_runs", "gmscrape_leads", "gmscrape_emails"):
                if f"create table if not exists {table}" in sql:
                    self.tables.add(table)
            return self._send(201, [])
        return self._send(404, {})

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
def cloud(monkeypatch):
    _Cloud.tables = set()
    _Cloud.sql_received = []
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Cloud)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr("gmscrape.store.supabase.MANAGEMENT_API", base + "/v1/projects/{ref}/database/query")
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


def _config(base: str, token: str = "") -> SupabaseConfig:
    config = SupabaseConfig(url=base, key=SERVICE_KEY, access_token=token)
    return config


def test_tables_are_created_on_first_use_with_an_access_token(cloud, monkeypatch):
    config = _config(cloud, ACCESS_TOKEN)
    monkeypatch.setattr(SupabaseConfig, "project_ref", property(lambda self: "abcdefgh"))
    assert missing_tables(config) == ["gmscrape_runs", "gmscrape_leads", "gmscrape_emails"]
    ready, detail = ensure_schema(config)
    assert ready and detail.startswith("created ")
    assert len(_Cloud.sql_received) == 1 and "create or replace view gmscrape_latest" in _Cloud.sql_received[0]
    assert ensure_schema(config) == (True, "tables present")      # idempotent, no second call
    assert len(_Cloud.sql_received) == 1


def test_without_a_token_it_explains_where_to_paste(cloud):
    ready, detail = ensure_schema(_config(cloud))
    assert not ready
    assert "tables missing" in detail and "gmscrape supabase-init" in detail
    assert _Cloud.sql_received == []


def test_a_bad_access_token_is_a_clear_error(cloud, monkeypatch):
    monkeypatch.setattr(SupabaseConfig, "project_ref", property(lambda self: "abcdefgh"))
    with pytest.raises(SupabaseError, match="Management API refused"):
        ensure_schema(_config(cloud, "sbp_wrong"))


def test_dashboard_urls_from_project_url():
    config = SupabaseConfig(url="https://abcdefghijkl.supabase.co", key="k")
    assert config.project_ref == "abcdefghijkl"
    assert config.table_editor_url == "https://supabase.com/dashboard/project/abcdefghijkl/editor"
    assert config.sql_editor_url.endswith("/sql/new")
    assert SupabaseConfig(url="http://127.0.0.1:5", key="k").table_editor_url == ""


def test_run_label_names_the_run():
    label = run_label(["dentist in austin tx", "plumber in miami fl"])
    assert label.startswith("dentist in austin tx +1 more · 20")
    assert run_label(["*"]).startswith("enrich · ")


def test_supabase_is_on_whenever_keys_exist_and_off_by_flag():
    from gmscrape.cli import build_parser, settings_from_args

    settings = Settings.from_env(supabase_url="https://x.supabase.co", supabase_key="k")
    assert settings.supabase and settings.supabase_configured
    assert not Settings.from_env().supabase_configured
    args = build_parser().parse_args(["run", "x", "--no-supabase"])
    assert settings_from_args(args).supabase is False


# --- one credential: the access token ------------------------------------------
class _Management(http.server.BaseHTTPRequestHandler):
    """Fake Management API: projects, api-keys, and SQL execution."""

    projects = [{"id": "abcdefghijkl", "name": "leads", "region": "us-east-1"}]
    keys = [{"name": "anon", "api_key": "anon-key"}, {"name": "service_role", "api_key": "svc-key"}]
    sql: list[str] = []
    token = "sbp_good"

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            return self._send(401, {"message": "unauthorized"})
        if self.path.startswith("/v1/projects/") and "api-keys" in self.path:
            return self._send(200, self.keys)
        if self.path.startswith("/v1/projects"):
            return self._send(200, self.projects)
        return self._send(404, {})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.sql.append(json.loads(self.rfile.read(length))["query"])
        return self._send(201, [])

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
def management(monkeypatch):
    _Management.sql = []
    _Management.projects = [{"id": "abcdefghijkl", "name": "leads", "region": "us-east-1"}]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Management)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr("gmscrape.store.supabase.MANAGEMENT_PROJECTS", base + "/v1/projects")
    monkeypatch.setattr("gmscrape.store.supabase.MANAGEMENT_KEYS", base + "/v1/projects/{ref}/api-keys")
    monkeypatch.setattr("gmscrape.store.supabase.MANAGEMENT_API", base + "/v1/projects/{ref}/database/query")
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


def test_everything_is_derived_from_the_token(management):
    from gmscrape.store.supabase import resolve_from_token

    config = resolve_from_token("sbp_good")
    assert config.url == "https://abcdefghijkl.supabase.co"
    assert config.key == "svc-key"                     # service_role, never anon
    assert config.access_token == "sbp_good"
    assert config.project_ref == "abcdefghijkl"


def test_bad_token_and_several_projects_are_explained(management):
    from gmscrape.store.supabase import resolve_from_token

    with pytest.raises(SupabaseError, match="rejected the access token"):
        resolve_from_token("sbp_wrong")
    _Management.projects.append({"id": "mnopqrstuvwx", "name": "other"})
    with pytest.raises(SupabaseError, match="SUPABASE_PROJECT_REF"):
        resolve_from_token("sbp_good")
    assert resolve_from_token("sbp_good", "mnopqrstuvwx").project_ref == "mnopqrstuvwx"


def test_run_table_names():
    from gmscrape.store.supabase import run_table_name

    assert run_table_name("dentist in austin tx · 2026-09-10") == "run_2026_09_10_dentist_in_austin_tx"
    assert run_table_name("dentist in austin tx +2 more · 2026-09-10") == "run_2026_09_10_dentist_in_austin_tx"
    assert run_table_name("Med Spa in Scottsdale, AZ · 2026-09-10") == "run_2026_09_10_med_spa_in_scottsdale_az"
    assert len(run_table_name("x" * 200 + " · 2026-09-10")) <= 60


def test_a_fresh_table_is_created_for_the_run(cloud, monkeypatch):
    """Shared tables plus the per-run table, in one SQL call through the token."""
    from gmscrape.store.supabase import run_table_sql

    monkeypatch.setattr(SupabaseConfig, "project_ref", property(lambda self: "abcdefghijkl"))
    config = SupabaseConfig(url=cloud, key=SERVICE_KEY, access_token=ACCESS_TOKEN)
    ready, detail = ensure_schema(config, run_table="run_2026_09_10_dentist_in_austin_tx")
    assert ready and "created run_2026_09_10_dentist_in_austin_tx" in detail
    sql = _Cloud.sql_received[0]
    assert "create table if not exists run_2026_09_10_dentist_in_austin_tx" in sql
    assert "company_name" in run_table_sql("t") and "verified_email" in run_table_sql("t")


def test_sink_streams_clean_rows_into_the_run_table(monkeypatch):
    """Rows written to the per-run table are the clean, human-facing shape."""
    import gmscrape.store.supabase as S
    from gmscrape.models import (
        BusinessResult, CONTACT_OWNER, EmailCandidate, Place, SOURCE_MAILTO, VerificationResult, V_VALID,
    )

    written: dict[str, list] = {}

    class Fake:
        def post(self, url, params=None, json=None, headers=None):
            written.setdefault(url.rsplit("/", 1)[-1], []).extend(json)
            return type("R", (), {"status_code": 201, "text": ""})()

        def close(self):
            return None

    config = SupabaseConfig(url="https://abc.supabase.co", key="k", access_token="sbp")
    sink = S.SupabaseSink(config, client=Fake(), run_table="run_2026_09_10_dentist_in_austin_tx")
    result = BusinessResult(
        place=Place(name="JOE'S PLUMBING", place_id="p1", city="austin", state="tx",
                    phone="5125550100", category="plumber", query="plumber in austin tx"),
        emails=[EmailCandidate(email="joe@joes.com", source=SOURCE_MAILTO, confidence=90,
                               contact_type=CONTACT_OWNER, contact_name="Joe Smith", contact_title="owner",
                               verification=VerificationResult(status=V_VALID, provider="t"))],
    )
    sink.start_run("r1", {"queries": ["plumber in austin tx"]})
    sink.upsert([result], "done")
    sink.flush()
    sink.close()
    rows = written["run_2026_09_10_dentist_in_austin_tx"]
    assert len(rows) == 1
    row = rows[0]
    assert row["company_name"] == "Joe's Plumbing" and row["phone_number"] == "(512)-555-0100"
    assert (row["contact_first_name"], row["contact_last_name"]) == ("Joe", "Smith")
    assert row["verified_email"] == "joe@joes.com" and row["status"] == "done"
    assert row["city"] == "Austin" and row["state"] == "TX" and row["contact_type"] == "Owner"
    assert "gmscrape_leads" in written                    # the shared table still gets everything
    assert sink.stats.run_rows_written == 1


def test_token_alone_counts_as_configured():
    assert Settings.from_env(supabase_access_token="sbp_x").supabase_configured
    assert not Settings.from_env().supabase_configured


def test_buddy_asks_only_for_the_token():
    from gmscrape.buddy import BUDDY_KEYS

    supabase = [k for k in BUDDY_KEYS if "supabase" in k.env.lower()]
    assert [k.env for k in supabase] == ["SUPABASE_ACCESS_TOKEN"]
