"""Websites are asked about before they are visited; a listed one is skipped."""

from __future__ import annotations

import http.server
import json
import socket
import threading
from pathlib import Path

import pytest

from gmscrape.core.pipeline import Pipeline
from gmscrape.providers.maps.file_provider import FileMaps
from gmscrape.store.db import Store
from gmscrape.web.safebrowsing import SafeBrowsing
from gmscrape.web.unsafe import host_listed, load_blocked, parse_host_list

from conftest import StubVerifier


class _FakeSafeBrowsing(http.server.BaseHTTPRequestHandler):
    """Lists any URL whose host contains 'evil'; rejects the key 'bad'."""
    requests: list[dict] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        if "key=bad" in self.path:
            payload = {"error": {"code": 400, "message": "API key not valid"}}
            self.send_response(400)
        else:
            entries = body["threatInfo"]["threatEntries"]
            matches = [{"threatType": "MALWARE", "threat": {"url": e["url"]}} for e in entries if "evil" in e["url"]]
            payload = {"matches": matches} if matches else {}
            self.send_response(200)
        raw = json.dumps(payload).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture
def api():
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _FakeSafeBrowsing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeSafeBrowsing.requests = []
    try:
        yield f"http://127.0.0.1:{port}/v4/threatMatches:find"
    finally:
        server.shutdown(); server.server_close()


def test_listed_sites_are_flagged_and_verdicts_cached(api, tmp_path):
    with Store(str(tmp_path / "t.sqlite")) as store:
        checker = SafeBrowsing("good", cache=store, api=api)
        flagged = checker.check(["https://www.evil-shop.com/page", "https://fine-dentist.com", "http://evil.example.org/"])
        assert flagged == {"evil-shop.com", "example.org"}
        assert checker.stats["requests"] == 1 and checker.stats["checked"] == 3
        # both the page and the bare site were asked about
        urls = [e["url"] for e in _FakeSafeBrowsing.requests[0]["threatInfo"]["threatEntries"]]
        assert "https://www.evil-shop.com/page" in urls and "http://evil-shop.com/" in urls
        # a second client (a resumed run) answers from the database, no request
        again = SafeBrowsing("good", cache=store, api=api)
        assert again.check(["https://fine-dentist.com/contact", "https://evil-shop.com"]) == {"evil-shop.com"}
        assert again.stats["requests"] == 0


def test_a_rejected_key_is_reported_once_and_sites_are_visited_unchecked(api, caplog):
    checker = SafeBrowsing("bad", api=api)
    assert checker.check(["https://evil.com"]) == set()
    assert not checker.enabled
    assert checker.check(["https://evil.com"]) == set()
    assert len(_FakeSafeBrowsing.requests) == 1
    assert "rejected the key" in caplog.text


def test_an_unreachable_api_fails_open(tmp_path):
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    checker = SafeBrowsing("good", api=f"http://127.0.0.1:{port}/x", timeout=1.0)
    assert checker.check(["https://evil.com"]) == set()
    assert checker.enabled and checker.stats["errors"] == 1


def test_public_lists_match_hosts_and_their_parents():
    hosts = parse_host_list("127.0.0.1 bad.example.com\nhttp://phish.example.org/login\nsite.evil.net\n")
    assert hosts == {"bad.example.com", "phish.example.org", "site.evil.net"}
    assert host_listed("bad.example.com", hosts)
    assert host_listed("cdn.bad.example.com", hosts)             # under a listed host
    assert not host_listed("example.com", hosts)                 # a sibling is not condemned
    assert not host_listed("good.example.org", hosts)
    assert not host_listed("evil.net", hosts)


def test_pipeline_screens_a_batch_before_crawling(api, tmp_path, settings, site_server, monkeypatch):
    from test_pipeline_e2e import _places_file

    import gmscrape.web.safebrowsing as sb
    monkeypatch.setattr(sb, "API", api)
    settings.places_file = str(_places_file(tmp_path, site_server))
    settings.crawl_websites = True
    settings.safe_browsing_key = "good"
    pipeline = Pipeline(settings, maps=FileMaps(settings), verifier=StubVerifier(settings))
    pipeline.safety.api = api
    # every fake site is on 127.0.0.1: make the fake API list that host
    monkeypatch.setattr(_FakeSafeBrowsing, "do_POST", _listing("127.0.0.1"))
    with pipeline:
        run = pipeline.run(["*"])
    joe = next(r for r in run.results if "Joe's Plumbing" in r.place.name)
    assert joe.website_status == "skipped:unsafe_site"
    assert not joe.pages_crawled
    assert "127.0.0.1" in load_blocked(Path(settings.out_dir))
    assert "listed by Safe Browsing" in (Path(settings.out_dir) / "blocked_sites.txt").read_text()


def _listing(needle: str):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        entries = body["threatInfo"]["threatEntries"]
        matches = [{"threatType": "MALWARE", "threat": {"url": e["url"]}} for e in entries if needle in e["url"]]
        raw = json.dumps({"matches": matches} if matches else {}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    return do_POST
