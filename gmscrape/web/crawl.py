"""Crawl a business website for contact addresses and the person in charge.

Fetch the homepage, rank internal links (contact / about / team / impressum),
crawl the best few, and stop once we have both an address on the business's
own domain and a named owner - or run out of page budget. When the address
turns up early but the owner hasn't, the remaining budget is spent on the
pages most likely to name them (About, Team, Meet the Doctor).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ..config import Settings
from ..emails.people import OwnerCandidate, choose_owner, owner_candidates_from_html
from ..models import EmailCandidate, Person
from ..util import dedupe_preserving_order, normalize_url, registered_domain
from .extract import extract_emails, find_internal_links, to_candidates
from .fetch import Fetcher, Page

log = logging.getLogger(__name__)

# Tried directly when the homepage exposes no useful internal links.
FALLBACK_PATHS = (
    "/contact", "/contact-us", "/contactus", "/contact.html", "/contact.php",
    "/about", "/about-us", "/team", "/staff", "/our-team", "/meet-the-team",
    "/impressum", "/kontakt",
)

# Pages where the owner is usually named.
_PEOPLE_PAGE_RE = re.compile(
    r"about|team|staff|meet|our-story|ourstory|story|who-we-are|founder|owner|"
    r"doctor|dr-|dentist|attorney|leadership|management|people|bio",
    re.IGNORECASE,
)


@dataclass
class SiteScrape:
    """Result of crawling one website."""

    url: str
    status: str = ""                     # "ok" | "no_website" | "unreachable:<detail>"
    candidates: list[EmailCandidate] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    final_url: str = ""
    homepage_text: str = ""              # for website confirmation
    owner: Optional[Person] = None
    owner_candidates: list[OwnerCandidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def scrape_site(
    fetcher: Fetcher,
    website: str,
    settings: Settings,
    business_domain: str = "",
    *,
    business_name: str = "",
    medical: bool = False,
    find_owner: bool = True,
    owner_min_confidence: int = 60,
) -> SiteScrape:
    """Crawl `website` for email candidates and the owner."""
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
    result.pages.append(result.final_url)
    result.homepage_text = _page_text(home.html)

    seen_emails: dict[str, EmailCandidate] = {}
    _absorb(seen_emails, home, domain)
    if find_owner:
        result.owner_candidates.extend(
            owner_candidates_from_html(home.html, result.final_url, business_name, medical=medical)
        )

    budget = max(0, settings.max_pages_per_site - 1)
    if budget and not _done(seen_emails, domain, result.owner_candidates, find_owner):
        targets = find_internal_links(home.html, result.final_url, limit=budget * 4)
        if not targets:
            base = result.final_url.rstrip("/")
            targets = [f"{base}{path}" for path in FALLBACK_PATHS]
        targets = [t for t in dedupe_preserving_order(targets) if t not in result.pages]

        while targets and budget > 0:
            # Once the address is in hand, spend what's left on people pages.
            if find_owner and _has_on_domain(seen_emails, domain) and not result.owner_candidates:
                targets.sort(key=lambda t: 0 if _PEOPLE_PAGE_RE.search(t) else 1)
            chunk_size = max(1, settings.per_host_concurrency)
            chunk, targets = targets[:chunk_size], targets[chunk_size:]
            budget -= len(chunk)
            pages = await asyncio.gather(*(fetcher.get(t) for t in chunk))
            for page in pages:
                if not page.ok:
                    if page.error:
                        result.errors.append(f"{page.url}: {page.error}")
                    continue
                result.pages.append(page.final_url or page.url)
                _absorb(seen_emails, page, domain)
                if find_owner:
                    result.owner_candidates.extend(
                        owner_candidates_from_html(
                            page.html, page.final_url or page.url, business_name, medical=medical
                        )
                    )
            if _done(seen_emails, domain, result.owner_candidates, find_owner):
                break

    result.candidates = list(seen_emails.values())
    if find_owner and result.owner_candidates:
        result.owner = choose_owner(result.owner_candidates, min_confidence=owner_min_confidence)
    return result


def _absorb(store: dict[str, EmailCandidate], page: Page, domain: str) -> None:
    from ..models import SOURCE_WEIGHT

    page_url = page.final_url or page.url
    found = extract_emails(page.html, page_url, business_domain=domain)
    for candidate in to_candidates(found, page_url=page_url, business_domain=domain):
        existing = store.get(candidate.email)
        if existing is None or SOURCE_WEIGHT.get(candidate.source, 0) > SOURCE_WEIGHT.get(
            existing.source, 0
        ):
            store[candidate.email] = candidate


def _has_on_domain(store: dict[str, EmailCandidate], domain: str) -> bool:
    if not domain:
        return bool(store)
    return any(c.on_business_domain and not c.is_low_value for c in store.values())


def _done(
    store: dict[str, EmailCandidate], domain: str, owners: list[OwnerCandidate], find_owner: bool
) -> bool:
    have_email = _has_on_domain(store, domain)
    have_owner = bool(owners) or not find_owner
    return have_email and have_owner


def _page_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return f"{title} {soup.get_text(' ', strip=True)}"[:20000]


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i: i + size] for i in range(0, len(items), size)]
