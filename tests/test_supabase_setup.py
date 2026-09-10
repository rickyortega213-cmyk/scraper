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
