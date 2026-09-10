"""Scale and failure hardening: nothing wedges, nothing wrong gets remembered."""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time

import httpx
import pytest

from gmscrape.config import Settings
from gmscrape.models import VerificationResult, V_UNKNOWN, V_VALID
from gmscrape.store.db import Store
from gmscrape.store.supabase import (
    SupabaseConfig, SupabaseSink, _statements, ensure_schema, exec_function_sql,
    runner_installed,
)


# --- Supabase through the project key ------------------------------------------
class _Project(http.server.BaseHTTPRequestHandler):
    """PostgREST with the gmscrape_exec runner installed (or not)."""

    runner = True
    tables: set[str] = set()
    sql: list[str] = []
    key = "sb_secret_testkey"

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("apikey") != self.key:
            return self._send(401, {"message": "bad key"})
        table = self.path.split("?")[0].rsplit("/", 1)[-1]
        return self._send(200 if table in self.tables else 404,
                          [] if table in self.tables else {"message": "does not exist"})

    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get("apikey") != self.key:
            return self._send(401, {"message": "bad key"})
        assert "Authorization" not in self.headers, "sb_secret keys go in apikey only"
        if self.path.endswith("/rpc/gmscrape_exec"):
            if not self.runner:
                return self._send(404, {"code": "PGRST202", "message": "function not found"})
            length = int(self.headers.get("Content-Length", 0))
            statement = json.loads(self.rfile.read(length))["sql"]
            self.sql.append(statement)
            lowered = statement.lower()
            if not lowered.startswith(("create table if not exists", "alter table",
                                       "create index if not exists", "create or replace view")):
                return self._send(400, {"message": "gmscrape_exec only manages gmscrape_* objects"})
            if lowered.startswith("create table if not exists "):
                self.tables.add(lowered.split()[5].strip("("))
            return self._send(204, [])
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
def project():
    _Project.runner = True
    _Project.tables = set()
    _Project.sql = []
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Project)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_project_key_creates_shared_and_run_tables_through_the_runner(project):
    config = SupabaseConfig(url=project, key=_Project.key)
    ready, detail = ensure_schema(config, run_table="run_2026_09_10_dentist_in_austin_tx")
    assert ready and "via project key" in detail
    assert {"gmscrape_runs", "gmscrape_leads", "gmscrape_emails",
            "run_2026_09_10_dentist_in_austin_tx"} <= _Project.tables
    # The runner itself is only ever touched by the one-time paste.
    assert not any(s.lower().startswith(("create or replace function", "revoke")) for s in _Project.sql)
    assert runner_installed(config)
    assert ensure_schema(config) == (True, "tables present")


def test_without_the_runner_it_explains_the_one_time_paste(project):
    _Project.runner = False
    config = SupabaseConfig(url=project, key=_Project.key)
    ready, detail = ensure_schema(config, run_table="run_x")
    assert not ready and "gmscrape supabase-init" in detail
    assert not runner_installed(config)
    # Shared tables already there (pasted earlier), runner not: still usable, just no per-run table.
    _Project.tables = {"gmscrape_runs", "gmscrape_leads", "gmscrape_emails"}
    ready, detail = ensure_schema(config, run_table="run_x")
    assert ready and "table per run" in detail


def test_bootstrap_sql_splits_cleanly_and_guards_the_runner():
    sql = exec_function_sql()
    statements = _statements(sql)
    assert statements[0].lower().startswith("create or replace function gmscrape_exec")
    assert "$$" in statements[0] and statements[0].count("$$") == 2   # body kept whole
    assert [s.split()[0] for s in statements[1:]] == ["revoke", "revoke", "revoke"]
    assert "gmscrape_|run_" in sql                                       # only our objects


def test_legacy_jwt_keys_also_get_a_bearer_header():
    jwt_like = "eyJhbGciOi.eyJyb2xlIjoi.signature"
    assert "Authorization" in SupabaseConfig(url="https://x.supabase.co", key=jwt_like).headers()
    assert "Authorization" not in SupabaseConfig(url="https://x.supabase.co", key="sb_secret_abc").headers()


# --- the sink cannot wedge a run ------------------------------------------------
def test_sink_survives_a_poisoned_batch_and_flush_never_hangs(monkeypatch):
    class Explodes:
        def post(self, *args, **kwargs):
            raise RuntimeError("serialisation blew up")

        def close(self):
            return None

    sink = SupabaseSink(SupabaseConfig(url="https://x.supabase.co", key="k"), client=Explodes())
    sink.start_run("r1", {"queries": ["q"]})
    started = time.monotonic()
    sink.flush()                                  # returns; the thread is still alive
    assert time.monotonic() - started < 5
    assert sink.stats.failures >= 1 and "serialisation" in sink.stats.last_error
    assert sink._thread.is_alive()
    sink.close()


def test_flush_gives_up_on_a_dead_writer(monkeypatch):
    sink = SupabaseSink(SupabaseConfig(url="https://x.supabase.co", key="k"),
                        client=type("C", (), {"post": lambda *a, **k: None, "close": lambda s: None})())
    sink.close()                                  # thread gone
    sink._enqueue("leads", [{"id": "x"}])         # something left unwritten
    monkeypatch.setattr(SupabaseSink, "FLUSH_TIMEOUT", 1.0)
    started = time.monotonic()
    sink.flush()
    assert time.monotonic() - started < 3
    assert "writer thread stopped" in sink.stats.last_error


