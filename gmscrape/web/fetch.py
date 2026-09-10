"""Polite async HTTP fetching for business websites.

Global + per-host concurrency caps, retries with backoff, robots.txt support,
a response-size ceiling, and an optional SQLite page cache so re-runs never
re-hit a site.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Optional, Protocol
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx

from ..config import DEFAULT_USER_AGENTS, Settings


def _cache_missing_optional_modules() -> None:
    """httpcore asks "is trio here? is sniffio here?" on every request. Python
    does not remember a failed import, so each ask walks sys.path and stats
    dozens of files - a fifth of the CPU per business in profiling. Record
    the absence once so the import fails instantly from then on."""
    import importlib.util
    import sys

    for name in ("trio", "sniffio"):
        if name in sys.modules:
            continue
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            sys.modules[name] = None  # type: ignore[assignment]


_cache_missing_optional_modules()
from ..util import hostname, normalize_url

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}
HTML_TYPES = ("text/html", "application/xhtml", "text/plain", "application/xml", "text/xml")


@dataclass
class Page:
    """One fetched page."""

    url: str
    final_url: str = ""
    status: int = 0
    html: str = ""
    error: str = ""
    from_cache: bool = False
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.html)


class PageCache(Protocol):
    """Minimal interface the store layer implements."""

    def get_page(self, url: str, ttl_hours: int) -> Optional[tuple[int, str, str]]: ...
    def put_page(self, url: str, status: int, final_url: str, html: str) -> None: ...


class Fetcher:
    """Async HTTP client with politeness controls."""

    def __init__(self, settings: Settings, cache: Optional[PageCache] = None) -> None:
        self.stats: dict[str, float] = {"n": 0, "s": 0.0}     # pages fetched, seconds on the wire
        self.settings = settings
        self.cache = cache if settings.cache_http else None
        self._global_sem = asyncio.Semaphore(max(1, settings.http_concurrency))
        self._host_sems: dict[str, asyncio.Semaphore] = {}
        self._robots: dict[str, Optional[RobotFileParser]] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._client: Optional[httpx.AsyncClient] = None

    # --- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "Fetcher":
        limits = httpx.Limits(
            max_connections=max(4, self.settings.http_concurrency * 2),
            max_keepalive_connections=self.settings.http_concurrency,
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.http_timeout),
            follow_redirects=True,
            limits=limits,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            verify=True,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- helpers -----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        agent = (
            random.choice(DEFAULT_USER_AGENTS)
            if self.settings.rotate_user_agent
            else self.settings.user_agent
        )
        return {"User-Agent": agent}

    def _host_sem(self, host: str) -> asyncio.Semaphore:
        if host not in self._host_sems:
            self._host_sems[host] = asyncio.Semaphore(max(1, self.settings.per_host_concurrency))
        return self._host_sems[host]

    async def allowed_by_robots(self, url: str) -> bool:
        """True when robots.txt permits our user agent (fail-open on errors)."""
        if not self.settings.obey_robots:
            return True
        host = hostname(url)
        if not host:
            return True
        if host not in self._robots:
            lock = self._robots_locks.setdefault(host, asyncio.Lock())
            async with lock:
                if host not in self._robots:
                    self._robots[host] = await self._load_robots(url)
        parser = self._robots[host]
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.settings.user_agent, url)
        except Exception:  # pragma: no cover - defensive
            return True

    async def _load_robots(self, url: str) -> Optional[RobotFileParser]:
        robots_url = urljoin(url, "/robots.txt")
        assert self._client is not None
        try:
            response = await self._client.get(
                robots_url, headers=self._headers(), timeout=8.0
            )
        except Exception as exc:
            log.debug("robots.txt fetch failed for %s: %s", robots_url, exc)
            return None
        if response.status_code >= 400 or not response.text:
            return None
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser

    # --- fetching ----------------------------------------------------------
    async def get(self, url: str, *, use_cache: bool = True) -> Page:
        url = normalize_url(url)
        if not url:
            return Page(url=url, error="invalid_url")

        if use_cache and self.cache is not None:
            cached = self.cache.get_page(url, self.settings.cache_ttl_hours)
            if cached is not None:
                status, final_url, body = cached
                return Page(
                    url=url, final_url=final_url or url, status=status,
                    html=body, from_cache=True,
                )

        if not await self.allowed_by_robots(url):
            return Page(url=url, error="blocked_by_robots")

        host = hostname(url)
        started = time.perf_counter()
        async with self._global_sem, self._host_sem(host):
            if self.settings.crawl_delay > 0:
                await asyncio.sleep(self.settings.crawl_delay)
            inner = time.perf_counter()
            page = await self._get_with_retries(url)
            self.stats["n"] += 1
            self.stats["s"] += time.perf_counter() - inner      # on the wire, not queueing
        page.elapsed = time.perf_counter() - started

        if page.ok and self.cache is not None:
            try:
                self.cache.put_page(url, page.status, page.final_url, page.html)
            except Exception as exc:  # pragma: no cover - cache is best effort
                log.debug("cache write failed for %s: %s", url, exc)
        return page

    async def _get_with_retries(self, requested: str) -> Page:
        """Fetch `requested`, retrying transient failures.

        A bare-domain failure is often a www-only host, so that spelling gets
        its own attempts rather than eating this URL's retries.
        """
        candidates = [requested]
        alternate = self._alternate_url(requested)
        if alternate:
            candidates.append(alternate)

        last_error = ""
        for url in candidates:
            page = await self._attempt_url(url, requested)
            if page.status or page.error == "too_many_redirects" or page.error.startswith(
                "unsupported_content_type"
            ):
                return page
            last_error = page.error or last_error
        return Page(url=requested, error=last_error or "fetch_failed")

    async def _attempt_url(self, url: str, requested: str) -> Page:
        """One URL spelling, with backoff across `http_retries` attempts."""
        assert self._client is not None
        attempts = max(1, self.settings.http_retries + 1)
        last_error = ""
        for attempt in range(attempts):
            try:
                response = await self._client.get(url, headers=self._headers())
            except httpx.TooManyRedirects:
                return Page(url=requested, error="too_many_redirects")
            except (httpx.TransportError, httpx.TimeoutException, httpx.HTTPError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts - 1:
                    await asyncio.sleep(1.5 * (2 ** attempt) + random.uniform(0, 0.3))
                continue

            if response.status_code in RETRY_STATUS and attempt < attempts - 1:
                delay = self._retry_after(response, attempt)
                log.debug("%s -> HTTP %s, retry in %.1fs", url, response.status_code, delay)
                await asyncio.sleep(delay)
                continue

            content_type = response.headers.get("content-type", "").lower()
            if content_type and not any(t in content_type for t in HTML_TYPES):
                return Page(
                    url=requested, final_url=str(response.url),
                    status=response.status_code,
                    error=f"unsupported_content_type:{content_type.split(';')[0]}",
                )

            body = response.content[: self.settings.http_max_bytes]
            text = body.decode(response.encoding or "utf-8", errors="replace")
            return Page(
                url=requested, final_url=str(response.url),
                status=response.status_code, html=text,
            )
        return Page(url=requested, error=last_error or "fetch_failed")

    @staticmethod
    def _alternate_url(url: str) -> str:
        """Try www.<host> once when the bare host will not resolve/connect."""
        host = hostname(url)
        if not host or host.startswith("www."):
            return ""
        return url.replace(f"//{host}", f"//www.{host}", 1)

    @staticmethod
    def _retry_after(response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return min(30.0, float(header))
            except ValueError:
                pass
        return 1.5 * (2 ** attempt) + random.uniform(0, 0.3)
