"""Websites that get a run killed are remembered and never visited again.

macOS (26.4 and later) traces the processes started by a command pasted into
Terminal and checks every website they connect to against Apple's Safe
Browsing list. A scrape visits tens of thousands of business websites; when
one of them is on that list, macOS stops the whole run ("Malicious Script
Blocked") with no override.

Two things make that survivable:

  * `Inflight` keeps `out/inflight.json` up to date with the websites being
    fetched right now. A worker that exits cleanly removes the file; a worker
    that is killed leaves it behind.
  * `quarantine_leftovers` runs before every start. A leftover file means the
    previous worker died mid-crawl, so every site it was visiting goes onto
    `out/blocked_sites.txt`, and `load_blocked` keeps the crawler off them.

Losing the crawl of the couple of hundred sites in flight when a run is cut
off costs a few leads; repeating the same death on resume would cost the run.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from ..util import hostname, registered_domain

log = logging.getLogger(__name__)

INFLIGHT_FILE = "inflight.json"
BLOCKED_FILE = "blocked_sites.txt"
_HEADER = (
    "# Websites skipped by the crawler. A run was cut off by the computer while\n"
    "# visiting them (macOS stops a scrape that reaches a site on Apple's unsafe\n"
    "# list). One domain per line; delete a line to visit that site again.\n"
)


class Inflight:
    """The set of registered domains with a request on the wire, mirrored to a
    file by a background thread (at most once a second, only on change)."""

    def __init__(self, path: Optional[Path], interval: float = 1.0) -> None:
        self.path = Path(path) if path else None
        self.interval = interval
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if self.path is not None:
            self._thread = threading.Thread(target=self._loop, name="inflight", daemon=True)
            self._thread.start()

    # -- registry ------------------------------------------------------------
    def enter(self, url_or_host: str) -> str:
        domain = site_key(url_or_host)
        if not domain:
            return ""
        with self._lock:
            self._counts[domain] = self._counts.get(domain, 0) + 1
            self._dirty = True
        return domain

    def leave(self, domain: str) -> None:
        if not domain:
            return
        with self._lock:
            left = self._counts.get(domain, 0) - 1
            if left <= 0:
                self._counts.pop(domain, None)
            else:
                self._counts[domain] = left
            self._dirty = True

    def domains(self) -> list[str]:
        with self._lock:
            return sorted(self._counts)

    # -- mirror --------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.flush()

    def flush(self) -> None:
        if self.path is None:
            return
        with self._lock:
            if not self._dirty:
                return
            snapshot = {"at": time.time(), "domains": sorted(self._counts)}
            self._dirty = False
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:  # pragma: no cover - best effort
            log.debug("inflight write failed: %s", exc)

    def close(self) -> None:
        """A clean exit leaves no file behind: nothing to quarantine."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.path is not None:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:  # pragma: no cover
                log.debug("inflight remove failed: %s", exc)


# The worker's registry; the fetcher reports to it when one is open.
current: Optional[Inflight] = None


def open_inflight(out_dir: Path | str) -> Inflight:
    global current
    path = Path(out_dir) / INFLIGHT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    current = Inflight(path)
    return current


def close_inflight() -> None:
    global current
    if current is not None:
        current.close()
        current = None


def site_key(url_or_host: str) -> str:
    """What a site is listed as: its registered domain, or the bare host when
    there is none (an IP address, a local test server)."""
    return registered_domain(url_or_host) or hostname(url_or_host)


# -- the skip list -----------------------------------------------------------
def load_blocked(out_dir: Path | str) -> set[str]:
    path = Path(out_dir) / BLOCKED_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return set()
    out: set[str] = set()
    for line in lines:
        line = line.strip().lower()
        if line and not line.startswith("#"):
            out.add(site_key(line) or line)
    return out


def add_blocked(out_dir: Path | str, domains: Iterable[str]) -> list[str]:
    """Append `domains` (those not already listed); return the ones added."""
    known = load_blocked(out_dir)
    fresh = sorted({d for d in (site_key(x) or x.strip().lower() for x in domains) if d} - known)
    if not fresh:
        return []
    path = Path(out_dir) / BLOCKED_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    with path.open("a", encoding="utf-8") as handle:
        if not known and path.stat().st_size == 0:
            handle.write(_HEADER)
        handle.write(f"# cut off {stamp}\n")
        handle.write("".join(f"{d}\n" for d in fresh))
    return fresh


def quarantine_leftovers(out_dir: Path | str) -> list[str]:
    """If the previous worker left `inflight.json` behind it died mid-crawl:
    block every site it was visiting and remove the file. Returns what was
    blocked (empty when there was nothing to do)."""
    path = Path(out_dir) / INFLIGHT_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return []
    try:
        domains = list(json.loads(raw).get("domains") or [])
    except (ValueError, AttributeError):
        domains = []
    added = add_blocked(out_dir, domains) if domains else []
    try:
        path.unlink()
    except OSError:
        pass
    if added:
        log.warning("previous run was cut off while visiting %d site(s); they are now skipped "
                    "(see %s)", len(added), Path(out_dir) / BLOCKED_FILE)
    return added
