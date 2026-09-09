"""Adapters for the common commercial email-verification APIs.

Every adapter maps the vendor's own vocabulary onto the normalized statuses in
`models` (valid / invalid / risky / catch_all / disposable / unknown), so the
rest of the pipeline never needs to know which vendor is in use.
"""

from __future__ import annotations

from typing import Any

from ...models import (
    VerificationResult,
    V_CATCH_ALL,
    V_DISPOSABLE,
    V_INVALID,
    V_RISKY,
    V_UNKNOWN,
    V_VALID,
)
from ...util import as_float
from ..base import EmailVerifier, ProviderError

# Vendor status string -> normalized status.
STATUS_ALIASES: dict[str, str] = {
    # valid
    "valid": V_VALID, "ok": V_VALID, "deliverable": V_VALID, "safe": V_VALID,
    "safe_to_send": V_VALID, "good": V_VALID, "email_valid": V_VALID,
    "valid_email": V_VALID, "true": V_VALID, "verified": V_VALID,
    # invalid
    "invalid": V_INVALID, "undeliverable": V_INVALID, "bad": V_INVALID,
    "email_invalid": V_INVALID, "invalid_email": V_INVALID, "false": V_INVALID,
    "do_not_mail": V_INVALID, "bounce": V_INVALID, "hard_bounce": V_INVALID,
    "spamtrap": V_INVALID, "spam_trap": V_INVALID, "abuse": V_INVALID,
    "blacklisted": V_INVALID, "role_account_invalid": V_INVALID,
    # risky
    "risky": V_RISKY, "unknown_risky": V_RISKY, "low_quality": V_RISKY,
    "low_deliverability": V_RISKY, "role": V_RISKY, "role_based": V_RISKY,
    "greylisted": V_RISKY, "toxic": V_RISKY, "inbox_full": V_RISKY,
    "mailbox_full": V_RISKY, "full_mailbox": V_RISKY,
    # catch-all
    "catch_all": V_CATCH_ALL, "catchall": V_CATCH_ALL, "catch-all": V_CATCH_ALL,
    "accept_all": V_CATCH_ALL, "acceptall": V_CATCH_ALL, "accept-all": V_CATCH_ALL,
    "unknown_catch_all": V_CATCH_ALL,
    # disposable
    "disposable": V_DISPOSABLE, "temporary": V_DISPOSABLE,
    "disposable_email": V_DISPOSABLE,
    # unknown
    "unknown": V_UNKNOWN, "error": V_UNKNOWN, "timeout": V_UNKNOWN,
    "no_connect": V_UNKNOWN, "unavailable": V_UNKNOWN, "antispam_system": V_UNKNOWN,
    "smtp_error": V_UNKNOWN, "smtp_protocol": V_UNKNOWN, "exception_occurred": V_UNKNOWN,
}


def normalize_status(raw: Any) -> str:
    key = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return STATUS_ALIASES.get(key, V_UNKNOWN)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


class MillionVerifier(EmailVerifier):
    """https://millionverifier.com - single-email endpoint."""

    name = "millionverifier"
    ENDPOINT = "https://api.millionverifier.com/api/v3/"

    def verify(self, email: str) -> VerificationResult:
        key = self.settings.millionverifier_key or self.settings.verify_api_key
        if not key:
            raise ProviderError("MILLIONVERIFIER_KEY is not set")
        data = self.client.get_json(
            self.ENDPOINT, params={"api": key, "email": email, "timeout": 20}
        )
        result = VerificationResult(provider=self.name, raw=data)
        if data.get("error"):
            result.error = str(data["error"])
            return result
        result.status = normalize_status(data.get("result") or data.get("resultcode"))
        result.sub_status = str(data.get("subresult") or "")
        result.free = _truthy(data.get("free"))
        result.is_role = _truthy(data.get("role"))
        result.is_catch_all = result.status == V_CATCH_ALL
        result.mx_found = None if data.get("mx") is None else bool(data.get("mx"))
        result.score = 90.0 if result.status == V_VALID else None
        return result


class ZeroBounce(EmailVerifier):
    """https://www.zerobounce.net/docs/email-validation-api-quickstart/"""

    name = "zerobounce"
    ENDPOINT = "https://api.zerobounce.net/v2/validate"

    def verify(self, email: str) -> VerificationResult:
        key = self.settings.zerobounce_key or self.settings.verify_api_key
        if not key:
            raise ProviderError("ZEROBOUNCE_KEY is not set")
        data = self.client.get_json(
            self.ENDPOINT, params={"api_key": key, "email": email, "ip_address": ""}
        )
        result = VerificationResult(provider=self.name, raw=data)
        if data.get("error"):
            result.error = str(data["error"])
            return result
        status = str(data.get("status", ""))
        sub = str(data.get("sub_status", ""))
        result.status = normalize_status(status)
        if result.status == V_UNKNOWN and sub:
            result.status = normalize_status(sub)
        result.sub_status = sub
        result.free = _truthy(data.get("free_email"))
        result.is_role = sub in {"role_based", "role_based_catch_all"}
        result.is_catch_all = status.lower() == "catch-all" or "catch_all" in sub
        if result.is_catch_all:
            result.status = V_CATCH_ALL
        result.mx_found = _truthy(data.get("mx_found"))
        result.is_disposable = sub == "disposable"
        return result


