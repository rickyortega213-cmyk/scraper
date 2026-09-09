"""MailTester Ninja adapter (https://mailtester.ninja).

Two-step API:

  1. exchange the API key for a short-lived bearer token
       GET https://token.mailtester.ninja/token?key=<API_KEY>   -> {"token": "<jwt>"}
  2. verify an address with that token
       GET https://happy.mailtester.ninja/ninja?email=<addr>&token=<jwt>

     {"email": "...", "user": "...", "domain": "...", "mx": "...",
      "code": "ok", "message": "Accepted", "connections": 1}

The token is cached for the life of the verifier and re-fetched automatically
when it expires (its JWT `exp` is read, with a conservative TTL fallback) or
when the API rejects it mid-run.

Unrecognized `code` values deliberately degrade to `unknown` rather than
`valid`: mislabelling an address as deliverable is the expensive mistake. The
provider's own code and message are always preserved in `sub_status`, so the
first live run shows you exactly what vocabulary the API is using.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import time
from typing import Any, Optional

from ...models import (
    VerificationResult,
    V_CATCH_ALL,
    V_INVALID,
    V_RISKY,
    V_UNKNOWN,
    V_VALID,
)
from ...util import as_float
from ..base import EmailVerifier, ProviderError
from .vendors import normalize_status

log = logging.getLogger(__name__)

TOKEN_URL = "https://token.mailtester.ninja/token"
VERIFY_URL = "https://happy.mailtester.ninja/ninja"

# Refresh a little before the token actually lapses.
TOKEN_SAFETY_MARGIN = 120.0
TOKEN_FALLBACK_TTL = 1800.0

# MailTester Ninja `code` -> normalized status.
CODE_MAP: dict[str, str] = {
    "ok": V_VALID,               # mailbox accepted
    "valid": V_VALID,
    "accepted": V_VALID,
    "ko": V_INVALID,             # mailbox rejected
    "invalid": V_INVALID,
    "rejected": V_INVALID,
    "mb": V_UNKNOWN,             # mailbox busy / greylisted - retry later
    "busy": V_UNKNOWN,
    "greylisted": V_UNKNOWN,
    "ca": V_CATCH_ALL,           # domain accepts anything
    "catch_all": V_CATCH_ALL,
    "catch-all": V_CATCH_ALL,
    "accept_all": V_CATCH_ALL,
    "dis": V_RISKY,              # disposable
    "disposable": V_RISKY,
    "err": V_UNKNOWN,
    "error": V_UNKNOWN,
    "unknown": V_UNKNOWN,
    "timeout": V_UNKNOWN,
}

# Substrings in the human-readable `message` that reveal a catch-all domain.
CATCH_ALL_HINTS = ("catch-all", "catch all", "catchall", "accept-all", "accept all")
DISPOSABLE_HINTS = ("disposable", "temporary")
UNAUTHORIZED_HINTS = ("token", "unauthorized", "expired", "invalid key", "forbidden")


def _jwt_expiry(token: str) -> Optional[float]:
    """Read `exp` out of a JWT payload without verifying the signature."""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    expiry = as_float(claims.get("exp"))
    return expiry if expiry and expiry > time.time() else None


class MailTesterNinja(EmailVerifier):
    name = "mailtester"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()

    # --- token handling ----------------------------------------------------
    def _api_key(self) -> str:
        key = self.settings.mailtester_key or self.settings.verify_api_key
        if not key:
            raise ProviderError("MAILTESTER_KEY is not set")
        return str(key)

    def _fetch_token(self) -> str:
        response = self.client.request("GET", TOKEN_URL, params={"key": self._api_key()})
        if response.status_code >= 400:
            raise ProviderError(
                f"mailtester token request failed (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )
        token = ""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            token = str(payload.get("token") or payload.get("access_token") or "")
            if not token and payload.get("message"):
                raise ProviderError(f"mailtester token request refused: {payload['message']}")
        if not token:
            # Some deployments return the bare token as text/plain.
            candidate = (response.text or "").strip().strip('"')
            if candidate and " " not in candidate:
                token = candidate
        if not token:
            raise ProviderError(
                f"mailtester token response contained no token: {response.text[:200]}"
            )
        expiry = _jwt_expiry(token)
        self._token_expires_at = (
            expiry - TOKEN_SAFETY_MARGIN if expiry else time.time() + TOKEN_FALLBACK_TTL
        )
        log.debug("mailtester token acquired, valid for %.0fs", self._token_expires_at - time.time())
        return token

    def _get_token(self, force: bool = False) -> str:
        with self._token_lock:
            if force or not self._token or time.time() >= self._token_expires_at:
                self._token = self._fetch_token()
            return self._token

    # --- verification ------------------------------------------------------
    def verify(self, email: str) -> VerificationResult:
        email = (email or "").strip().lower()
        payload, retried = self._request(email, self._get_token())
        if payload is None and not retried:
            # Token may have lapsed mid-run; get a fresh one and try once more.
            payload, _ = self._request(email, self._get_token(force=True))
        if payload is None:
            return self._unknown("mailtester did not return a usable response")
        return self._to_result(email, payload)

    def _request(self, email: str, token: str) -> tuple[Optional[dict[str, Any]], bool]:
        """Returns (payload, token_was_rejected)."""
        response = self.client.request(
            "GET", VERIFY_URL, params={"email": email, "token": token}
        )
        if response.status_code in (401, 403):
            return None, False
        if response.status_code >= 400:
            log.warning(
                "mailtester HTTP %s for %s: %s", response.status_code, email,
                response.text[:160],
            )
            return None, True
        try:
            payload = response.json()
        except ValueError:
            log.warning("mailtester returned non-JSON for %s: %s", email, response.text[:160])
            return None, True
        if not isinstance(payload, dict):
            return None, True
        message = str(payload.get("message") or "").lower()
        if not payload.get("code") and any(hint in message for hint in UNAUTHORIZED_HINTS):
            return None, False
        return payload, True

    def _to_result(self, email: str, payload: dict[str, Any]) -> VerificationResult:
        code = str(payload.get("code") or "").strip().lower()
        message = str(payload.get("message") or "")
        lowered = message.lower()

        status = CODE_MAP.get(code)
        if status is None:
            status = normalize_status(code) if code else normalize_status(message)
            if status == V_UNKNOWN and code:
                log.warning(
                    "mailtester returned unmapped code %r (message %r) - treating as unknown",
                    code, message,
                )

        result = VerificationResult(
            status=status,
            provider=self.name,
            sub_status=f"{code}:{message}".strip(":") if (code or message) else "",
            raw=payload,
        )
        result.mx_found = bool(payload.get("mx")) or None
        if any(hint in lowered for hint in CATCH_ALL_HINTS):
            result.is_catch_all = True
            if result.status != V_INVALID:
                result.status = V_CATCH_ALL
        if any(hint in lowered for hint in DISPOSABLE_HINTS):
            result.is_disposable = True
        # The API reports acceptance, not confidence, so keep the score coarse.
        if result.status == V_VALID:
            result.score = 90.0
        elif result.status == V_CATCH_ALL:
            result.score = 50.0
        return result

    def is_catch_all(self, domain: str) -> Optional[bool]:
        """Probe a domain with an address nobody would ever own."""
        if not domain:
            return None
        probe = f"gmscrape-probe-{int(time.time())}@{domain}"
        result = self.verify(probe)
        if result.status in (V_VALID, V_CATCH_ALL) or result.is_catch_all:
            return True
        if result.status == V_INVALID:
            return False
        return None
