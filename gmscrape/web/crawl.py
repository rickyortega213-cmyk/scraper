"""Crawl a business website looking for contact addresses.

Fetch the homepage, rank internal links (contact / about / team / impressum),
crawl the best few, and stop as soon as an on-domain address turns up.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..config import Settings
from ..models import EmailCandidate
from ..util import dedupe_preserving_order, normalize_url, registered_domain
from .extract import extract_emails, find_internal_links, to_candidates
from .fetch import Fetcher, Page

log = logging.getLogger(__name__)

# Tried directly when the homepage exposes no useful internal links.
FALLBACK_PATHS = (
    "/contact", "/contact-us", "/contactus", "/contact.html", "/contact.php",
    "/about", "/about-us", "/team", "/staff", "/impressum", "/kontakt",
)


@dataclass
class SiteScrape:
    """Result of crawling one website."""

    url: str
    status: str = ""                     # "ok" | "unreachable:<detail>"
    candidates: list[EmailCandidate] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    final_url: str = ""
    errors: list[str] = field(default_factory=list)


async def scrape_site(
    fetcher: Fetcher,
    website: str,
    settings: Settings,
    business_domain: str = "",
) -> SiteScrape:
    """Crawl `website` and return every email candidate found on it."""
    url = normalize_url(website)
    result = SiteScrape(url=url)
    if not url:
        result.status = "no_website"
        return result

    domain = business_domain or registered_domain(url)
    home = await fetcher.get(url)
    if not home.ok:
        detail = home.error or f"http_{home.status}"
        result.status = f"unreachable:{detail}"
        result.errors.append(detail)
        return result

    result.status = "ok"
    result.final_url = home.final_url or url
    result.pages.append(home.final_url or url)
    seen_emails: dict[str, EmailCandidate] = {}
    _absorb(seen_emails, home, domain)

    if _has_on_domain(seen_emails, domain) and settings.max_pages_per_site <= 1:
        result.candidates = list(seen_emails.values())
        return result

    budget = max(0, settings.max_pages_per_site - 1)
    if budget:
        targets = find_internal_links(home.html, result.final_url, limit=budget * 3)
        if not targets:
            base = (home.final_url or url).rstrip("/")
            targets = [f"{base}{path}" for path in FALLBACK_PATHS]
        targets = [t for t in dedupe_preserving_order(targets) if t not in result.pages][:budget]

        for chunk in _chunks(targets, max(1, settings.per_host_concurrency)):
            pages = await asyncio.gather(*(fetcher.get(t) for t in chunk))
            for page in pages:
                if not page.ok:
                    if page.error:
                        result.errors.append(f"{page.url}: {page.error}")
                    continue
                result.pages.append(page.final_url or page.url)
                _absorb(seen_emails, page, domain)
            # Enough signal: an address on the business's own domain.
            if _has_on_domain(seen_emails, domain):
                break

    result.candidates = list(seen_emails.values())
    return result


def _absorb(store: dict[str, EmailCandidate], page: Page, domain: str) -> None:
    from ..models import SOURCE_WEIGHT

    found = extract_emails(page.html, page.final_url or page.url)
    for candidate in to_candidates(
        found, page_url=page.final_url or page.url, business_domain=domain
    ):
        existing = store.get(candidate.email)
        if existing is None or SOURCE_WEIGHT.get(candidate.source, 0) > SOURCE_WEIGHT.get(
            existing.source, 0
        ):
            store[candidate.email] = candidate


def _has_on_domain(store: dict[str, EmailCandidate], domain: str) -> bool:
    if not domain:
        return bool(store)
    return any(c.on_business_domain and not c.is_low_value for c in store.values())


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i: i + size] for i in range(0, len(items), size)]
