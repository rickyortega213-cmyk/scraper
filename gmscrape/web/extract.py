"""Pull email addresses out of raw HTML.

Handles the ways small-business sites actually publish addresses:
  * plain text and mailto: links
  * HTML entities (&#64;, &#x40;, &commat;)
  * Cloudflare "email protection" (data-cfemail / /cdn-cgi/l/email-protection#hex)
  * human obfuscation: info [at] domain [dot] com, "info (at) domain dot com"
  * JSON-LD / inline JSON blobs and JS string concatenation
  * URL-encoded mailto targets

Then filters the noise that a naive regex always drags in: image filenames
(logo@2x.png), CDN/analytics hosts, CMS placeholders and no-reply mailboxes.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from ..data.domains import (
    FREE_MAIL_DOMAINS,
    JUNK_EMAIL_DOMAINS,
    LOW_VALUE_LOCAL_PARTS,
    PLATFORM_DOMAINS,
    ROLE_LOCAL_PARTS,
)
from ..models import (
    EmailCandidate,
    SOURCE_CLOUDFLARE,
    SOURCE_HTML_TEXT,
    SOURCE_JSONLD,
    SOURCE_MAILTO,
    SOURCE_OBFUSCATED,
)
from ..util import (
    has_valid_suffix,
    registered_domain,
    squeeze,
    trim_context,
)

# --- patterns --------------------------------------------------------------
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"([A-Za-z0-9][A-Za-z0-9._%+\-]{0,63})"
    r"@"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})"
    r"(?![A-Za-z0-9\-])"
)

# "info (at) example (dot) com", "info [at] example . com", "info AT example DOT com"
_AT = r"(?:\(\s*at\s*\)|\[\s*at\s*\]|\{\s*at\s*\}|\s+at\s+|&#0?64;|&#x0?40;|&commat;|\(@\)|\s*@\s*)"
_DOT = r"(?:\(\s*dot\s*\)|\[\s*dot\s*\]|\{\s*dot\s*\}|\s+dot\s+|&#0?46;|&#x0?2e;|\s*\.\s*)"
OBFUSCATED_RE = re.compile(
    rf"([A-Za-z0-9][A-Za-z0-9._%+\-]{{0,63}})\s*{_AT}\s*"
    rf"((?:[A-Za-z0-9][A-Za-z0-9\-]{{0,61}}\s*{_DOT}\s*)+[A-Za-z]{{2,24}})",
    re.IGNORECASE,
)

CF_LINK_RE = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]{4,})")
CF_ATTR_RE = re.compile(r"data-cfemail=[\"']([0-9a-fA-F]{4,})[\"']", re.IGNORECASE)

# Image/asset filenames that look like emails after a naive match.
_ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
    ".css", ".js", ".json", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".webm",
    ".pdf", ".zip", ".map", ".php", ".html", ".htm", ".xml", ".txt", ".sql",
)
_ASSET_LOCAL_RE = re.compile(r"^\d+x$|^\d+px$|^[0-9a-f]{16,}$", re.IGNORECASE)

# Link text / hrefs that suggest a page worth crawling for contact details.
CONTACT_HINTS = (
    "contact", "contact-us", "contactus", "kontakt", "contacto", "get-in-touch",
    "reach-us", "reach-out", "about", "about-us", "aboutus", "team", "our-team",
    "staff", "our-staff", "people", "leadership", "management", "meet",
    "support", "help", "customer-service", "impressum", "legal", "privacy",
    "locations", "location", "our-office", "offices", "book", "booking",
    "appointment", "appointments", "schedule", "quote", "request-a-quote",
    "estimate", "free-estimate", "careers", "jobs", "employment", "info",
    "enquiry", "enquiries", "inquiry", "inquiries", "faq", "hours",
)


@dataclass
class Found:
    """One raw hit before it becomes an EmailCandidate."""

    email: str
    source: str
    context: str = ""


def _decode_cfemail(hex_string: str) -> str:
    """Cloudflare XORs the address with the first byte."""
    try:
        data = bytes.fromhex(hex_string)
    except ValueError:
        return ""
    if len(data) < 2:
        return ""
    key = data[0]
    try:
        return "".join(chr(b ^ key) for b in data[1:])
    except ValueError:  # pragma: no cover
        return ""


def _plausible(email: str) -> bool:
    """Reject matches that are structurally emails but obviously not addresses."""
    if not email or email.count("@") != 1:
        return False
    local, _, domain = email.partition("@")
    if len(email) > 254 or len(local) > 64 or not domain:
        return False
    lowered = email.lower()
    if lowered.endswith(_ASSET_SUFFIXES):
        return False
    if any(f"{suffix}?" in lowered or f"{suffix}#" in lowered for suffix in _ASSET_SUFFIXES):
        return False
    if _ASSET_LOCAL_RE.match(local):
        return False
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    if domain.startswith("-") or domain.endswith("-") or ".." in domain:
        return False
    if not has_valid_suffix(domain):
        return False
    # Minified-JS artefacts: long hex/base64-ish local parts with no separators.
    if len(local) > 40 and not any(c in local for c in "._-+"):
        return False
    if domain.lower() in JUNK_EMAIL_DOMAINS:
        return False
    if registered_domain(domain) in JUNK_EMAIL_DOMAINS:
        return False
    return True


def _normalize(email: str) -> str:
    email = html.unescape(email or "").strip().strip("'\"<>(),;:").lower()
    email = email.rstrip(".")
    if email.startswith("mailto:"):
        email = email[len("mailto:"):]
    return email


def _iter_regex(text: str, source: str) -> Iterable[Found]:
    for match in EMAIL_RE.finditer(text or ""):
        email = _normalize(match.group(0))
        if _plausible(email):
            yield Found(email, source, trim_context(text, match.start()))


# Ordinary prose that the obfuscation pattern would otherwise read as an
# address: "join us at meetup.com", "find us at facebook.com".
_PROSE_LOCALS = {
    "us", "me", "them", "him", "her", "it", "you", "we", "here", "there", "now",
    "available", "found", "visit", "see", "look", "shop", "order", "book", "call",
    "located", "back", "more", "online", "out", "up", "in", "on", "and", "or",
    "the", "a", "at", "to", "of", "for", "is", "are", "was", "be", "our", "your",
    "info", "details", "menu", "reviews", "page", "site", "website", "profile",
}


def _iter_obfuscated(text: str, business_domain: str = "") -> Iterable[Found]:
    for match in OBFUSCATED_RE.finditer(text or ""):
        local = squeeze(match.group(1)).replace(" ", "")
        domain_raw = match.group(2)
        at_part = match.group(0)[len(match.group(1)):][: -len(domain_raw)]
        # Rebuild "example (dot) com" -> "example.com"
        domain = re.sub(_DOT, ".", domain_raw, flags=re.IGNORECASE)
        domain = re.sub(r"\s+", "", html.unescape(domain)).strip(".")
        email = _normalize(f"{local}@{domain}")
        if not _plausible(email):
            continue
        plain_at = bool(re.fullmatch(r"\s*at\s*", at_part, re.IGNORECASE))
        plain_dot = not re.search(r"\(|\[|\{|\bdot\b|&#", domain_raw, re.IGNORECASE)
        if plain_at and plain_dot:
            # "word at host.tld" is only an address when the word is a mailbox
            # name and the host is the business's own domain.
            if local.lower() in _PROSE_LOCALS:
                continue
            if not business_domain or registered_domain(domain) != registered_domain(business_domain):
                continue
        yield Found(email, SOURCE_OBFUSCATED, trim_context(text, match.start()))


def _iter_mailto(soup: BeautifulSoup) -> Iterable[Found]:
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        if not href.lower().startswith("mailto:"):
            continue
        target = unquote(href[len("mailto:"):]).split("?")[0]
        for part in re.split(r"[,;]", target):
            email = _normalize(part)
            if _plausible(email):
                yield Found(email, SOURCE_MAILTO, squeeze(anchor.get_text(" ", strip=True))[:120])
        # mailto:?to=/cc= query forms
        query = href.split("?", 1)[1] if "?" in href else ""
        if query:
            for found in _iter_regex(unquote(query), SOURCE_MAILTO):
                yield found


def _iter_cloudflare(raw_html: str, soup: BeautifulSoup) -> Iterable[Found]:
    hexes: list[str] = []
    for tag in soup.select("[data-cfemail]"):
        value = tag.get("data-cfemail")
        if value:
            hexes.append(str(value))
    hexes.extend(CF_ATTR_RE.findall(raw_html or ""))
    hexes.extend(CF_LINK_RE.findall(raw_html or ""))
    for hex_string in hexes:
        email = _normalize(_decode_cfemail(hex_string))
        if _plausible(email):
            yield Found(email, SOURCE_CLOUDFLARE, "cloudflare-protected")


def _iter_jsonld(soup: BeautifulSoup) -> Iterable[Found]:
    for script in soup.select('script[type="application/ld+json"]'):
        body = script.string or script.get_text() or ""
        if not body.strip():
            continue
        try:
            data = json.loads(body)
        except ValueError:
            yield from _iter_regex(body, SOURCE_JSONLD)
            continue
        for value in _walk_json(data):
            email = _normalize(str(value))
            if _plausible(email):
                yield Found(email, SOURCE_JSONLD, "schema.org")


def _walk_json(node: object) -> Iterable[object]:
    """Yield values of email-ish keys plus any string containing '@'."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and ("email" in str(key).lower() or "@" in value):
                yield value.replace("mailto:", "")
            else:
                yield from _walk_json(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk_json(item)


_NOISE_TAGS = ("script", "style", "noscript", "svg", "template")
_CHROME_TAGS = ("nav", "footer")


@dataclass
class ParsedPage:
    """One HTML parse, shared by every consumer of a page.

    Parsing with lxml is the single biggest CPU cost per business, and the
    email extractor, the owner finder, the link ranker and the homepage text
    each used to build their own tree from the same bytes. Now the tree is
    built once; the soup-based extractors run first (JSON-LD and Cloudflare
    hints live in tags the text pass strips), then the tree is reduced to
    visible text twice: with and without the site chrome.
    """

    raw_html: str
    title: str = ""
    text: str = ""                 # visible text, single spaces
    people_text: str = ""          # visible text minus nav/footer, " | " between blocks
    jsonld_bodies: list[str] = None  # type: ignore[assignment]
    soup_found: list[Found] = None   # type: ignore[assignment]  mailto / cloudflare / json-ld hits
    links: list[str] = None          # type: ignore[assignment]  ranked same-site links (if asked)


# A parsed tree is ten times the size of the HTML. Parsing is CPU-bound and
# serialised by the interpreter anyway, so letting every socket's page be
# parsed at once only multiplied memory (hundreds of trees at a time was a
# fast way to exhaust a laptop); a few at a time costs no speed.
_PARSE_GATE = __import__("threading").BoundedSemaphore(4)


def parse_page(raw_html: str, base_url: str = "", *, want_links: bool = False,
               link_limit: int = 40) -> ParsedPage:
    page = ParsedPage(raw_html or "", jsonld_bodies=[], soup_found=[], links=[])
    if not raw_html:
        return page
    with _PARSE_GATE:
        return _parse_page_locked(page, raw_html, base_url, want_links, link_limit)


def _parse_page_locked(page: ParsedPage, raw_html: str, base_url: str, want_links: bool,
                       link_limit: int) -> ParsedPage:
    soup = BeautifulSoup(raw_html, "lxml")
    page.title = soup.title.get_text(" ", strip=True) if soup.title else ""
    page.soup_found.extend(_iter_mailto(soup))
    page.soup_found.extend(_iter_cloudflare(raw_html, soup))
    page.soup_found.extend(_iter_jsonld(soup))
    page.jsonld_bodies = [
        (script.string or script.get_text() or "")
        for script in soup.select('script[type="application/ld+json"]')
    ]
    if want_links:
        page.links = _rank_links(soup, base_url, link_limit)
    for tag in soup(list(_NOISE_TAGS)):
        tag.decompose()
    page.text = soup.get_text(" ", strip=True)
    for tag in soup(list(_CHROME_TAGS)):
        tag.decompose()
    page.people_text = squeeze(soup.get_text(" | ", strip=True))
    return page


def _visible_text(soup: BeautifulSoup) -> str:
    clone = BeautifulSoup(str(soup), "lxml")
    for tag in clone(list(_NOISE_TAGS)):
        tag.decompose()
    return clone.get_text(" ", strip=True)


def extract_emails(raw_html: str, page_url: str = "", business_domain: str = "",
                   parsed: "ParsedPage | None" = None) -> list[Found]:
    """All plausible email hits on one page, best-provenance-first per address."""
    if not raw_html:
        return []
    if parsed is None:
        parsed = parse_page(raw_html)
    text = parsed.text

    found: list[Found] = []
    found.extend(parsed.soup_found)
    found.extend(_iter_regex(text, SOURCE_HTML_TEXT))
    found.extend(_iter_regex(html.unescape(raw_html), SOURCE_HTML_TEXT))
    found.extend(_iter_obfuscated(text, business_domain))
    found.extend(_iter_obfuscated(html.unescape(raw_html), business_domain))

    # Keep the strongest source per address, preferring one with context.
    from ..models import SOURCE_WEIGHT

    best: dict[str, Found] = {}
    for item in found:
        current = best.get(item.email)
        if current is None:
            best[item.email] = item
            continue
        better_source = SOURCE_WEIGHT.get(item.source, 0) > SOURCE_WEIGHT.get(current.source, 0)
        if better_source or (not current.context and item.context):
            best[item.email] = item
    return list(best.values())


def to_candidates(
    found: Iterable[Found],
    *,
    page_url: str,
    business_domain: str = "",
) -> list[EmailCandidate]:
    """Turn raw hits into classified EmailCandidates."""
    candidates: list[EmailCandidate] = []
    for item in found:
        local, _, domain = item.email.partition("@")
        candidates.append(
            EmailCandidate(
                email=item.email,
                source=item.source,
                source_url=page_url,
                context=item.context,
                is_role=local in ROLE_LOCAL_PARTS,
                is_personal_domain=domain in FREE_MAIL_DOMAINS,
                is_low_value=local in LOW_VALUE_LOCAL_PARTS,
                on_business_domain=bool(business_domain)
                and registered_domain(domain) == registered_domain(business_domain),
            )
        )
    return candidates


def score_link(href: str, text: str) -> int:
    """How likely a link leads to contact info (higher = crawl sooner)."""
    haystack = f"{href} {text}".lower()
    score = 0
    for index, hint in enumerate(CONTACT_HINTS):
        if hint in haystack:
            # Earlier hints (contact, about, team) are the strongest signals.
            score += max(6 - (index // 6), 1) * 10
            break
    path = urlsplit(href).path.strip("/").lower()
    if path in {"contact", "contact-us", "contactus", "about", "about-us"}:
        score += 40
    if "mailto" in haystack:
        score += 50
    depth = path.count("/")
    score -= depth * 5
    if any(bad in haystack for bad in ("blog/", "/news", "/product", "cart", "checkout",
                                       "/tag/", "/category/", "?add-to-cart", "/wp-")):
        score -= 30
    return score


def find_internal_links(raw_html: str, base_url: str, limit: int = 40,
                        parsed: "ParsedPage | None" = None) -> list[str]:
    """Same-site links ranked by how likely they hold contact details."""
    if parsed is not None and parsed.links:
        return parsed.links[:limit]
    return _rank_links(BeautifulSoup(raw_html or "", "lxml"), base_url, limit)


def _rank_links(soup: BeautifulSoup, base_url: str, limit: int) -> list[str]:
    from urllib.parse import urljoin

    from ..util import same_site

    scored: dict[str, int] = {}
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "tel:", "mailto:", "sms:", "data:")):
            continue
        absolute = urljoin(base_url, href).split("#")[0]
        if not absolute.startswith(("http://", "https://")):
            continue
        if not same_site(absolute, base_url):
            continue
        if any(absolute.lower().endswith(ext) for ext in _ASSET_SUFFIXES if ext != ".html"):
            continue
        text = squeeze(anchor.get_text(" ", strip=True))[:80]
        score = score_link(absolute, text)
        if absolute not in scored or score > scored[absolute]:
            scored[absolute] = score
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], len(kv[0])))
    return [url for url, score in ranked[:limit] if score > 0]


def social_links(raw_html: str) -> list[str]:
    """Social profile URLs (a fallback place to look for a contact address)."""
    soup = BeautifulSoup(raw_html or "", "lxml")
    out: list[str] = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        host = urlsplit(href).netloc.lower().removeprefix("www.")
        if host in PLATFORM_DOMAINS and any(
            key in host for key in ("facebook", "instagram", "linkedin", "twitter", "x.com")
        ):
            out.append(href)
    return out[:6]