# --- wrong answers are never remembered -----------------------------------------
def test_transient_verification_errors_are_not_cached(tmp_path, monkeypatch):
    from gmscrape.core.pipeline import Pipeline
    from gmscrape.providers.base import EmailVerifier

    calls = {"n": 0}

    class Flaky(EmailVerifier):
        name = "flaky"
        requires_key = True

        def verify(self, email):
            calls["n"] += 1
            if calls["n"] == 1:
                return VerificationResult(status=V_UNKNOWN, provider="flaky", error="timeout")
            return VerificationResult(status=V_VALID, provider="flaky")

    settings = Settings.from_env(db_path=str(tmp_path / "t.sqlite"), maps_provider="file",
                                 places_file=str(tmp_path / "p.csv"))
    (tmp_path / "p.csv").write_text("name,website\n", encoding="utf-8")
    from gmscrape.providers.maps.file_provider import FileMaps

    pipeline = Pipeline(settings, store=Store(settings.db_path), maps=FileMaps(settings),
                        verifier=Flaky(settings))
    first = pipeline._verify_one("a@b.com")
    second = pipeline._verify_one("a@b.com")
    third = pipeline._verify_one("a@b.com")
    pipeline.close()
    assert first.status == V_UNKNOWN and second.status == V_VALID and third.status == V_VALID
    assert calls["n"] == 2                        # the error was retried, the answer was cached


def test_dns_timeouts_are_not_remembered_as_no_mx(tmp_path, monkeypatch):
    from gmscrape.core.pipeline import Pipeline
    from gmscrape.models import BusinessResult, Place
    from gmscrape.providers.maps.file_provider import FileMaps
    import gmscrape.core.pipeline as P

    settings = Settings.from_env(db_path=str(tmp_path / "t.sqlite"), maps_provider="file",
                                 places_file=str(tmp_path / "p.csv"), verify_provider="local")
    (tmp_path / "p.csv").write_text("name,website\n", encoding="utf-8")
    pipeline = Pipeline(settings, store=Store(settings.db_path), maps=FileMaps(settings))
    result = BusinessResult(place=Place(name="x", domain="slow-dns.example.com"))

    monkeypatch.setattr(P, "mx_lookup", lambda d: None)         # DNS timed out
    assert pipeline._domain_has_mx(result, "slow-dns.example.com") is False
    assert pipeline.store.get_domain_facts("slow-dns.example.com") is None   # not cached

    monkeypatch.setattr(P, "mx_lookup", lambda d: ("mx.example.com",))
    assert pipeline._domain_has_mx(result, "slow-dns.example.com") is True
    assert pipeline.store.get_domain_facts("slow-dns.example.com") == (True, None)
    pipeline.close()


# --- the page cache stays small --------------------------------------------------
def test_pages_are_stored_compressed_capped_and_pruned(tmp_path):
    store = Store(str(tmp_path / "t.sqlite"))
    big = "<html>" + ("x" * 900_000) + "</html>"
    store.put_page("https://a.com/", 200, "https://a.com/", big)
    stored = store.conn.execute("SELECT length(html) AS n FROM pages").fetchone()["n"]
    assert stored < 20_000                                    # 400KB cap, then compressed
    status, final, html = store.get_page("https://a.com/", 24)
    assert status == 200 and html.startswith("<html>") and len(html) == Store.MAX_PAGE_BYTES
    store.conn.execute("UPDATE pages SET fetched_at = fetched_at - 999999")
    store.conn.commit()
    assert store.prune(page_ttl_hours=1) == 1
    assert store.get_page("https://a.com/", 24) is None


# --- MCP retries ----------------------------------------------------------------
def test_mcp_client_retries_transient_errors(monkeypatch):
    from gmscrape.providers.mcp import MCPClient

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "result": {"serverInfo": {"name": "ok"}}})

    monkeypatch.setattr("time.sleep", lambda s: None)
    client = MCPClient("http://mcp.test/key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.initialize()["name"] == "ok"
    assert attempts["n"] >= 3


# --- cookies never come back to bite -------------------------------------------
def test_a_sites_non_ascii_cookie_does_not_break_its_next_page(tmp_path, monkeypatch):
    """Seen in the wild: a Set-Cookie with an accented value made every later
    request to that site fail with "'ascii' codec can't encode character".
    The crawler keeps no cookies at all."""
    import asyncio

    import httpx

    import gmscrape.web.fetch as F

    def handler(request):
        if request.url.path == "/":
            raw = [(b"content-type", b"text/html"),
                   (b"set-cookie", ("sesion=" + "x" * 400 + "ó; Path=/").encode("utf-8"))]
            return httpx.Response(200, headers=raw, text="<html><a href='/about'>about</a></html>")
        assert "cookie" not in {k.lower() for k in request.headers}
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>about</html>")

    real_client = httpx.AsyncClient

    def client_with_fake_transport(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(F.httpx, "AsyncClient", client_with_fake_transport)
    settings = Settings.from_env(db_path=str(tmp_path / "t.sqlite"), obey_robots=False, cache_http=False,
                                 http_retries=0)

    async def crawl():
        async with F.Fetcher(settings) as fetcher:
            home = await fetcher.get("http://site.test/")
            about = await fetcher.get("http://site.test/about")
            return home, about

    home, about = asyncio.run(crawl())
    assert home.ok and about.ok and "about" in about.html


def test_runs_also_log_to_a_file(tmp_path):
    import logging

    from gmscrape.cli import add_log_file

    path = tmp_path / "out" / "scraper.log"
    add_log_file(str(path))
    logging.getLogger("gmscrape.test").warning("something worth keeping")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert path.exists() and "something worth keeping" in path.read_text(encoding="utf-8")
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "baseFilename", "") == str(path):
            root.removeHandler(handler)
            handler.close()
