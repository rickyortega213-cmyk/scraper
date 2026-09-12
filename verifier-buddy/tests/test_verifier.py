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


def read_rows(path: Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def by_email(path: Path, column: str = "email") -> dict[str, dict]:
    return {row[column]: row for row in read_rows(path)}


def test_first_run_keeps_every_column_and_only_verified_rows():
    h = Harness()
    try:
        src = h.tmp / "leads.csv"
        src.write_text("Name,Company,Email Address,Phone\n"
                       "Ann,Acme,ok@one.com,111\n"
                       "Bob,Beta,KO@two.com,222\n"
                       "Cy,Gamma,catch@three.com,333\n"
                       "Di,Delta,busy@four.com,444\n"
                       "Ed,Echo,flaky@five.com,555\n"
                       "Fay,Foxtrot,not-an-email,666\n"
                       "Gus,Golf,ok@one.com,777\n"        # same address, different row
                       "Hal,Hotel,,888\n")
        out = h.tmp / "out.csv"
        r = h.run("--key", "good-key-12345", "--rate", "100", str(src), "-o", str(out))
        assert r.returncode == 0, r.stderr + r.stdout
        rows = read_rows(out)
        assert list(rows[0].keys()) == ["Name", "Company", "Email Address", "Phone",
                                        "verified_email", "verify_status", "verify_message"]
        assert [(x["Name"], x["Company"], x["Phone"]) for x in rows] == [
            ("Ann", "Acme", "111"), ("Di", "Delta", "444"), ("Ed", "Echo", "555"),
            ("Gus", "Golf", "777")], rows
        assert all(x["verify_status"] == "valid" for x in rows)
        assert rows[0]["Email Address"] == "ok@one.com" and rows[0]["verified_email"] == "ok@one.com"
        assert rows[1]["Email Address"] == "busy@four.com", "busy mailbox re-checked and kept"
        # the address shared by two rows was verified once
        calls = [q["email"] for q in h.state.requests if q.get("email") == "ok@one.com"]
        assert len(calls) == 1
        cfg = h.config()
        assert cfg["api_key"] == "good-key-12345" and cfg["rate"] == 100 and cfg["auth_mode"] == "direct"
        assert oct(os.stat(h.cfg_dir / "config.json").st_mode & 0o777) == "0o600"
        assert "4 row(s) kept" in r.stdout and "4 dropped" in r.stdout

        # --all keeps every row and labels it
        out_all = h.tmp / "all.csv"
        r = h.run("--all", str(src), "-o", str(out_all), stdin="y\n")
        assert r.returncode == 0, r.stderr + r.stdout
        rows = read_rows(out_all)
        assert len(rows) == 8
        status = {x["Name"]: x["verify_status"] for x in rows}
        assert status == {"Ann": "valid", "Bob": "invalid", "Cy": "catch-all", "Di": "valid",
                          "Ed": "valid", "Fay": "no-email", "Gus": "valid", "Hal": "no-email"}
        assert rows[1]["verified_email"] == "" and rows[1]["Email Address"] == "KO@two.com"

        # --keep widens what counts as verified; --column picks the column by name
        out_keep = h.tmp / "keep.csv"
        r = h.run("--keep", "valid,catch-all", "--column", "Email Address", str(src),
                  "-o", str(out_keep), stdin="y\n")
        assert r.returncode == 0, r.stderr + r.stdout
        assert [x["Name"] for x in read_rows(out_keep)] == ["Ann", "Cy", "Di", "Ed", "Gus"]
    finally:
        h.close()


def test_tsv_and_semicolon_delimiters_are_kept():
    h = Harness()
    try:
        src = h.tmp / "leads.tsv"
        src.write_text("id\towner_email\tnote\n1\tok@a.com\thello, world\n2\tko@b.com\tx\n")
        r = h.run("--key", "good-key-12345", "--rate", "100", str(src))
        assert r.returncode == 0, r.stderr + r.stdout
        out = next(h.tmp.glob("leads-verified-*.csv"))
        text = out.read_text()
        assert text.splitlines()[0] == "id\towner_email\tnote\tverified_email\tverify_status\tverify_message"
        assert text.splitlines()[1].startswith("1\tok@a.com\thello, world\tok@a.com\tvalid")
        assert len(text.splitlines()) == 2
    finally:
        h.close()


def test_second_run_offers_saved_key_and_can_replace_it():
    h = Harness()
    try:
        r = h.run("--key", "good-key-12345", "--rate", "100", "ok@a.com")
        assert r.returncode == 0, r.stderr
        assert by_email(next(h.tmp.glob("verified-*.csv")))["ok@a.com"]["verify_status"] == "valid"
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
        r = h.run("--all", stdin=stdin)
        assert r.returncode == 0, r.stderr + r.stdout
        outs = list(h.tmp.glob("verified-*.csv"))
        assert len(outs) == 1
        rows = read_rows(outs[0])
        assert [x["verified_email"] for x in rows] == ["ok@x.com", "ok@z.com"]
        assert "bye!" in r.stdout
        assert "3 unique address(es)" in r.stdout

        # pasting a whole CSV keeps its columns too
        stdin = "y\nname,email\nA,ok@p.com\nB,ko@q.com\n\nn\n"
        r = h.run(stdin=stdin)
        assert r.returncode == 0, r.stderr + r.stdout
        newest = max(h.tmp.glob("verified-*.csv"), key=lambda p: p.stat().st_mtime_ns)
        assert read_rows(newest) == [{"name": "A", "email": "ok@p.com", "verified_email": "ok@p.com",
                                      "verify_status": "valid", "verify_message": "Accepted"}]
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
        rows = by_email(next(h.tmp.glob("*.csv")))
        assert len(rows) == 14 and all(v["verify_status"] == "valid" for v in rows.values())
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
        rows = by_email(next(h.tmp.glob("*.csv")))
        assert len(rows) == 6 and all(v["verify_status"] == "valid" for v in rows.values()), rows
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
