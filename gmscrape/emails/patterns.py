"""Generate likely email addresses for a domain when none could be scraped.

Ordered by real-world hit rate for small local businesses, in tiers so you can
trade verification credits for coverage:

  tier 1 - info@, contact@, hello@            (the overwhelming majority)
  tier 2 - + office@, admin@, sales@, ...     (default)
  tier 3 - + booking@, service@, quotes@, ... (aggressive)

Guessing is deliberately refused for free-mail domains (you cannot guess
someone's gmail), site-builder/social hosts, and national chains, since a
guess there is either impossible or worthless.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from ..data.domains import FREE_MAIL_DOMAINS, JUNK_EMAIL_DOMAINS, PLATFORM_DOMAINS
from ..models import CONTACT_OWNER, EmailCandidate, Person, SOURCE_PERMUTATION
from ..util import (
    domain_has_mx,
    name_tokens,
    normalize_name,
    registered_domain,
)

TIER_1 = ("info", "contact", "hello")
TIER_2 = ("office", "admin", "sales", "team", "mail", "support")
TIER_3 = (
    "service", "customerservice", "help", "inquiries", "enquiries",
    "booking", "bookings", "appointments", "reception", "frontdesk",
    "orders", "shop", "quotes", "estimates", "hi", "accounts", "billing",
    "schedule", "dispatch", "care", "general",
)

# Industry-specific mailboxes, added when the business category matches.
INDUSTRY_LOCALS: dict[str, tuple[str, ...]] = {
    r"dent|orthodont|endodont|periodont": ("frontdesk", "newpatients", "smile", "scheduling"),
    r"medical|clinic|doctor|physician|urgent care|pediatric|derma|chiro|health": (
        "appointments", "frontoffice", "patients", "scheduling",
    ),
    r"law|attorney|legal|lawyer": ("intake", "casemanager", "newclients", "paralegal"),
    r"real estate|realtor|realty|broker": ("listings", "leasing", "agents"),
    r"restaurant|pizza|cafe|coffee|bakery|bar|grill|diner|catering|food": (
        "catering", "events", "orders", "reservations", "manager",
    ),
    r"salon|spa|barber|nail|hair|lash|beauty|massage|tattoo": (
        "bookings", "appointments", "frontdesk", "studio",
    ),
    r"plumb|hvac|electric|roof|construct|contractor|remodel|landscap|clean|pest|garage": (
        "dispatch", "estimates", "service", "scheduling", "quotes",
    ),
    r"auto|mechanic|body shop|tire|collision|detail": ("service", "parts", "estimates"),
    r"gym|fitness|yoga|pilates|crossfit|martial|dance": ("membership", "frontdesk", "training"),
    r"hotel|motel|inn|resort|lodge": ("reservations", "frontdesk", "events", "stay"),
    r"vet|animal|pet": ("frontdesk", "appointments", "reception"),
    r"school|academy|tutor|daycare|childcare|learning|preschool": (
        "admissions", "enrollment", "office", "registrar",
    ),
    r"insur|financ|account|tax|cpa|bookkeep|mortgage|loan": (
        "claims", "quotes", "clientservices", "newbusiness",
    ),
    r"photograph|video|studio|design|marketing|agency|print": (
        "studio", "bookings", "projects", "creative",
    ),
    r"moving|storage|haul|junk|logistic|courier|delivery": ("dispatch", "quotes", "bookings"),
}

# "Joe's Plumbing" -> also try joe@, "Smith & Sons" -> smith@
_POSSESSIVE_RE = re.compile(r"\b([A-Za-z][A-Za-z\-]{1,20})'s\b")
_SUFFIX_WORDS = {
    "llc", "inc", "corp", "corporation", "co", "company", "ltd", "limited",
    "pllc", "pc", "pa", "lp", "llp", "group", "the", "and", "of", "at", "in",
    "services", "service", "solutions", "professional", "professionals",
}


@dataclass
class PermutationPlan:
    """What we intend to guess for one domain, and why we might not."""

    domain: str
    candidates: list[EmailCandidate]
    skipped_reason: str = ""

    @property
    def allowed(self) -> bool:
        return not self.skipped_reason


def domain_is_guessable(domain: str) -> tuple[bool, str]:
    """Whether guessing addresses on this domain can ever make sense."""
    domain = (domain or "").strip().lower()
    if not domain:
        return False, "no_domain"
    registered = registered_domain(domain) or domain
    if registered in FREE_MAIL_DOMAINS:
        return False, "free_mail_domain"
    if registered in PLATFORM_DOMAINS:
        return False, "platform_or_social_domain"
    if registered in JUNK_EMAIL_DOMAINS:
        return False, "not_a_business_domain"
    return True, ""


def personal_locals_from_name(business_name: str) -> list[str]:
    """Owner-style local parts implied by the business name.

    Local businesses are often named after their owner, and `joe@joesplumbing.com`
    is a very common real mailbox.
    """
    out: list[str] = []
    for match in _POSSESSIVE_RE.finditer(business_name or ""):
        token = normalize_name(match.group(1)).replace(" ", "")
        if token and token not in _SUFFIX_WORDS:
            out.append(token)
    tokens = [t for t in name_tokens(business_name) if t not in _SUFFIX_WORDS and len(t) > 2]
    if tokens and not out:
        # A two-word name whose first token is not an industry word is often a
        # surname ("Kowalski Roofing" -> kowalski@).
        if len(tokens) <= 3:
            out.append(tokens[0])
    seen: set[str] = set()
    unique: list[str] = []
    for token in out:
        if token not in seen and 2 < len(token) <= 24:
            seen.add(token)
            unique.append(token)
    return unique[:2]


def industry_locals(category: str, business_type: str = "") -> list[str]:
    haystack = f"{category} {business_type}".lower()
    out: list[str] = []
    for pattern, locals_ in INDUSTRY_LOCALS.items():
        if re.search(pattern, haystack):
            out.extend(locals_)
    return out


def local_parts_for_tier(tier: int) -> list[str]:
    parts = list(TIER_1)
    if tier >= 2:
        parts += list(TIER_2)
    if tier >= 3:
        parts += list(TIER_3)
    return parts


def owner_locals_from_person(full_name: str) -> list[str]:
    """first@, first.last@, flast@ ... for a known contact person."""
    tokens = [t for t in name_tokens(full_name) if t not in _SUFFIX_WORDS]
    if not tokens:
        return []
    first = tokens[0]
    if len(tokens) == 1:
        return [first]
    last = tokens[-1]
    return [
        first,
        f"{first}.{last}",
        f"{first[0]}{last}",
        f"{first}{last}",
        f"{first}_{last}",
        last,
    ]


def build_permutations(
    domain: str,
    *,
    business_name: str = "",
    category: str = "",
    business_type: str = "",
    owner_name: str = "",
    tier: int = 2,
    max_candidates: int = 12,
    require_mx: bool = True,
    is_chain: bool = False,
    allow_chains: bool = False,
    exclude: Sequence[str] = (),
) -> PermutationPlan:
    """Ordered guess list for `domain`, or a plan explaining why we skipped."""
    registered = registered_domain(domain) or (domain or "").strip().lower()
    if is_chain and not allow_chains:
        return PermutationPlan(domain=registered, candidates=[], skipped_reason="national_chain")
    guessable, reason = domain_is_guessable(registered)
    if not guessable:
        return PermutationPlan(domain=registered, candidates=[], skipped_reason=reason)
    if require_mx and not domain_has_mx(registered):
        return PermutationPlan(domain=registered, candidates=[], skipped_reason="domain_has_no_mx")

    ordered: list[str] = []
    ordered.extend(local_parts_for_tier(tier))
    if owner_name:
        ordered.extend(owner_locals_from_person(owner_name))
    if tier >= 2:
        ordered.extend(personal_locals_from_name(business_name))
    if tier >= 2:
        ordered.extend(industry_locals(category, business_type))

    excluded = {e.strip().lower() for e in exclude}
    seen: set[str] = set()
    candidates: list[EmailCandidate] = []
    for local in ordered:
        local = local.strip().lower()
        if not local or local in seen:
            continue
        seen.add(local)
        email = f"{local}@{registered}"
        if email in excluded:
            continue
        candidates.append(
            EmailCandidate(
                email=email,
                source=SOURCE_PERMUTATION,
                pattern=f"{local}@{{domain}}",
                is_role=True,
                on_business_domain=True,
                notes=["guessed_pattern"],
            )
        )
        if len(candidates) >= max_candidates:
            break
    return PermutationPlan(domain=registered, candidates=candidates)


def build_owner_permutations(
    domain: str,
    person: Person,
    *,
    max_candidates: int = 8,
    require_mx: bool = True,
    is_chain: bool = False,
    allow_chains: bool = False,
    exclude: Sequence[str] = (),
) -> PermutationPlan:
    """Likely mailboxes for a named person at the business domain.

    Ordered by how common each pattern is at small businesses:
    first@, first.last@, flast@, firstl@, first_last@, firstlast@, last@, f.last@
    """
    from .people import owner_local_parts

    registered = registered_domain(domain) or (domain or "").strip().lower()
    if is_chain and not allow_chains:
        return PermutationPlan(domain=registered, candidates=[], skipped_reason="national_chain")
    guessable, reason = domain_is_guessable(registered)
    if not guessable:
        return PermutationPlan(domain=registered, candidates=[], skipped_reason=reason)
    if require_mx and not domain_has_mx(registered):
        return PermutationPlan(domain=registered, candidates=[], skipped_reason="domain_has_no_mx")
    locals_ = owner_local_parts(person)
    if not locals_:
        return PermutationPlan(domain=registered, candidates=[], skipped_reason="no_usable_name")

    excluded = {e.strip().lower() for e in exclude}
    candidates: list[EmailCandidate] = []
    for local in locals_:
        email = f"{local}@{registered}"
        if email in excluded:
            continue
        candidates.append(
            EmailCandidate(
                email=email,
                source=SOURCE_PERMUTATION,
                pattern=f"{_pattern_label(local, person)}@{{domain}}",
                is_role=False,
                on_business_domain=True,
                contact_type=CONTACT_OWNER,
                contact_name=person.name,
                contact_title=person.title,
                notes=["guessed_owner_pattern"],
            )
        )
        if len(candidates) >= max_candidates:
            break
    return PermutationPlan(domain=registered, candidates=candidates)


def _pattern_label(local: str, person: Person) -> str:
    first, last = person.first, person.last
    label = local
    if last:
        label = label.replace(last, "{last}") if last in local else label
        label = label.replace(last[0], "{l}", 1) if "{last}" not in label and last[0] in local else label
    if first:
        label = label.replace(first, "{first}") if first in label else label
        if "{first}" not in label and label.startswith(first[0]):
            label = "{f}" + label[1:]
    return label