class NeverBounce(EmailVerifier):
    """https://developers.neverbounce.com/v4.0/"""

    name = "neverbounce"
    ENDPOINT = "https://api.neverbounce.com/v4/single/check"

    def verify(self, email: str) -> VerificationResult:
        key = self.settings.neverbounce_key or self.settings.verify_api_key
        if not key:
            raise ProviderError("NEVERBOUNCE_KEY is not set")
        data = self.client.get_json(
            self.ENDPOINT,
            params={"key": key, "email": email, "address_info": 1, "credits_info": 0},
        )
        result = VerificationResult(provider=self.name, raw=data)
        if str(data.get("status")) == "auth_failure":
            raise ProviderError(f"neverbounce auth failure: {data.get('message')}")
        # NeverBounce returns numeric result codes as well as strings.
        code_map = {0: V_VALID, 1: V_INVALID, 2: V_DISPOSABLE, 3: V_CATCH_ALL, 4: V_UNKNOWN}
        raw_result = data.get("result")
        if isinstance(raw_result, int):
            result.status = code_map.get(raw_result, V_UNKNOWN)
        else:
            result.status = normalize_status(raw_result)
        flags = data.get("flags") or []
        result.is_role = "role_account" in flags
        result.free = "free_email_host" in flags
        result.is_disposable = "disposable_email" in flags or result.status == V_DISPOSABLE
        result.is_catch_all = result.status == V_CATCH_ALL
        result.mx_found = "has_dns_mx" in flags if flags else None
        result.sub_status = ",".join(str(f) for f in flags[:4])
        return result


class Reoon(EmailVerifier):
    """https://emailverifier.reoon.com - power mode does real SMTP checks."""

    name = "reoon"
    ENDPOINT = "https://emailverifier.reoon.com/api/v1/verify"

    def verify(self, email: str) -> VerificationResult:
        key = self.settings.reoon_key or self.settings.verify_api_key
        if not key:
            raise ProviderError("REOON_KEY is not set")
        data = self.client.get_json(
            self.ENDPOINT, params={"key": key, "email": email, "mode": "power"}
        )
        result = VerificationResult(provider=self.name, raw=data)
        result.status = normalize_status(data.get("status"))
        result.is_role = _truthy(data.get("is_role_account"))
        result.free = _truthy(data.get("is_free_email"))
        result.is_disposable = _truthy(data.get("is_disposable"))
        result.is_catch_all = _truthy(data.get("is_catch_all_domain"))
        if result.is_catch_all and result.status not in {V_VALID, V_INVALID}:
            result.status = V_CATCH_ALL
        result.mx_found = _truthy(data.get("has_inbox_full")) or None
        result.score = as_float(data.get("overall_score"))
        return result


class EmailListVerify(EmailVerifier):
    """https://www.emaillistverify.com - plain-text single-email endpoint."""

    name = "emaillistverify"
    ENDPOINT = "https://apps.emaillistverify.com/api/verifyEmail"

    def verify(self, email: str) -> VerificationResult:
        key = self.settings.emaillistverify_key or self.settings.verify_api_key
        if not key:
            raise ProviderError("EMAILLISTVERIFY_KEY is not set")
        response = self.client.request(
            "GET", self.ENDPOINT, params={"secret": key, "email": email}
        )
        body = (response.text or "").strip().lower()
        result = VerificationResult(provider=self.name, raw={"body": body})
        mapping = {
            "ok": V_VALID,
            "email_disabled": V_INVALID,
            "dead_server": V_INVALID,
            "invalid_mx": V_INVALID,
            "invalid_syntax": V_INVALID,
            "disposable": V_DISPOSABLE,
            "spamtrap": V_INVALID,
            "accept_all": V_CATCH_ALL,
            "role": V_RISKY,
            "unknown": V_UNKNOWN,
            "smtp_protocol": V_UNKNOWN,
            "antispam_system": V_UNKNOWN,
            "attempt_rejected": V_UNKNOWN,
            "relay_error": V_UNKNOWN,
            "key_not_valid": V_UNKNOWN,
            "missing_parameters": V_UNKNOWN,
        }
        result.status = mapping.get(body, normalize_status(body))
        result.sub_status = body
        result.is_catch_all = body == "accept_all"
        result.is_disposable = body == "disposable"
        if body in {"key_not_valid", "missing_parameters"}:
            raise ProviderError(f"emaillistverify rejected the request: {body}")
        return result


class Bouncer(EmailVerifier):
    """https://docs.usebouncer.com - single email verification."""

    name = "bouncer"
    ENDPOINT = "https://api.usebouncer.com/v1.1/email/verify"

    def verify(self, email: str) -> VerificationResult:
        key = self.settings.bouncer_key or self.settings.verify_api_key
        if not key:
            raise ProviderError("BOUNCER_KEY is not set")
        data = self.client.get_json(
            self.ENDPOINT, params={"email": email, "timeout": 20},
            headers={"x-api-key": key},
        )
        result = VerificationResult(provider=self.name, raw=data)
        result.status = normalize_status(data.get("status"))
        reason = str(data.get("reason") or "")
        result.sub_status = reason
        if reason in {"accept_all", "accepted_email"}:
            result.is_catch_all = True
            if result.status != V_VALID:
                result.status = V_CATCH_ALL
        account = data.get("account") or {}
        domain_info = data.get("domain") or {}
        result.is_role = _truthy(account.get("role"))
        result.is_disposable = _truthy(domain_info.get("disposable"))
        result.free = _truthy(domain_info.get("free"))
        result.mx_found = bool(domain_info.get("acceptAll") is not None or domain_info.get("mx"))
        return result


VENDOR_CLASSES: dict[str, type[EmailVerifier]] = {
    MillionVerifier.name: MillionVerifier,
    ZeroBounce.name: ZeroBounce,
    NeverBounce.name: NeverBounce,
    Reoon.name: Reoon,
    EmailListVerify.name: EmailListVerify,
    Bouncer.name: Bouncer,
}
