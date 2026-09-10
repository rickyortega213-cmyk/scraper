"""Tell a local business apart from a national chain or franchise branch.

Why it matters for this pipeline: guessing `info@walmart.com` is worthless,
a franchise branch's inbox is usually a corporate alias several states away,
and chains blow up your verification bill for leads you cannot sell to. So
chains are scored, flagged with reasons, and excluded from permutation
guessing by default (`--chain-mode` controls whether they are kept at all).

Emails actually *found* on a chain's site are still reported - they are real,
they are just rarely the local decision-maker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..data.chains import CHAIN_BRANDS, CHAIN_DOMAINS, CHAIN_KINDS, FRANCHISE_HINTS
from ..models import Place
from ..util import name_tokens, normalize_name, registered_domain

# "Store #1234", "- 0421", "Location 12"
_STORE_NUMBER_RE = re.compile(r"(?:#|no\.?\s*|store\s*|location\s*|unit\s*)\d{2,6}\b", re.IGNORECASE)
_TRAILING_NUMBER_RE = re.compile(r"\s[-–]\s*\d{2,6}$")

# Scores add up; >= CHAIN_THRESHOLD means "treat as a chain".
CHAIN_THRESHOLD = 50

SCORE_BRAND_EXACT = 100
SCORE_BRAND_PREFIX = 70
SCORE_DOMAIN = 100
SCORE_STORE_NUMBER = 25
SCORE_FRANCHISE_HINT = 20
SCORE_HIGH_REVIEWS = 15


@dataclass
class ChainVerdict:
    is_chain: bool
    score: int
    reasons: list[str]
    brand: str = ""            # normalized brand that matched, if any


@dataclass
class ChainProfile:
    """Who to look for at a chain location, and how to ask the web for them."""

    kind: str                       # franchise | corporate_store | corporate_restaurant
    contact_type: str               # owner | manager
    target_titles: tuple[str, ...]  # best first - boosts these when choosing a person
    queries: tuple[str, ...]        # templates with {brand} {name} {city} {state} {where}
    label: str                      # human description for notes / summaries


_PROFILES: dict[str, ChainProfile] = {
    "franchise": ChainProfile(
        kind="franchise",
        contact_type="owner",
        target_titles=("franchise owner", "franchisee", "owner/operator", "owner operator",
                       "owner", "operator", "co-owner", "general manager"),
        queries=(
            "who owns the {brand} franchise in {where}",
            "{brand} {where} franchisee owner",
        ),
        label="franchise owner",
    ),
    "corporate_store": ChainProfile(
        kind="corporate_store",
        contact_type="manager",
        target_titles=("store manager", "general manager", "branch manager", "store director",
                       "market manager", "district manager", "regional manager",
                       "location manager", "manager"),
        queries=(
            "who is the store manager of {brand} in {where}",
            "{brand} {where} store manager OR district manager OR regional manager",
        ),
        label="store / district manager",
    ),
    "corporate_restaurant": ChainProfile(
        kind="corporate_restaurant",
        contact_type="manager",
        target_titles=("general manager", "managing partner", "restaurant manager",
                       "kitchen manager", "district manager", "regional manager", "manager"),
        queries=(
            "who is the general manager of {brand} in {where}",
            "{brand} {where} general manager OR managing partner",
        ),
        label="general manager",
    ),
}

_FRANCHISE_CATEGORY_RE = re.compile(
    r"fast food|hamburger|pizza|sandwich|chicken|donut|coffee|ice cream|frozen yogurt|"
    r"hair salon|barber|fitness|gym|hotel|motel|inn\b|tax|insurance|real estate|"
    r"cleaning|plumb|hvac|heating|electric|handyman|pest|lawn|landscap|painting|"
    r"printing|shipping|mail|storage|car wash|oil change|auto repair|tire|tutoring|"
    r"learning|child care|day ?care|preschool|senior|home care|massage|wax|tanning|"
    r"convenience store|gas station",
    re.IGNORECASE,
)
_RESTAURANT_CATEGORY_RE = re.compile(
    r"restaurant|steak|grill|bar\b|diner|cafe|bistro|eatery|brewery|kitchen|buffet",
    re.IGNORECASE,
)


def chain_profile(place: Place, verdict: ChainVerdict) -> ChainProfile | None:
    """Which local person to look for at this chain location, or None if local."""
    if not verdict.is_chain:
        return None
    kind = CHAIN_KINDS.get(verdict.brand or "", "")
    if not kind:
        haystack = f"{place.category} {place.name}"
        if _FRANCHISE_CATEGORY_RE.search(haystack):
            kind = "franchise"
        elif _RESTAURANT_CATEGORY_RE.search(haystack):
            kind = "corporate_restaurant"
        else:
            kind = "corporate_store"
    return _PROFILES[kind]


def profile_for_kind(kind: str) -> ChainProfile | None:
    return _PROFILES.get(kind)


def brand_display_name(place: Place) -> str:
    """The brand as people write it: the listing name minus store numbers."""
    name = _TRAILING_NUMBER_RE.sub("", _STORE_NUMBER_RE.sub("", place.name)).strip(" -–—,")
    return name or place.name


def _brand_match(name: str) -> tuple[int, str]:
    normalized = normalize_name(name)
    if not normalized:
        return 0, ""
    stripped = _TRAILING_NUMBER_RE.sub("", _STORE_NUMBER_RE.sub("", normalized)).strip()
    if stripped in CHAIN_BRANDS:
        return SCORE_BRAND_EXACT, f"brand_exact:{stripped}"

    tokens = name_tokens(stripped)
    # Chain names usually lead the listing: "Starbucks Reserve Roastery".
    for length in (4, 3, 2):
        if len(tokens) >= length:
            prefix = " ".join(tokens[:length])
            if prefix in CHAIN_BRANDS:
                return SCORE_BRAND_PREFIX, f"brand_prefix:{prefix}"
    if tokens and tokens[0] in CHAIN_BRANDS and len(tokens[0]) > 4:
        return SCORE_BRAND_PREFIX, f"brand_prefix:{tokens[0]}"
    return 0, ""


def classify(place: Place, *, review_threshold: int = 1500) -> ChainVerdict:
    """Score how likely `place` belongs to a national chain."""
    score = 0
    reasons: list[str] = []

    brand_score, brand_reason = _brand_match(place.name)
    brand = brand_reason.split(":", 1)[1] if brand_score else ""
    if brand_score:
        score += brand_score
        reasons.append(brand_reason)

    domain = registered_domain(place.website or place.domain)
    if domain and domain in CHAIN_DOMAINS:
        score += SCORE_DOMAIN
        reasons.append(f"corporate_domain:{domain}")

    if _STORE_NUMBER_RE.search(place.name) or _TRAILING_NUMBER_RE.search(place.name):
        score += SCORE_STORE_NUMBER
        reasons.append("store_number_in_name")

    haystack = f"{place.name} {place.category}".lower()
    for hint in FRANCHISE_HINTS:
        if hint in haystack:
            score += SCORE_FRANCHISE_HINT
            reasons.append(f"franchise_hint:{hint}")
            break

    if place.reviews and place.reviews >= review_threshold:
        score += SCORE_HIGH_REVIEWS
        reasons.append(f"reviews>={review_threshold}")

    if not brand and domain in CHAIN_DOMAINS:
        brand = normalize_name(domain.rsplit(".", 1)[0].replace("-", " "))
    return ChainVerdict(is_chain=score >= CHAIN_THRESHOLD, score=score, reasons=reasons, brand=brand)


def should_keep(verdict: ChainVerdict, mode: str) -> bool:
    """Apply --chain-mode (flag | skip | only) to a verdict."""
    mode = (mode or "flag").lower()
    if mode == "skip":
        return not verdict.is_chain
    if mode == "only":
        return verdict.is_chain
    return True
