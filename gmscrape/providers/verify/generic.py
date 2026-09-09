"""Config-driven email verification provider.

Point GENERIC_VERIFY_CONFIG at a JSON file describing your verification API:

    {
      "name": "myverifier",
      "method": "GET",
      "url": "https://api.example.com/v1/verify",
      "headers": {"Authorization": "Bearer {api_key}"},
      "query": {"email": "{email}"},
      "status_path": "data.result",
      "score_path": "data.score",
      "sub_status_path": "data.reason",
      "catch_all_path": "data.accept_all",
      "role_path": "data.role",
      "free_path": "data.free",
      "disposable_path": "data.disposable",
      "mx_path": "data.mx_found",
      "status_map": {"deliverable": "valid", "undeliverable": "invalid"},
      "batch": {
        "url": "https://api.example.com/v1/verify/batch",
        "method": "POST",
        "body": {"emails": "{emails}"},
        "results_path": "data",
        "email_path": "email"
      }
    }

Vendor status strings are first run through `status_map`, then through the
built-in alias table, so most APIs need no mapping at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Sequence

from ...models import VerificationResult, V_CATCH_ALL, V_UNKNOWN, V_VALID
from ...util import as_float, dig
from ..base import EmailVerifier, ProviderError
from .vendors import normalize_status


def _substitute(node: Any, values: dict[str, Any]) -> Any:
    if isinstance(node, str):
        # A lone "{emails}" placeholder becomes the actual list, not a string.
        if node.strip() == "{emails}":
            return values.get("emails", [])
        out = node
        for key, value in values.items():
            token = "{" + key + "}"
            if token in out:
                out = out.replace(token, "" if value is None else str(value))
        return out
    if isinstance(node, dict):
        return {k: _substitute(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, values) for v in node]
    return node


def _bool_or_none(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "accept_all"}


class GenericVerifier(EmailVerifier):
    name = "generic"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.config = self._load_config(settings.generic_verify_config)
        self.name = str(self.config.get("name") or "generic")

    @staticmethod
    def _load_config(path: str) -> dict[str, Any]:
        if not path:
            raise ProviderError(
                "GENERIC_VERIFY_CONFIG is not set - point it at a JSON file "
                "describing your verification API "
                "(see examples/verify_api.example.json)"
            )
        p = Path(path).expanduser()
        if not p.exists():
            raise ProviderError(f"generic verify config not found: {p}")
        try:
            config = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ProviderError(f"invalid JSON in {p}: {exc}") from exc
        if not config.get("url"):
            raise ProviderError(f"{p} must define a 'url'")
        return config

    def _api_key(self) -> str:
        return str(
            self.config.get("api_key")
            or self.settings.verify_api_key
            or ""
        )

    def _map_status(self, raw: Any) -> str:
        custom = self.config.get("status_map") or {}
        key = str(raw or "").strip().lower()
        if key in {str(k).lower() for k in custom}:
            for k, v in custom.items():
                if str(k).lower() == key:
                    return str(v).lower()
        return normalize_status(raw)

    def _result_from(self, payload: Any) -> VerificationResult:
        cfg = self.config
        result = VerificationResult(
            provider=self.name,
            raw=payload if isinstance(payload, dict) else {"body": payload},
        )
        status_raw = dig(payload, str(cfg.get("status_path", "status")))
        result.status = self._map_status(status_raw)
        result.sub_status = str(dig(payload, str(cfg.get("sub_status_path", "")), "") or "")
        result.score = as_float(dig(payload, str(cfg.get("score_path", "")), None))
        result.is_catch_all = bool(_bool_or_none(dig(payload, str(cfg.get("catch_all_path", "")))))
        result.is_role = bool(_bool_or_none(dig(payload, str(cfg.get("role_path", "")))))
        result.free = bool(_bool_or_none(dig(payload, str(cfg.get("free_path", "")))))
        result.is_disposable = bool(_bool_or_none(dig(payload, str(cfg.get("disposable_path", "")))))
        result.mx_found = _bool_or_none(dig(payload, str(cfg.get("mx_path", ""))))
        if result.is_catch_all and result.status not in {V_VALID}:
            result.status = V_CATCH_ALL
        if cfg.get("error_path"):
            error = dig(payload, str(cfg["error_path"]))
            if error:
                result.error = str(error)
        return result

    def verify(self, email: str) -> VerificationResult:
        cfg = self.config
        values = {"api_key": self._api_key(), "email": email, "emails": [email]}
        url = _substitute(cfg["url"], values)
        headers = _substitute(cfg.get("headers") or {}, values)
        params = _substitute(cfg.get("query") or {}, values)
        body = _substitute(cfg.get("body"), values) if cfg.get("body") else None
        method = str(cfg.get("method", "GET")).upper()

        kwargs: dict[str, Any] = {"headers": headers, "params": params}
        if body is not None:
            kwargs["json"] = body
        response = self.client.request(method, url, **kwargs)
        if response.status_code >= 400:
            return VerificationResult(
                status=V_UNKNOWN,
                provider=self.name,
                error=f"HTTP {response.status_code}: {response.text[:160]}",
            )
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text.strip()
        if cfg.get("result_path"):
            payload = dig(payload, str(cfg["result_path"]), payload)
        return self._result_from(payload)

    def verify_many(self, emails: Sequence[str]) -> dict[str, VerificationResult]:
        batch = self.config.get("batch")
        if not batch or not batch.get("url"):
            return super().verify_many(emails)
        values = {
            "api_key": self._api_key(),
            "emails": list(emails),
            "email": emails[0] if emails else "",
        }
        url = _substitute(batch["url"], values)
        headers = _substitute(batch.get("headers") or self.config.get("headers") or {}, values)
        params = _substitute(batch.get("query") or {}, values)
        body = _substitute(batch.get("body"), values) if batch.get("body") else None
        method = str(batch.get("method", "POST")).upper()

        kwargs: dict[str, Any] = {"headers": headers, "params": params}
        if body is not None:
            kwargs["json"] = body
        response = self.client.request(method, url, **kwargs)
        if response.status_code >= 400:
            error = f"HTTP {response.status_code}: {response.text[:160]}"
            return {e: self._unknown(error) for e in emails}
        try:
            payload = response.json()
        except ValueError:
            return {e: self._unknown("non-JSON batch response") for e in emails}

        rows = dig(payload, str(batch.get("results_path", "")), payload)
        if isinstance(rows, dict):
            rows = [rows]
        email_key = str(batch.get("email_path", "email"))
        out: dict[str, VerificationResult] = {}
        for row in rows or []:
            address = str(dig(row, email_key, "") or "").strip().lower()
            if address:
                out[address] = self._result_from(row)
        for email in emails:
            out.setdefault(email.lower(), self._unknown("missing from batch response"))
        return out
