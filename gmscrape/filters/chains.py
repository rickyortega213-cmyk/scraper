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

from ..data.chains import CHAIN_BRANDS, CHAIN_DOMAINS, FRANCHISE_HINTS
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

    return ChainVerdict(is_chain=score >= CHAIN_THRESHOLD, score=score, reasons=reasons)


def should_keep(verdict: ChainVerdict, mode: str) -> bool:
    """Apply --chain-mode (flag | skip | only) to a verdict."""
    mode = (mode or "flag").lower()
    if mode == "skip":
        return not verdict.is_chain
    if mode == "only":
        return verdict.is_chain
    return True
