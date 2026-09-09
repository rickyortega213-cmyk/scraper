"""Zero-cost local verification: syntax, MX/DNS and disposable/junk checks.

This is the default when no verification API key is configured, and it always
runs first as a pre-filter so paid credits are never spent on addresses that
cannot possibly deliver.
"""

from __future__ import annotations

import re
from typing import Optional

from ...data.domains import (
    DISPOSABLE_DOMAINS,
    FREE_MAIL_DOMAINS,
    JUNK_EMAIL_DOMAINS,
    LOW_VALUE_LOCAL_PARTS,
    ROLE_LOCAL_PARTS,
)
from ...models import (
    VerificationResult,
    V_DISPOSABLE,
    V_INVALID,
    V_RISKY,
)
from ...util import domain_has_mx, has_valid_suffix
from ..base import EmailVerifier

# Deliberately practical rather than RFC-exhaustive.
EMAIL_SYNTAX_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def syntax_ok(email: str) -> bool:
    if not email or email.count("@") != 1 or len(email) > 254:
        return False
    local, _, domain = email.partition("@")
    if not local or local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    if ".." in domain or domain.startswith("-") or domain.endswith("-"):
        return False
    return bool(EMAIL_SYNTAX_RE.match(email)) and has_valid_suffix(domain)


class LocalVerifier(EmailVerifier):
    """Syntax + DNS only. Never reports `valid` - the best it can say is risky."""

    name = "local"
    requires_key = False

    def verify(self, email: str) -> VerificationResult:
        email = (email or "").strip().lower()
        local, _, domain = email.partition("@")
        result = VerificationResult(provider=self.name)
        result.is_role = local in ROLE_LOCAL_PARTS
        result.free = domain in FREE_MAIL_DOMAINS

        if not syntax_ok(email):
            result.status = V_INVALID
            result.sub_status = "bad_syntax"
            return result
        if domain in DISPOSABLE_DOMAINS:
            result.status = V_DISPOSABLE
            result.is_disposable = True
            result.sub_status = "disposable_domain"
            return result
        if domain in JUNK_EMAIL_DOMAINS:
            result.status = V_INVALID
            result.sub_status = "boilerplate_domain"
            return result
        if local in LOW_VALUE_LOCAL_PARTS:
            result.status = V_INVALID
            result.sub_status = "no_reply_mailbox"
            return result

        has_mx = domain_has_mx(domain)
        result.mx_found = has_mx
        if not has_mx:
            result.status = V_INVALID
            result.sub_status = "no_mx_record"
            return result

        # Syntax + MX are fine; mailbox existence is unknown without SMTP.
        result.status = V_RISKY
        result.sub_status = "mx_ok_mailbox_unverified"
        result.score = 50.0
        return result

    def is_catch_all(self, domain: str) -> Optional[bool]:
        return None


def prefilter(email: str) -> Optional[VerificationResult]:
    """Cheap local reject. Returns a result only when the address is hopeless,
    so paid verification credits are never spent on it."""
    email = (email or "").strip().lower()
    local, _, domain = email.partition("@")
    if not syntax_ok(email):
        return VerificationResult(
            status=V_INVALID, provider="local", sub_status="bad_syntax"
        )
    if domain in DISPOSABLE_DOMAINS:
        return VerificationResult(
            status=V_DISPOSABLE,
            provider="local",
            sub_status="disposable_domain",
            is_disposable=True,
        )
    if domain in JUNK_EMAIL_DOMAINS:
        return VerificationResult(
            status=V_INVALID, provider="local", sub_status="boilerplate_domain"
        )
    if local in LOW_VALUE_LOCAL_PARTS:
        return VerificationResult(
            status=V_INVALID, provider="local", sub_status="no_reply_mailbox"
        )
    return None
