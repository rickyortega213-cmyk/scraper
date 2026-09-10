"""Confidence scoring for email candidates.

Confidence is 0-100 and blends three things:
  1. provenance  - a mailto: on the site beats a guessed pattern
  2. verification - what the verification API said about deliverability
  3. context     - role vs personal mailbox, on-domain vs free mail, chain status

The score is what `BusinessResult.best_email` sorts on, and what `--min-confidence`
filters on, so it is deliberately conservative about guesses: an unverified
permutation can never outrank a scraped address.
"""

from __future__ import annotations

from typing import Optional

from ..models import (
    BusinessResult,
    EmailCandidate,
    SOURCE_WEIGHT,
    V_CATCH_ALL,
    V_DISPOSABLE,
    V_INVALID,
    V_RISKY,
    V_SKIPPED,
    V_UNKNOWN,
    V_VALID,
)

# Base score by provenance.
BASE_BY_SOURCE = {
    "mailto": 70,
    "jsonld": 68,
    "cloudflare_decoded": 66,
    "html_text": 64,
    "obfuscated": 60,
    "maps_api": 62,
    "search_snippet": 52,
    "permutation": 22,
}

# Verification adjustment.
VERIFY_DELTA = {
    V_VALID: +28,
    V_CATCH_ALL: +4,
    V_RISKY: -4,
    V_UNKNOWN: 0,
    V_SKIPPED: 0,
    V_DISPOSABLE: -45,
    V_INVALID: -60,
}


def score_candidate(
    candidate: EmailCandidate,
    *,
    is_chain: bool = False,
    domain_is_catch_all: Optional[bool] = None,
) -> int:
    """Assign and return `candidate.confidence`."""
    score = BASE_BY_SOURCE.get(candidate.source, 40)

    verification = candidate.verification
    status = verification.status if verification else V_SKIPPED
    score += VERIFY_DELTA.get(status, 0)

    if verification and verification.score is not None:
        # Vendors report 0-100 (or 0-10); nudge by up to +/-6.
        raw = verification.score
        normalized = raw * 10 if raw <= 10 else raw
        score += int(round((normalized - 50) / 8.0))

    # A guess on a catch-all domain "verifies" but proves nothing.
    if candidate.from_permutation and (
        domain_is_catch_all or (verification and verification.is_catch_all)
    ):
        score -= 18
        if "catch_all_domain_guess_unproven" not in candidate.notes:
            candidate.notes.append("catch_all_domain_guess_unproven")

    if candidate.on_business_domain and not candidate.from_permutation:
        score += 8
    if candidate.is_personal_domain:
        # Normal and useful for local businesses - a real human reads it.
        score += 2 if not candidate.from_permutation else -25
    if candidate.is_low_value:
        score -= 35
    if candidate.is_role and not candidate.from_permutation:
        score += 2
    if is_chain:
        score -= 10
    if candidate.context and not candidate.from_permutation:
        score += 2

    candidate.confidence = max(0, min(100, score))
    return candidate.confidence


def score_business(result: BusinessResult) -> BusinessResult:
    """Score every candidate on a business and sort best-first."""
    for candidate in result.emails:
        score_candidate(
            candidate,
            is_chain=result.is_chain,
            domain_is_catch_all=result.domain_is_catch_all,
        )
    result.emails.sort(
        key=lambda c: (c.confidence, SOURCE_WEIGHT.get(c.source, 0), not c.from_permutation),
        reverse=True,
    )
    return result


def keep_candidate(candidate: EmailCandidate, *, keep_risky: bool, keep_invalid: bool) -> bool:
    """Output filter applied after scoring."""
    status = candidate.status
    if status in {V_INVALID, V_DISPOSABLE} and not keep_invalid:
        return False
    if status == V_RISKY and not keep_risky:
        return False
    if candidate.is_low_value:
        return False
    return True
