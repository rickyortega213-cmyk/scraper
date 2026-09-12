"""A stand-in for the MailTester Ninja API, for testing the CLI offline.

Keys:   good-key-12345      accepted by the direct endpoint
        tokenonly-key-12345 refused directly, accepted by the token endpoint
        anything else is refused both ways
Emails: the local part decides the answer:
        ok@…   valid       ko@…   rejected     catch@…  catch-all
        busy@… mb once, then ok       slow@…  ok after a delay
        flaky@… HTTP 500 once, then ok
The server also enforces `limit` requests per 10 s and answers 429 beyond it,
and records every request so tests can assert on rate and auth behaviour.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQxMDI0NDQ4MDB9.sig"     # exp far in the future


class State:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.window: deque[float] = deque()
        self.busy_seen: set[str] = set()
        self.flaky_seen: set[str] = set()
        self.max_in_window = 0


class Handler(BaseHTTPRequestHandler):
    state: State

    def log_message(self, *_: object) -> None:      # silence
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        st = self.state
        parts = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        with st.lock:
            st.requests.append({"path": parts.path, **q, "t": time.monotonic()})

        if parts.path == "/token":
            if q.get("key") in ("good-key-12345", "tokenonly-key-12345"):
                return self._json(200, {"token": TOKEN})
            return self._json(401, {"message": "Unauthorized"})

        if parts.path != "/ninja":
            return self._json(404, {"message": "not found"})

        key, token = q.get("key"), q.get("token")
        if not (key == "good-key-12345" or token == TOKEN):
            if key == "tokenonly-key-12345":
                return self._json(403, {"message": "Forbidden"})
            return self._json(401, {"message": "Invalid key, please subscribe"})

        # rate limit: `limit` per 10 s sliding window
        now = time.monotonic()
        with st.lock:
            while st.window and now - st.window[0] > 10.0:
                st.window.popleft()
            if len(st.window) >= st.limit:
                return self._json(429, {"message": "Limited"})
            st.window.append(now)
            st.max_in_window = max(st.max_in_window, len(st.window))

        email = q.get("email", "")
        local = email.split("@")[0]
        base = {"email": email, "user": local, "domain": email.split("@")[-1],
                "mx": "mx.example.com", "connections": 1}
        if local == "ko":
            return self._json(200, {**base, "code": "ko", "message": "Rejected"})
        if local == "catch":
            return self._json(200, {**base, "code": "ok", "message": "Catch-All"})
        if local == "nomx":
            return self._json(200, {**base, "mx": "", "code": "ko", "message": "No Mx"})
        if local == "busy":
            with st.lock:
                first = email not in st.busy_seen
                st.busy_seen.add(email)
            if first:
                return self._json(200, {**base, "code": "mb", "message": "Timeout"})
        if local == "flaky":
            with st.lock:
                first = email not in st.flaky_seen
                st.flaky_seen.add(email)
            if first:
                return self._json(500, {"message": "boom"})
        if local == "slow":
            time.sleep(0.5)
        return self._json(200, {**base, "code": "ok", "message": "Accepted"})


def serve(limit: int = 100) -> tuple[ThreadingHTTPServer, State]:
    state = State(limit)
    handler = type("H", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state
