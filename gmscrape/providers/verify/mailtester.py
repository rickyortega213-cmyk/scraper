"""MailTester Ninja adapter (https://mailtester.ninja).

Two ways in, both supported; `auto` (the default) finds the one your key
accepts and remembers it for the run:

  direct  GET https://happy.mailtester.ninja/ninja?email=<addr>&key=<API_KEY>
          (what their current docs and every public example use)
  token   GET https://token.mailtester.ninja/token?key=<API_KEY> -> {"token": "<jwt>"}
          GET https://happy.mailtester.ninja/ninja?email=<addr>&token=<jwt>
          (the older two-step flow; the token is cached until its `exp`)

Either answers:

     {"email": "...", "user": "...", "domain": "...", "mx": "...",
      "code": "ok", "message": "Accepted", "connections": 1}

`code` is ok / ko / mb; `message` is Accepted, Limited, Rejected, Catch-All,
No Mx, Mx Error, Timeout or SPAM Block. "Limited" and HTTP 429 mean the plan's
rate limit was hit: the call is retried after a pause, never recorded as a
verdict. Plans allow 5 / 11 / 57 requests per 10 s (Starter / Pro / Ultimate)
and the vendor warns that exceeding them can get an account banned, so calls
are metered by MAILTESTER_RATE (default: the Starter limit).

A key the service refuses raises ProviderAuthError once and is not retried.
Unrecognized `code` values deliberately degrade to `unknown` rather than
`valid`: mislabelling an address as deliverable is the expensive mistake.
"""

from __future__ import annotations

import base64
import binascii
import collections
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
from ..base import RETRY_STATUS, ApiClient, EmailVerifier, ProviderAuthError, ProviderError
from .vendors import normalize_status

log = logging.getLogger(__name__)

TOKEN_URL = "https://token.mailtester.ninja/token"
VERIFY_URL = "https://happy.mailtester.ninja/ninja"
PROBE_EMAIL = "probe@example.com"          # used once to learn which auth the key accepts

# Refresh a little before the token actually lapses.
TOKEN_SAFETY_MARGIN = 120.0
TOKEN_FALLBACK_TTL = 1800.0

RATE_WINDOW = 10.0                         # the vendor's limits are "per 10 seconds"
LIMITED_RETRIES = 4                        # "Limited" / HTTP 429: pause and try again
LIMITED_BACKOFF = (2.0, 5.0, 10.0, 20.0)

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
UNAUTHORIZED_HINTS = ("token", "unauthorized", "expired", "invalid key", "forbidden", "subscribe")
LIMITED_HINTS = ("limited", "rate limit", "too many")


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


class RateLimiter:
    """`limit` calls per `window` seconds, spread evenly rather than in bursts.

    MailTester's limiter is a steady drip (Ultimate: one call every ~175 ms),
    so 57 calls in the first second of a 10 s window still trip it. Calls are
    scheduled `window / limit` apart across every thread. A 429 stretches the
    gap (penalize); a long run of clean answers relaxes it back (reward)."""

    MAX_STRETCH = 8.0
    RECOVER_AFTER = 100          # clean answers before the gap shrinks a step

    def __init__(self, limit: int, window: float = RATE_WINDOW) -> None:
        self.limit = max(0, int(limit))
        self.window = window
        self.base_interval = (window / self.limit) if self.limit else 0.0
        self.interval = self.base_interval
        self._next_slot = 0.0
        self._hold_until = 0.0
        self._clean = 0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.limit <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot, self._hold_until)
            self._next_slot = slot + self.interval
        pause = slot - time.monotonic()
        if pause > 0:
            time.sleep(pause)

    def penalize(self, hold: float) -> None:
        """Called on a 429: everyone pauses `hold` seconds and the gap grows."""
        with self._lock:
            self._hold_until = max(self._hold_until, time.monotonic() + hold)
            self.interval = min(self.base_interval * self.MAX_STRETCH, self.interval * 1.5)
            self._clean = 0

    def reward(self) -> None:
        with self._lock:
            self._clean += 1
            if self._clean >= self.RECOVER_AFTER and self.interval > self.base_interval:
                self.interval = max(self.base_interval, self.interval / 1.25)
                self._clean = 0


class _KeySlot:
    """One API key with its own auth state and its own rate limit."""

    def __init__(self, key: str, mode: str, rate: int) -> None:
        self.key = key
        self.mode = mode                    # auto | direct | token
        self.key_param = ""                 # the spelling the direct endpoint accepted
        self.token = ""
        self.token_expires_at = 0.0
        self.auth_error = ""
        self.limiter = RateLimiter(rate)
        self.lock = threading.Lock()
        self.calls = 0

    @property
    def ready(self) -> bool:
        return not self.auth_error and self.mode in ("direct", "token") and bool(self.key_param or self.token)

    @property
    def next_free(self) -> float:
        lim = self.limiter
        return max(lim._next_slot, lim._hold_until)


