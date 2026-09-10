"""Find a business's website when Google Maps doesn't list one.

Search for the business, score every organic hit on how well its domain and
title match the business, and accept only a clear winner. Then - crucially -
the site is *confirmed* after the homepage is fetched: it must mention the
business's phone number, name, or street address, or it is thrown away. A
plausible-looking but wrong website is worse than none, because every email
scraped from it would be a confident, verified, wrong lead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlsplit

from ..data.chains import CHAIN_DOMAINS
from ..data.domains import PLATFORM_DOMAINS
from ..models import Place
from ..providers.search.base import SearchHit
from ..util import name_tokens, normalize_name, squeeze

# Words that carry no identity in a business name.
_GENERIC = {
    "the", "and", "of", "a", "an", "at", "in", "on", "for", "llc", "inc", "co",
    "company", "corp", "corporation", "ltd", "limited", "group", "services",
    "service", "solutions", "professional", "professionals", "pllc", "pc", "pa",
    "dba", "llp", "lp", "enterprises", "enterprise", "usa", "us", "america",
    "american", "local", "quality", "best", "new", "old", "north",
    "south", "east", "west", "central", "greater", "metro", "area",
}

# Extra directory / aggregator hosts beyond the platform list.
_DIRECTORY_HINTS = (
    "yellowpages", "yelp", "mapquest", "manta", "chamberofcommerce", "birdeye",
    "superpages", "citysearch", "merchantcircle", "hotfrog", "cylex", "brownbook",
    "foursquare", "tripadvisor", "zocdoc", "healthgrades", "vitals", "avvo",
    "justia", "lawyers", "findlaw", "houzz", "angi", "thumbtack", "porch",
    "homeadvisor", "bbb.org", "dnb.com", "buzzfile", "zoominfo", "crunchbase",
    "opencorporates", "bizapedia", "companieshouse", "glassdoor", "indeed",
    "linkedin", "facebook", "instagram", "nextdoor", "patch.com", "wikipedia",
    "google.com", "apple.com", "bing.com", "yahoo.com", "opentable", "doordash",
    "ubereats", "grubhub", "seamless", "postmates", "toasttab", "clover",
    "square.site", "booksy", "vagaro", "styleseat", "fresha", "mindbody",
    "carfax", "cars.com", "autotrader", "cargurus", "realtor.com", "zillow",
    "redfin", "trulia", "loopnet", "apartments.com", "expedia", "booking.com",
    "hotels.com", "tripadvisor", "kayak",
)


@dataclass
class WebsiteGuess:
    url: str
    domain: str
    score: int
    reasons: list[str] = field(default_factory=list)
    runner_up_score: int = 0

    @property
    def accepted(self) -> bool:
        return self.score > 0


def distinctive_tokens(name: str) -> list[str]:
    return [t for t in name_tokens(name) if t not in _GENERIC and len(t) > 1]


def discovery_query(place: Place) -> str:
    """The search that best isolates one local business."""
    parts = [f'"{squeeze(place.name)}"']
    if place.city:
        parts.append(place.city)
    elif place.address:
        parts.append(place.address.split(",")[-2].strip() if "," in place.address else place.address)
    if place.state:
        parts.append(place.state)
    return " ".join(p for p in parts if p)


def _phone_digits(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-7:] if len(digits) >= 7 else ""


def _is_directory(domain: str, host: str) -> bool:
    if domain in PLATFORM_DOMAINS or domain in CHAIN_DOMAINS:
        return True
    haystack = f"{host} {domain}".lower()
    return any(hint in haystack for hint in _DIRECTORY_HINTS)


def score_hit(hit: SearchHit, place: Place) -> tuple[int, list[str]]:
    domain = hit.domain
    if not domain:
        return 0, ["no_domain"]
    if _is_directory(domain, hit.host):
        return 0, ["directory_or_platform"]

    reasons: list[str] = []
    score = 0
    tokens = distinctive_tokens(place.name)
    concat = "".join(tokens)
    domain_label = domain.split(".")[0].replace("-", "")
    biz_norm = normalize_name(place.name)
    text_norm = normalize_name(hit.text)

    if tokens and (domain_label == concat or (len(concat) >= 6 and concat in domain_label)):
        score += 60
        reasons.append("domain_matches_name")
    elif tokens:
        present = [t for t in tokens if t in domain_label]
        if present:
            fraction = len(present) / len(tokens)
            gained = int(45 * fraction)
            score += gained
            reasons.append(f"domain_has_{len(present)}/{len(tokens)}_name_tokens")
        # A domain that is a bare surname/initials tells us little; a sole
        # generic label like "dental" tells us nothing.
    if biz_norm and biz_norm in text_norm:
        score += 25
        reasons.append("title_or_snippet_names_business")
    elif tokens:
        hits = sum(1 for t in tokens if t in text_norm)
        if hits >= max(1, (len(tokens) + 1) // 2):
            score += 12
            reasons.append("text_matches_most_name_tokens")

    digits = _phone_digits(place.phone)
    if digits and digits in re.sub(r"\D", "", hit.text):
        # The listing's own phone number in the snippet is close to conclusive.
        score += 25
        reasons.append("phone_in_snippet")
    if place.city and normalize_name(place.city) in text_norm:
        score += 8
        reasons.append("city_in_snippet")
    if hit.position == 1:
        score += 8
    elif hit.position == 2:
        score += 4
    if re.search(r"directory|listing|reviews?|near-?me|findа?|top-?10|best-?\d", hit.host):
        score -= 30
        reasons.append("aggregator_looking_host")
    return max(0, score), reasons


def pick_website(
    place: Place, hits: Iterable[SearchHit], *, min_score: int = 60, min_margin: int = 10
) -> Optional[WebsiteGuess]:
    """The single site we believe is theirs, or None when it isn't clear."""
    scored: list[tuple[int, list[str], SearchHit]] = []
    seen_domains: set[str] = set()
    for hit in hits:
        score, reasons = score_hit(hit, place)
        if score <= 0 or hit.domain in seen_domains:
            continue
        seen_domains.add(hit.domain)
        scored.append((score, reasons, hit))
    if not scored:
        return None
    scored.sort(key=lambda t: -t[0])
    best_score, reasons, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0
    if best_score < min_score:
        return None
    if runner_up and best_score - runner_up < min_margin:
        return None      # two plausible sites: don't gamble
    # A shallow hit (the homepage, /contact/, /about/) is the best page to
    # confirm from and the crawler finds the rest; a deep blog post is not, so
    # start those from the site root. Scheme and port are kept as-is.
    parts = urlsplit(best.url)
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) <= 1 and not parts.query:
        url = f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"
        if segments and not url.endswith("/") and "." not in segments[-1]:
            url += "/"
    else:
        url = f"{parts.scheme}://{parts.netloc}/"
    return WebsiteGuess(url=url, domain=best.domain, score=best_score,
                        reasons=reasons, runner_up_score=runner_up)


