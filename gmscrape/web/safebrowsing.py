"""Ask before visiting: is this website on the unsafe list?

macOS stops a scrape the moment it connects to a website on Apple's Safe
Browsing list (see web/unsafe.py). That list is Google Safe Browsing, which
answers lookups for free with an API key: up to 500 URLs per request. Every
batch's websites are checked here before the first byte is fetched, and a
listed site is skipped instead of visited. Verdicts are remembered in the
database for a day, so a resumed run asks nothing twice.

Getting a key (once, five minutes): console.cloud.google.com → APIs &
Services → Library → "Safe Browsing API" → Enable → Credentials → Create
credentials → API key. Save it as SAFE_BROWSING_KEY.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional, Protocol

import httpx

from .. import __version__
from .unsafe import site_key
from ..util import normalize_url

log = logging.getLogger(__name__)

API = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
THREAT_TYPES = ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE", "POTENTIALLY_HARMFUL_APPLICATION"]
BATCH = 500                     # the API's per-request maximum
TTL_HOURS = 24.0


class SafetyCache(Protocol):
    def get_site_safety(self, domains: Iterable[str]) -> dict[str, tuple[bool, float]]: ...
    def put_site_safety(self, verdicts: dict[str, bool], source: str) -> None: ...


class SafeBrowsing:
    """Google Safe Browsing Lookup API v4 client with a per-site verdict cache."""

    name = "google_safe_browsing"

    def __init__(self, key: str, cache: Optional[SafetyCache] = None, *, timeout: float = 15.0,
                 api: str = API, ttl_hours: float = TTL_HOURS) -> None:
        self.key = (key or "").strip()
        self.cache = cache
        self.timeout = timeout
        self.api = api
        self.ttl = ttl_hours * 3600
        self.stats = {"checked": 0, "flagged": 0, "requests": 0, "errors": 0}
        self._memory: dict[str, tuple[bool, float]] = {}
        self._broken = ""             # set once the key is rejected: no more requests

    @property
    def enabled(self) -> bool:
        return bool(self.key) and not self._broken

    # -- lookups ---------------------------------------------------------------
    def check(self, urls: Iterable[str]) -> set[str]:
        """Site keys (registered domains) among `urls` that are listed."""
        if not self.enabled:
            return set()
        wanted: dict[str, str] = {}
        for raw in urls:
            url = normalize_url(raw)
            domain = site_key(url) if url else ""
            if domain and domain not in wanted:
                wanted[domain] = url
        if not wanted:
            return set()

        flagged: set[str] = set()
        now = time.time()
        cached = self._lookup_cache(wanted)
        pending: dict[str, str] = {}
        for domain, url in wanted.items():
            hit = cached.get(domain)
            if hit is not None and now - hit[1] < self.ttl:
                if hit[0]:
                    flagged.add(domain)
            else:
                pending[domain] = url

        items = list(pending.items())
        for start in range(0, len(items), BATCH):
            chunk = dict(items[start:start + BATCH])
            listed = self._query(chunk)
            if listed is None:                       # request failed: fail open, no caching
                continue
            verdicts = {domain: (domain in listed) for domain in chunk}
            self._remember(verdicts)
            flagged.update(listed)
            self.stats["checked"] += len(chunk)
            self.stats["flagged"] += len(listed)
        return flagged

    def _query(self, chunk: dict[str, str]) -> Optional[set[str]]:
        """One request; the site keys the API listed, or None when it failed."""
        body = {
            "client": {"clientId": "gmscrape", "clientVersion": __version__},
            "threatInfo": {
                "threatTypes": THREAT_TYPES,
                "platformTypes": ["ANY_PLATFORM"],
                "threatEntryTypes": ["URL"],
                # The site itself and the page Maps gave: a listing can be either.
                "threatEntries": [{"url": u} for u in _entries(chunk)],
            },
        }
        self.stats["requests"] += 1
        try:
            response = httpx.post(self.api, params={"key": self.key}, json=body, timeout=self.timeout)
        except httpx.HTTPError as exc:
            self.stats["errors"] += 1
            log.warning("safe browsing lookup failed (%s); those sites are visited unchecked", exc)
            return None
        if response.status_code in (400, 403):
            self._broken = f"HTTP {response.status_code}"
            self.stats["errors"] += 1
            log.error("Safe Browsing rejected the key (HTTP %s: %s). Check SAFE_BROWSING_KEY and that the "
                      "Safe Browsing API is enabled for it; sites are visited unchecked until then.",
                      response.status_code, _reason(response))
            return None
        if response.status_code != 200:
            self.stats["errors"] += 1
            log.warning("safe browsing lookup HTTP %s (%s); those sites are visited unchecked",
                        response.status_code, _reason(response))
            return None
        try:
            matches = response.json().get("matches") or []
        except ValueError:
            self.stats["errors"] += 1
            return None
        listed: set[str] = set()
        for match in matches:
            url = ((match.get("threat") or {}).get("url")) or ""
            domain = site_key(url)
            if domain in chunk:
                listed.add(domain)
                log.info("unsafe site skipped: %s (%s)", domain, match.get("threatType", "listed"))
        return listed

    # -- cache -----------------------------------------------------------------
    def _lookup_cache(self, wanted: dict[str, str]) -> dict[str, tuple[bool, float]]:
        out = {d: self._memory[d] for d in wanted if d in self._memory}
        missing = [d for d in wanted if d not in out]
        if missing and self.cache is not None:
            try:
                stored = self.cache.get_site_safety(missing)
            except Exception as exc:  # pragma: no cover - cache is best effort
                log.debug("site safety cache read failed: %s", exc)
                stored = {}
            out.update(stored)
            self._memory.update(stored)
        return out

    def _remember(self, verdicts: dict[str, bool]) -> None:
        now = time.time()
        for domain, unsafe in verdicts.items():
            self._memory[domain] = (unsafe, now)
        if self.cache is not None:
            try:
                self.cache.put_site_safety(verdicts, self.name)
            except Exception as exc:  # pragma: no cover
                log.debug("site safety cache write failed: %s", exc)


def _entries(chunk: dict[str, str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for domain, url in chunk.items():
        for candidate in (url, f"http://{domain}/"):
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def _reason(response: httpx.Response) -> str:
    try:
        return str((response.json().get("error") or {}).get("message") or response.text[:200])
    except ValueError:
        return response.text[:200]
