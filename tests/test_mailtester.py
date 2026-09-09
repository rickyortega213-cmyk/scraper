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
    ) -> None:
        self.responses = responses
        self.default = default or {"code": "err"}
        self.token = token or _jwt()
        self.token_calls = 0
        self.verify_calls: list[str] = []
        self.reject_tokens: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "token.mailtester.ninja":
            self.token_calls += 1
            return httpx.Response(200, json={"token": self.token})
        email = request.url.params.get("email", "")
        supplied = request.url.params.get("token", "")
        if supplied in self.reject_tokens:
            return httpx.Response(401, json={"message": "token expired"})
        self.verify_calls.append(email)
        return httpx.Response(200, json=self.responses.get(email, self.default))


def make_verifier(api: Api) -> MailTesterNinja:
    settings = Settings.from_env(mailtester_key="sub_test_key")
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