def confirm_website(place: Place, html_text: str) -> tuple[bool, list[str]]:
    """Does the fetched page actually belong to this business?"""
    text = squeeze(html_text or "")
    if not text:
        return False, ["empty_page"]
    reasons: list[str] = []
    digits = _phone_digits(place.phone)
    page_digits = re.sub(r"\D", "", text)
    if digits and digits in page_digits:
        reasons.append("phone_on_page")
    norm_text = normalize_name(text)
    biz_norm = normalize_name(place.name)
    tokens = distinctive_tokens(place.name)
    name_hit = ""
    if biz_norm and biz_norm in norm_text:
        name_hit = "name_on_page"
    elif tokens and all(t in norm_text for t in tokens):
        name_hit = "name_tokens_on_page"
    street = _street_key(place.street or place.address)
    if street and street in norm_text:
        reasons.append("street_on_page")
    if name_hit:
        # "Smile Dental" appears on every Smile Dental site in the country; a
        # short generic name only confirms alongside the phone or address.
        if reasons or len(tokens) >= 3 or not (place.phone or street):
            reasons.append(name_hit)
        else:
            return False, [f"{name_hit}_but_no_phone_or_address"]
    return bool(reasons), reasons or ["no_business_markers_on_page"]


def _street_key(address: str) -> str:
    """'1200 S Lamar Blvd, Austin, TX' -> '1200 s lamar'"""
    first = (address or "").split(",")[0]
    match = re.match(r"\s*(\d{1,6})\s+([A-Za-z0-9.'\- ]{2,40})", first)
    if not match:
        return ""
    words = normalize_name(match.group(2)).split()[:3]
    return f"{match.group(1)} {' '.join(words)}".strip()
