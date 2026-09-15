"""Shared fixtures: a throwaway local website to crawl, and a stub verifier."""

from __future__ import annotations

import http.server
import os
import socket
import threading
from pathlib import Path

import pytest

os.environ.setdefault("UNSAFE_LISTS", "0")          # tests never download the public lists

from gmscrape.config import Settings
from gmscrape.models import VerificationResult, V_INVALID, V_VALID
from gmscrape.providers.base import EmailVerifier


def _encode_cfemail(email: str, key: int = 0x4a) -> str:
    return format(key, "02x") + "".join(format(ord(c) ^ key, "02x") for c in email)


PAGES: dict[str, str] = {
    "/site1/": """
        <html><head><title>Joe's Plumbing</title></head><body>
        <h1>Joe's Plumbing &amp; Heating</h1>
        <nav><a href="/site1/contact">Contact Us</a>
             <a href="/site1/blog/tips">Blog</a></nav>
        <p>Serving Austin since 1998.</p></body></html>
    """,
    "/site1/contact": """
        <html><body>
        <a href="mailto:Office@joesplumbing.com?subject=Quote">Email the office</a>
        <p>Dispatch: dispatch [at] joesplumbing (dot) com</p>
        <p>Owner's cell mailbox: joe.plumber1972@gmail.com</p>
        <img src="/static/logo@2x.png">
        <script>Sentry.init({dsn:"https://abc@sentry.io/123"});</script>
        </body></html>
    """,
    "/site2/": f"""
        <html><body><h1>Austin Family Dental</h1>
        <p>Reach us: <a class="__cf_email__"
           data-cfemail="{_encode_cfemail('frontdesk@austinfamilydental.com')}"
           href="/cdn-cgi/l/email-protection">[email&#160;protected]</a></p>
        <script type="application/ld+json">
        {{"@type":"Dentist","email":"mailto:newpatients@austinfamilydental.com"}}
        </script>
        <a href="/site2/about">About</a></body></html>
    """,
    "/site2/about": "<html><body><p>Dr. Smith, DDS. no-reply@austinfamilydental.com</p></body></html>",
    "/site3/": """
        <html><body><h1>Riverside Taqueria</h1>
        <p>Call us at (512) 555-0199. Order online!</p>
        <a href="/site3/menu">Menu</a></body></html>
    """,
    "/site3/menu": "<html><body><p>Tacos, burritos, tortas.</p></body></html>",
    # A business Maps lists with no website; discovered via search.
    "/site4/": """
        <html><head><title>Bluebonnet Roofing | Austin TX Roofers</title></head><body>
        <h1>Bluebonnet Roofing</h1>
        <p>Austin's roofing specialists. Call (512) 555-0177 for a free estimate.</p>
        <p>Email: <a href="mailto:hello@bluebonnetroofing.com">hello@bluebonnetroofing.com</a></p>
        <a href="/site4/about">About Us</a></body></html>
    """,
    "/site4/about": """
        <html><body><h1>About Bluebonnet Roofing</h1>
        <p>Bluebonnet Roofing was founded in 2009 by John Kowalski, a third-generation roofer.</p>
        <p>John Kowalski, Owner</p></body></html>
    """,
    # A business whose site never names the owner - found via web search.
    "/site5/": """
        <html><body><h1>Hill Country Landscaping</h1>
        <p>Contact us: info@hillcountrylandscaping.com or (512) 555-0190</p>
        <a href="/site5/services">Services</a></body></html>
    """,
    "/site5/services": "<html><body><p>Lawn care, hardscapes, irrigation.</p></body></html>",
    "/robots.txt": "User-agent: *\nAllow: /\n",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>not found</body></html>")
            return
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:  # silence test output
        return


@pytest.fixture(scope="session")
def site_server() -> str:
    """Serve the fake business pages; yields the base URL."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


# Mailboxes the stub treats as real: everything published on the fake sites,
# plus info@ (so the "stop at the first valid guess" path is exercised).
VALID_LOCALS = {
    "office", "dispatch", "joe.plumber1972", "frontdesk", "newpatients", "info",
    "hello", "john.kowalski", "maria.lopez", "dana.whitfield", "rosa.delgado",
}


class StubVerifier(EmailVerifier):
    """Deterministic stand-in for a paid verification API.

    Records every address it was asked about, so tests can assert on how many
    credits a run would have spent.
    """

    name = "stub"
    requires_key = True

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.calls: list[str] = []

    def verify(self, email: str) -> VerificationResult:
        self.calls.append(email)
        valid = email.split("@")[0] in VALID_LOCALS
        return VerificationResult(
            status=V_VALID if valid else V_INVALID,
            provider=self.name,
            score=95.0 if valid else 5.0,
            mx_found=True,
        )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        db_path=str(tmp_path / "test.sqlite"),
        out_dir=str(tmp_path / "out"),
        maps_provider="file",
        verify_provider="local",
        http_retries=0,
        http_timeout=8.0,
        obey_robots=False,
        cache_http=False,
        permutation_require_mx=False,
        max_pages_per_site=4,
    )
