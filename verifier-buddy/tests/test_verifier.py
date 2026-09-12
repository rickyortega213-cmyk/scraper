"""End-to-end tests: run the real `verifier` script against a fake MailTester
Ninja server.  `python3 -m pytest verifier-buddy/tests` or
`python3 verifier-buddy/tests/test_verifier.py`."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fake_mailtester import serve  # noqa: E402

VERIFIER = HERE.parent / "verifier"


class Harness:
    def __init__(self, limit: int = 100) -> None:
        self.server, self.state = serve(limit)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.tmp = Path(tempfile.mkdtemp(prefix="verifier-test-"))
        self.cfg_dir = self.tmp / "config"

    def run(self, *args: str, stdin: str = "", timeout: float = 120) -> subprocess.CompletedProcess:
        env = {**os.environ,
               "VERIFIER_API_URL": f"{self.base}/ninja",
               "VERIFIER_TOKEN_URL": f"{self.base}/token",
               "VERIFIER_CONFIG_DIR": str(self.cfg_dir),
               "NO_COLOR": "1"}
        return subprocess.run([sys.executable, str(VERIFIER), "--no-banner", *args],
                              input=stdin, capture_output=True, text=True,
                              env=env, cwd=self.tmp, timeout=timeout)

    def config(self) -> dict:
        return json.loads((self.cfg_dir / "config.json").read_text())

    def close(self) -> None:
        self.server.shutdown()


def read_csv(path: Path) -> dict[str, dict]:
    with open(path, newline="") as fh:
        return {row["email"]: row for row in csv.DictReader(fh)}


def test_first_run_saves_key_and_verifies_file():
    h = Harness()
    try:
        src = h.tmp / "leads.csv"
        src.write_text("name,email\nA,ok@one.com\nB,KO@two.com\nC,catch@three.com\n"
                       "D,busy@four.com\nE,flaky@five.com\nF,not-an-email\n"
                       "G,ok@one.com\n")  # duplicate is dropped
        out = h.tmp / "out.csv"
        r = h.run("--key", "good-key-12345", "--rate", "100", str(src), "-o", str(out))
        assert r.returncode == 0, r.stderr + r.stdout
        rows = read_csv(out)
        assert rows["ok@one.com"]["status"] == "valid"
        assert rows["ko@two.com"]["status"] == "invalid"
        assert rows["catch@three.com"]["status"] == "catch-all"
        assert rows["busy@four.com"]["status"] == "valid", "busy mailbox re-checked"
        assert rows["flaky@five.com"]["status"] == "valid", "500 retried"
        assert len(rows) == 5
        cfg = h.config()
        assert cfg["api_key"] == "good-key-12345" and cfg["rate"] == 100 and cfg["auth_mode"] == "direct"
        assert oct(os.stat(h.cfg_dir / "config.json").st_mode & 0o777) == "0o600"
        assert "Summary" in r.stdout and "valid" in r.stdout
    finally:
        h.close()


def test_second_run_offers_saved_key_and_can_replace_it():
    h = Harness()
    try:
        r = h.run("--key", "good-key-12345", "--rate", "100", "ok@a.com")
        assert r.returncode == 0, r.stderr
        # keep it
        r = h.run("ok@b.com", stdin="y\n")
        assert r.returncode == 0, r.stderr + r.stdout
        assert "Keep the saved API key (good…2345)?" in r.stdout
        assert h.config()["api_key"] == "good-key-12345"
        # replace it with a token-only key
        r = h.run("ok@c.com", stdin="n\ntokenonly-key-12345\n")
        assert r.returncode == 0, r.stderr + r.stdout
        assert h.config()["api_key"] == "tokenonly-key-12345"
        assert h.config()["auth_mode"] == "token"
        paths = [q["path"] for q in h.state.requests]
        assert "/token" in paths
        # a refused key asks again, then the good one is saved
        r = h.run("ok@d.com", stdin="n\nnope-nope-nope\ngood-key-12345\n")
        assert r.returncode == 0, r.stderr + r.stdout
        assert "refused this key" in r.stdout
        assert h.config()["api_key"] == "good-key-12345"
    finally:
        h.close()


def test_interactive_paste_flow():
    h = Harness()
    try:
        stdin = "good-key-12345\nok@x.com, ko@y.com\nsomething ok@z.com\n\nn\n"
        r = h.run(stdin=stdin)
        assert r.returncode == 0, r.stderr + r.stdout
        outs = list(h.tmp.glob("verified-*.csv"))
        assert len(outs) == 1
        rows = read_csv(outs[0])
        assert set(rows) == {"ok@x.com", "ko@y.com", "ok@z.com"}
        assert "bye!" in r.stdout
    finally:
        h.close()


def test_rate_limit_is_respected():
    limit = 6
    h = Harness(limit=limit)
    try:
        emails = [f"ok{i}@r.com" for i in range(14)]
        started = time.monotonic()
        r = h.run("--key", "good-key-12345", "--rate", str(limit), "--no-recheck", *emails)
        elapsed = time.monotonic() - started
        assert r.returncode == 0, r.stderr + r.stdout
        rows = read_csv(next(h.tmp.glob("*.csv")))
        assert all(v["status"] == "valid" for v in rows.values())
        assert h.state.max_in_window <= limit
        # 15 calls (14 + probe) at 6 / 10s ≈ 25s of drip; well under a burst-then-429 pattern
        assert elapsed < 60
        assert "rate limited" not in r.stdout
    finally:
        h.close()


def test_backs_off_after_429_instead_of_failing():
    # Ask for a faster rate than the server allows: the 429s must be absorbed.
    h = Harness(limit=3)
    try:
        emails = [f"ok{i}@s.com" for i in range(6)]
        r = h.run("--key", "good-key-12345", "--rate", "30", "--no-recheck", *emails)
        assert r.returncode == 0, r.stderr + r.stdout
        rows = read_csv(next(h.tmp.glob("*.csv")))
        assert all(v["status"] == "valid" for v in rows.values()), rows
    finally:
        h.close()


def test_bad_key_non_interactive_exits_2():
    h = Harness()
    try:
        r = h.run("--key", "wrong-key-here", "ok@a.com")
        assert r.returncode == 2
        assert "refused this key" in r.stdout
        assert not (h.cfg_dir / "config.json").exists()
    finally:
        h.close()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok    {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL  {name}: {exc!r}")
    sys.exit(1 if failures else 0)
