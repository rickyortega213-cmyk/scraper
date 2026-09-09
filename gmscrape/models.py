"""Normalized data models shared across providers, extractors and exporters."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# --- email discovery sources, ordered best -> worst -------------------------
SOURCE_MAILTO = "mailto"
SOURCE_HTML_TEXT = "html_text"
SOURCE_OBFUSCATED = "obfuscated"
SOURCE_CLOUDFLARE = "cloudflare_decoded"
SOURCE_JSONLD = "jsonld"
SOURCE_MAPS = "maps_api"
SOURCE_PERMUTATION = "permutation"

# Higher = more trustworthy provenance.
SOURCE_WEIGHT = {
    SOURCE_MAILTO: 100,
    SOURCE_JSONLD: 95,
    SOURCE_CLOUDFLARE: 92,
    SOURCE_HTML_TEXT: 90,
    SOURCE_OBFUSCATED: 85,
    SOURCE_MAPS: 80,
    SOURCE_PERMUTATION: 40,
}

# --- verification statuses (normalized across vendors) ---------------------
V_VALID = "valid"
V_INVALID = "invalid"
V_RISKY = "risky"
V_CATCH_ALL = "catch_all"
V_DISPOSABLE = "disposable"
V_UNKNOWN = "unknown"
V_SKIPPED = "skipped"


@dataclass
class QuerySpec:
    """A parsed 'business type in location' search query."""

    raw: str
    business_type: str
    location: str = ""

    @property
    def search_string(self) -> str:
        if self.location:
            return f"{self.business_type} in {self.location}"
        return self.business_type

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Place:
    """A business returned by a Google Maps provider."""

    name: str
    query: str = ""
    source: str = ""
    place_id: str = ""
    category: str = ""
    address: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""
    phone: str = ""
    website: str = ""
    domain: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    rating: Optional[float] = None
    reviews: Optional[int] = None
    price_level: str = ""
    hours: str = ""
    google_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def dedupe_key(self) -> str:
        """Stable identity for a business across overlapping queries."""
        if self.place_id:
            return f"pid:{self.place_id}".lower()
        if self.domain:
            return f"dom:{self.domain}".lower()
        if self.phone:
            digits = "".join(c for c in self.phone if c.isdigit())[-10:]
            if len(digits) >= 7:
                return f"tel:{digits}"
        return f"na:{self.name.strip().lower()}|{self.address.strip().lower()}"


@dataclass
class VerificationResult:
    """Normalized output of an email verification provider."""

    status: str = V_UNKNOWN
    provider: str = ""
    score: Optional[float] = None
    sub_status: str = ""
    is_catch_all: bool = False
    is_disposable: bool = False
    is_role: bool = False
    free: bool = False
    mx_found: Optional[bool] = None
    checked_at: float = field(default_factory=time.time)
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def deliverable(self) -> bool:
        return self.status == V_VALID


@dataclass
class EmailCandidate:
    """One candidate email for a business, with provenance and verification."""

    email: str
    source: str = SOURCE_HTML_TEXT
    source_url: str = ""
    pattern: str = ""          # set for permutations, e.g. "info@{domain}"
    context: str = ""          # short snippet around where it was found
    is_role: bool = False      # info@, sales@, ...
    is_personal_domain: bool = False   # gmail/hotmail/yahoo/...
    is_low_value: bool = False  # noreply@, postmaster@, ...
    on_business_domain: bool = False   # local part hosted on the business domain
    verification: Optional[VerificationResult] = None
    confidence: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def local_part(self) -> str:
        return self.email.rsplit("@", 1)[0]

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower()

    @property
    def from_permutation(self) -> bool:
        return self.source == SOURCE_PERMUTATION

    @property
    def status(self) -> str:
        return self.verification.status if self.verification else V_SKIPPED


@dataclass
class BusinessResult:
    """A place plus everything we discovered about how to email it."""

    place: Place
    emails: list[EmailCandidate] = field(default_factory=list)
    is_chain: bool = False
    chain_score: int = 0
    chain_reasons: list[str] = field(default_factory=list)
    website_status: str = ""        # "", "ok", "no_website", "unreachable:<detail>"
    pages_crawled: list[str] = field(default_factory=list)
    domain_has_mx: Optional[bool] = None
    domain_is_catch_all: Optional[bool] = None
    permutations_skipped_reason: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def best_email(self) -> Optional[EmailCandidate]:
        if not self.emails:
            return None
        return max(self.emails, key=lambda e: (e.confidence, SOURCE_WEIGHT.get(e.source, 0)))

    @property
    def found_emails(self) -> list[EmailCandidate]:
        return [e for e in self.emails if not e.from_permutation]

    @property
    def guessed_emails(self) -> list[EmailCandidate]:
        return [e for e in self.emails if e.from_permutation]
