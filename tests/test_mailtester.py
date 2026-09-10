"""MailTester Ninja adapter, driven through a mocked HTTP transport."""

from __future__ import annotations

import base64
import itertools
import json
import time

import httpx
import pytest

from gmscrape.config import Settings
from gmscrape.models import V_CATCH_ALL, V_INVALID, V_UNKNOWN, V_VALID
from gmscrape.providers.base import ProviderError
from gmscrape.providers.verify.mailtester import MailTesterNinja


_nonce = itertools.count(1)


def _jwt(ttl: int = 3600) -> str:
    """A unique, unexpired JWT-shaped token (the nonce keeps tokens distinct)."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + ttl, "jti": next(_nonce)}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


class Api:
    """Scriptable stand-in for the MailTester Ninja endpoints."""

    def __init__(
        self,
        responses: dict[str, dict],
        token: str | None = None,
        default: dict | None = None,
        direct_key: str = "",            # a key the direct endpoint accepts ("" = token flow only)
    ) -> None:
        self.responses = responses
        self.default = default or {"code": "err"}
        self.token = token or _jwt()
        self.token_calls = 0
        self.verify_calls: list[str] = []
        self.reject_tokens: set[str] = set()
        self.direct_key = direct_key
        self.key_params: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "token.mailtester.ninja":
            self.token_calls += 1
            return httpx.Response(200, json={"token": self.token})
        email = request.url.params.get("email", "")
        if "key" in request.url.params:
            self.key_params.append(request.url.params["key"])
            if not self.direct_key or request.url.params["key"] != self.direct_key:
                return httpx.Response(401, text="https://mailtester.ninja/subscribe")
        else:
            supplied = request.url.params.get("token", "")
            if supplied in self.reject_tokens:
                return httpx.Response(401, json={"message": "token expired"})
        self.verify_calls.append(email)
        return httpx.Response(200, json=self.responses.get(email, self.default))


def make_verifier(api: Api, key: str = "sub_test_key") -> MailTesterNinja:
    settings = Settings.from_env(mailtester_key=key, mailtester_rate=0)
    verifier = MailTesterNinja(settings)
    verifier.client._client = httpx.Client(transport=httpx.MockTransport(api.handler))
    return verifier


def test_maps_accepted_and_rejected_mailboxes():
    api = Api({
        "good@acme.com": {"code": "ok", "message": "Accepted", "mx": "mx.acme.com"},
        "bad@acme.com": {"code": "ko", "message": "Rejected", "mx": "mx.acme.com"},
    })
    verifier = make_verifier(api)

    good = verifier.verify("good@acme.com")
    assert good.status == V_VALID and good.provider == "mailtester"
    assert good.mx_found is True and good.score == 90.0
    assert good.sub_status == "ok:Accepted"

    assert verifier.verify("bad@acme.com").status == V_INVALID


def test_greylisted_mailbox_is_unknown_not_invalid():
    api = Api({"busy@acme.com": {"code": "mb", "message": "Mailbox busy"}})
    assert make_verifier(api).verify("busy@acme.com").status == V_UNKNOWN


def test_detects_catch_all_from_code_or_message():
    api = Api({
        "a@acme.com": {"code": "ca", "message": "Catch-all domain"},
        "b@acme.com": {"code": "ok", "message": "Accepted (catch all)"},
    })
    verifier = make_verifier(api)

    first = verifier.verify("a@acme.com")
    assert first.status == V_CATCH_ALL and first.is_catch_all

    # An "ok" that the message reveals to be catch-all must not stay `valid`.
    second = verifier.verify("b@acme.com")
    assert second.is_catch_all and second.status == V_CATCH_ALL


def test_unmapped_code_degrades_to_unknown():
    api = Api({"x@acme.com": {"code": "wat", "message": "Something new"}})
    result = make_verifier(api).verify("x@acme.com")
    assert result.status == V_UNKNOWN
    assert result.sub_status == "wat:Something new"   # raw vocabulary preserved
    assert result.raw["code"] == "wat"


def test_token_is_fetched_once_and_reused():
    api = Api({e: {"code": "ok", "message": "Accepted"} for e in ("a@x.com", "b@x.com")})
    verifier = make_verifier(api)
    verifier.verify("a@x.com")
    verifier.verify("b@x.com")
    assert api.token_calls == 1
    assert api.verify_calls == ["a@x.com", "b@x.com"]


def test_expired_token_is_refreshed_and_the_call_retried():
    api = Api({"a@x.com": {"code": "ok", "message": "Accepted"}})
    verifier = make_verifier(api)
    stale = verifier._get_token()
    api.reject_tokens.add(stale)
    api.token = _jwt()                     # the next token request returns a fresh one

    result = verifier.verify("a@x.com")
    assert result.status == V_VALID
    assert api.token_calls == 2


def test_missing_key_is_a_clear_error():
    verifier = MailTesterNinja(Settings.from_env(mailtester_key="", verify_api_key=""))
    with pytest.raises(ProviderError, match="MAILTESTER_KEY"):
        verifier.verify("a@x.com")


def test_catch_all_probe_reads_the_domain():
    """An address nobody could own tells you whether the domain accepts all."""
    accepting = make_verifier(Api({}, default={"code": "ok", "message": "Accepted"}))
    assert accepting.is_catch_all("acme.com") is True

    strict = make_verifier(Api({}, default={"code": "ko", "message": "Rejected"}))
    assert strict.is_catch_all("acme.com") is False

    flaky = make_verifier(Api({}, default={"code": "mb", "message": "Mailbox busy"}))
    assert flaky.is_catch_all("acme.com") is None
    assert flaky.is_catch_all("") is None


def test_a_refused_key_fails_fast_and_says_what_to_do():
    """401 from both doors (body: a subscribe link) means the key is no good.
    One clear error, no further network calls."""
    from gmscrape.providers.base import ProviderAuthError

    class Refusing(Api):
        def handler(self, request):
            if request.url.host == "token.mailtester.ninja":
                self.token_calls += 1
                return httpx.Response(401, text="https://mailtester.ninja/subscribe")
            return super().handler(request)

    api = Refusing({})
    verifier = make_verifier(api)
    with pytest.raises(ProviderAuthError) as err:
        verifier.preflight()
    text = str(err.value)
    assert "https://mailtester.ninja/subscribe" in text and "curly braces" in text
    assert "scraper keys set MAILTESTER_KEY" in text
    for email in ("a@x.com", "b@x.com", "c@x.com"):
        with pytest.raises(ProviderAuthError):
            verifier.verify(email)
    assert api.token_calls == 1 and api.verify_calls == []     # never hammered again
    assert api.key_params == ["sub_test_key", "{sub_test_key}"]  # both spellings were tried


def test_direct_key_auth_is_used_when_the_service_accepts_it():
    """The documented call: key= on every request, no token endpoint at all."""
    api = Api({"a@x.com": {"code": "ok", "message": "Accepted"}}, direct_key="sub_test_key")
    verifier = make_verifier(api)
    verifier.preflight()
    assert verifier.auth_mode == "direct"
    assert verifier.verify("a@x.com").status == V_VALID
    assert api.token_calls == 0
    assert api.verify_calls == ["probe@example.com", "a@x.com"]   # one probe, then real work


def test_key_pasted_with_braces_still_works():
    api = Api({"a@x.com": {"code": "ok", "message": "Accepted"}}, direct_key="sub_test_key")
    verifier = make_verifier(api, key="{sub_test_key}")
    assert verifier.verify("a@x.com").status == V_VALID
    assert verifier.auth_mode == "direct" and api.token_calls == 0


def test_token_flow_is_the_fallback_when_direct_is_refused():
    api = Api({"a@x.com": {"code": "ok", "message": "Accepted"}})     # no direct key accepted
    verifier = make_verifier(api)
    assert verifier.verify("a@x.com").status == V_VALID
    assert verifier.auth_mode == "token" and api.token_calls == 1


def test_rate_limited_answers_are_retried_not_recorded(monkeypatch):
    """'Limited' or HTTP 429 means slow down - never a verdict on the address."""
    from gmscrape.providers.verify import mailtester as M

    naps: list[float] = []
    monkeypatch.setattr(M.time, "sleep", lambda s: naps.append(s))
    answers = iter([
        httpx.Response(429, text="slow down"),
        httpx.Response(200, json={"code": "mb", "message": "Limited"}),
        httpx.Response(200, json={"code": "ok", "message": "Accepted"}),
    ])

    class Throttling(Api):
        def handler(self, request):
            if "key" in request.url.params and request.url.params.get("email") == "probe@example.com":
                return super().handler(request)
            return next(answers)

    api = Throttling({}, direct_key="sub_test_key")
    result = make_verifier(api).verify("a@x.com")
    # the HTTP client retries the 429 itself (its own backoff), then "Limited"
    # is paused on by the adapter; neither answer became a verdict
    assert result.status == V_VALID and len(naps) == 2 and naps[-1] == 2.0


def test_rate_limiter_meters_calls_per_window():
    from gmscrape.providers.verify.mailtester import RateLimiter

    limiter = RateLimiter(3, window=0.4)
    started = time.monotonic()
    for _ in range(4):
        limiter.wait()
    assert time.monotonic() - started >= 0.3          # the fourth call waited for the window
    assert RateLimiter(0).limit == 0                    # 0 = unmetered