class MailTesterNinja(EmailVerifier):
    name = "mailtester"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        mode = (getattr(settings, "mailtester_auth", "") or "auto").lower()
        rate = int(getattr(settings, "mailtester_rate", 0) or 0)
        raw = str(settings.mailtester_key or settings.verify_api_key or "")
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        # Several keys (MAILTESTER_KEY=key1,key2) each get their own plan-rate
        # limiter; every call goes to whichever key is free soonest.
        self._slots: list[_KeySlot] = [_KeySlot(k, mode, rate) for k in keys]
        self._pick_lock = threading.Lock()
        # 429 is handled here (a global pause + a longer gap), not by blind per-call retries.
        self.client.close()
        self.client = ApiClient(timeout=45.0, retries=2, retry_statuses=RETRY_STATUS - {429})

    @property
    def auth_mode(self) -> str:
        """'direct' or 'token' once known, else 'auto' (first working key)."""
        for slot in self._slots:
            if slot.ready:
                return slot.mode
        return self._slots[0].mode if self._slots else "auto"

    @property
    def key_count(self) -> int:
        return len(self._slots)

    @property
    def current_interval(self) -> float:
        """Seconds between checks on the busiest key right now (0 = unmetered)."""
        live = [s.limiter.interval for s in self._slots if not s.auth_error]
        return max(live) if live else 0.0

    @property
    def working_keys(self) -> int:
        return sum(1 for s in self._slots if not s.auth_error)

    # --- token handling ----------------------------------------------------
    def _api_key(self) -> str:
        if not self._slots:
            raise ProviderError("MAILTESTER_KEY is not set")
        return self._slots[0].key

    @staticmethod
    def _auth_message(refusals: list[str]) -> str:
        tried = "; ".join(refusals) if refusals else "no answer"
        return (
            f"MailTester Ninja rejected the API key ({tried}). "
            "Log in at https://mailtester.ninja, open the API / key page, copy the key exactly "
            "as shown (it may be displayed inside curly braces), check the subscription is "
            "active, then:  scraper keys set MAILTESTER_KEY=<paste>"
        )

    def preflight(self) -> None:
        """Every key must answer; a refused one is dropped with a warning as
        long as another works, and the last one refused is a hard error."""
        if not self._slots:
            raise ProviderError("MAILTESTER_KEY is not set")
        errors: list[str] = []
        for slot in self._slots:
            try:
                self._resolve_auth(slot)
            except ProviderAuthError as exc:
                errors.append(str(exc))
        if errors and len(errors) == len(self._slots):
            raise ProviderAuthError(errors[0])
        for slot in self._slots:
            if slot.auth_error:
                log.warning("mailtester: key %s… refused, continuing with %d working key(s)",
                            slot.key[:6], self.working_keys)

    def _pick(self) -> _KeySlot:
        """The working key whose rate limiter frees up soonest."""
        with self._pick_lock:
            live = [s for s in self._slots if not s.auth_error]
            if not live:
                raise ProviderAuthError(self._slots[0].auth_error if self._slots
                                        else "MAILTESTER_KEY is not set")
            chosen = min(live, key=lambda s: (s.next_free, s.calls))
            chosen.calls += 1
            return chosen

    # --- which door does this key open? --------------------------------------
    @staticmethod
    def _key_variants(key: str) -> list[str]:
        key = key.strip()
        bare = key.strip("{}").strip()
        variants = [key]
        if bare != key:
            variants.append(bare)             # pasted with the braces the web form wants
        else:
            variants.append("{" + key + "}")  # or without them, if the API wants them
        return variants

    def _resolve_auth(self, slot: _KeySlot) -> None:
        """Settle on direct-key or token auth for one key (once per run).
        Raises ProviderAuthError when the service refuses it both ways."""
        with slot.lock:
            if slot.auth_error:
                raise ProviderAuthError(slot.auth_error)
            if slot.ready:
                return
            refusals: list[str] = []
            if slot.mode in ("auto", "direct"):
                for variant in self._key_variants(slot.key):
                    verdict, detail = self._try_direct(slot, variant)
                    if verdict == "ok":
                        slot.key_param = variant
                        slot.mode = "direct"
                        log.debug("mailtester: direct key auth accepted")
                        return
                    refusals.append(f"direct key: {detail}")
                    if verdict == "error":
                        raise ProviderError(detail)
            if slot.mode in ("auto", "token"):
                try:
                    slot.token = self._fetch_token(slot)
                    slot.mode = "token"
                    log.debug("mailtester: token auth accepted")
                    return
                except ProviderAuthError as exc:
                    refusals.append(f"token: {exc}")
            slot.auth_error = self._auth_message(refusals)
            raise ProviderAuthError(slot.auth_error)

    def _try_direct(self, slot: _KeySlot, key: str) -> tuple[str, str]:
        """Probe the direct endpoint with one throwaway address.
        Returns ('ok' | 'refused' | 'error', detail)."""
        slot.limiter.wait()
        response = self.client.request("GET", VERIFY_URL, params={"email": PROBE_EMAIL, "key": key})
        if response.status_code in (401, 403):
            return "refused", f"HTTP {response.status_code} {response.text[:120].strip()}"
        if response.status_code >= 400:
            return "error", f"mailtester HTTP {response.status_code}: {response.text[:160]}"
        try:
            payload = response.json()
        except ValueError:
            return "refused", f"non-JSON answer: {response.text[:120].strip()}"
        if not isinstance(payload, dict):
            return "refused", f"unexpected answer: {str(payload)[:120]}"
        message = str(payload.get("message") or "").lower()
        if payload.get("code") or payload.get("email"):
            if not payload.get("code") and any(h in message for h in UNAUTHORIZED_HINTS):
                return "refused", message
            return "ok", ""
        return "refused", message or str(payload)[:120]

    def _fetch_token(self, slot: _KeySlot) -> str:
        response = self.client.request("GET", TOKEN_URL, params={"key": slot.key})
        if response.status_code in (401, 403):
            raise ProviderAuthError(f"HTTP {response.status_code} {response.text[:120].strip()}")
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
        slot.token_expires_at = (
            expiry - TOKEN_SAFETY_MARGIN if expiry else time.time() + TOKEN_FALLBACK_TTL
        )
        log.debug("mailtester token acquired, valid for %.0fs", slot.token_expires_at - time.time())
        return token

    def _get_token(self, slot: Optional[_KeySlot] = None, force: bool = False) -> str:
        slot = slot or self._pick()
        with slot.lock:
            if slot.auth_error:
                raise ProviderAuthError(slot.auth_error)
            if force or not slot.token or time.time() >= slot.token_expires_at:
                try:
                    slot.token = self._fetch_token(slot)
                except ProviderAuthError as exc:
                    slot.auth_error = self._auth_message([f"token: {exc}"])
                    raise ProviderAuthError(slot.auth_error) from exc
            return slot.token

    # --- verification ------------------------------------------------------
    def verify(self, email: str) -> VerificationResult:
        email = (email or "").strip().lower()
        if not self._slots:
            raise ProviderError("MAILTESTER_KEY is not set")
        slot = self._pick()
        self._resolve_auth(slot)
        outcome, payload = "error", None
        for attempt in range(LIMITED_RETRIES + 1):
            payload, outcome = self._request(slot, email)
            if outcome == "limited" and attempt < LIMITED_RETRIES:
                pause = LIMITED_BACKOFF[min(attempt, len(LIMITED_BACKOFF) - 1)]
                slot.limiter.penalize(pause)          # every thread on this key slows down
                log.info("mailtester rate-limited; pausing %.0fs and spacing calls further apart", pause)
                time.sleep(pause)
                slot = self._pick()                   # another key may be free meanwhile
                self._resolve_auth(slot)
                continue
            if outcome == "ok":
                slot.limiter.reward()
            break
        if outcome == "limited":
            return self._unknown("mailtester_rate_limited")
        if payload is None:
            return self._unknown("mailtester did not return a usable response")
        return self._to_result(email, payload)

    def _auth_params(self, slot: _KeySlot, force: bool = False) -> dict[str, str]:
        if slot.mode == "direct":
            return {"key": slot.key_param}
        return {"token": self._get_token(slot, force=force)}

    def _request(self, slot: _KeySlot, email: str) -> tuple[Optional[dict[str, Any]], str]:
        """One metered call on one key. Returns (payload, outcome) where
        outcome is 'ok', 'limited', 'refused' or 'error'."""
        params = self._auth_params(slot)
        for retry_auth in (False, True):
            slot.limiter.wait()
            response = self.client.request("GET", VERIFY_URL, params={"email": email, **params})
            if response.status_code == 429:
                return None, "limited"
            if response.status_code in (401, 403):
                if slot.mode == "token" and not retry_auth:
                    # The token may have lapsed mid-run: one fresh token, one more try.
                    params = self._auth_params(slot, force=True)
                    continue
                slot.auth_error = self._auth_message(
                    [f"{slot.mode}: HTTP {response.status_code} {response.text[:120].strip()}"])
                raise ProviderAuthError(slot.auth_error)
            if response.status_code >= 400:
                log.warning("mailtester HTTP %s for %s: %s", response.status_code, email,
                            response.text[:160])
                return None, "error"
            try:
                payload = response.json()
            except ValueError:
                log.warning("mailtester returned non-JSON for %s: %s", email, response.text[:160])
                return None, "error"
            if not isinstance(payload, dict):
                return None, "error"
            message = str(payload.get("message") or "").lower()
            if not payload.get("code") and any(hint in message for hint in UNAUTHORIZED_HINTS):
                if slot.mode == "token" and not retry_auth:
                    params = self._auth_params(slot, force=True)
                    continue
                slot.auth_error = self._auth_message([f"{slot.mode}: {message}"])
                raise ProviderAuthError(slot.auth_error)
            if any(hint in message for hint in LIMITED_HINTS) and payload.get("code") != "ok":
                return None, "limited"
            return payload, "ok"
        return None, "error"

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
