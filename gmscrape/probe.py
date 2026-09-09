"""Discover an unknown maps API's request shape and response schema.

Point this at an endpoint with your key and it works out how the API wants to
be called, then prints a ready-to-use GENERIC_MAPS_CONFIG file. Useful when
the docs are behind a login, or you just want the wiring done for you.

It is deliberately frugal, because a successful call usually costs a credit:

  phase 1  find the auth style, using the most likely query parameter
           (a 401/403 means auth is wrong; a 400/422 means auth worked and
           only the parameters are off, which is already the answer)
  phase 2  with auth settled, find the search parameter name

Typically that is 1-2 requests, and never more than --max-requests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit

import httpx

from .providers.base import PLACE_FIELD_PATHS, place_from_mapping
from .util import dig

log = logging.getLogger(__name__)

# Ordered by how common they are in scraper APIs.
AUTH_STYLES: tuple[tuple[str, str], ...] = (
    ("query:apikey", "apikey"),
    ("query:api_key", "api_key"),
    ("query:key", "key"),
    ("query:token", "token"),
    ("query:access_token", "access_token"),
    ("header:X-API-Key", "X-API-Key"),
    ("header:Authorization-Bearer", "Authorization"),
    ("header:apikey", "apikey"),
)

QUERY_PARAMS: tuple[str, ...] = ("query", "q", "search", "keyword", "term", "text", "search_query")

# Tried when only a host (or a bare base path) is given.
ENDPOINT_PATHS: tuple[str, ...] = (
    "", "/maps", "/maps/search", "/google-maps", "/googlemaps",
    "/search/maps", "/api/maps", "/v1/maps", "/places", "/local",
)

AUTH_FAILURE_STATUS = {401, 403, 407}
# "Your parameters are wrong" - which means the endpoint and key were fine.
PARAM_FAILURE_STATUS = {400, 422}
# "There is nothing here" - keep looking for the right path.
NOT_FOUND_STATUS = {404, 405, 410, 501}

NAME_KEYS = ("name", "title", "business_name", "businessname", "company", "place_name")


@dataclass
class Attempt:
    """One probe request and what came back."""

    url: str
    auth_style: str
    query_param: str
    status: int = 0
    error: str = ""
    rows: int = 0
    results_path: str = ""
    sample: dict[str, Any] = field(default_factory=dict)
    body_snippet: str = ""

    @property
    def found_listings(self) -> bool:
        return self.rows > 0

    @property
    def auth_accepted(self) -> bool:
        """Auth is fine if the server got far enough to complain about params."""
        return self.found_listings or self.status in PARAM_FAILURE_STATUS or (
            200 <= self.status < 300
        )


def _apply_auth(style: str, name: str, api_key: str) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (params, headers) carrying the key for one auth style."""
    if style.startswith("query:"):
        return {name: api_key}, {}
    if style == "header:Authorization-Bearer":
        return {}, {"Authorization": f"Bearer {api_key}"}
    return {}, {name: api_key}


def find_result_rows(payload: Any) -> tuple[str, list[dict[str, Any]]]:
    """Locate the listings array in an arbitrary JSON envelope.

    Returns a dotted path (empty for a top-level array) and the rows. Prefers
    the longest list of objects that look like business records.
    """
    best_path = ""
    best_rows: list[dict[str, Any]] = []
    best_score = -1

    def walk(node: Any, path: str, depth: int) -> Iterator[tuple[str, list[dict[str, Any]]]]:
        if depth > 5:
            return
        if isinstance(node, list):
            dicts = [item for item in node if isinstance(item, dict)]
            if dicts:
                yield path, dicts
            for index, item in enumerate(node[:3]):
                yield from walk(item, f"{path}.{index}" if path else str(index), depth + 1)
        elif isinstance(node, dict):
            for key, value in node.items():
                yield from walk(value, f"{path}.{key}" if path else str(key), depth + 1)

    for path, rows in walk(payload, "", 0):
        first = rows[0]
        has_name = any(
            k.lower().replace("_", "") in {n.replace("_", "") for n in NAME_KEYS} for k in first
        )
        score = len(rows) + (100 if has_name else 0) + (20 if len(first) >= 4 else 0)
        if score > best_score:
            best_score, best_path, best_rows = score, path, rows
    return best_path, best_rows


def _record(
    attempt: Attempt, response: Optional[httpx.Response], error: str = ""
) -> Attempt:
    if error:
        attempt.error = error
        return attempt
    assert response is not None
    attempt.status = response.status_code
    attempt.body_snippet = (response.text or "")[:400].replace("\n", " ")
    try:
        payload = response.json()
    except ValueError:
        attempt.error = "response was not JSON"
        return attempt
    path, rows = find_result_rows(payload)
    attempt.results_path = path
    attempt.rows = len(rows)
    if rows:
        attempt.sample = rows[0]
    return attempt


def probe_maps_api(
    endpoint: str,
    api_key: str,
    query: str,
    *,
    timeout: float = 30.0,
    max_requests: int = 12,
    extra_params: Optional[dict[str, str]] = None,
) -> tuple[Optional[Attempt], list[Attempt]]:
    """Find a working request shape. Returns (winner or None, all attempts)."""
    attempts: list[Attempt] = []
    urls = _candidate_urls(endpoint)
    budget = max(1, max_requests)

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:

        def send(url: str, style: str, auth_name: str, query_param: str) -> Attempt:
            nonlocal budget
            params, headers = _apply_auth(style, auth_name, api_key)
            params[query_param] = query
            params.update(extra_params or {})
            attempt = Attempt(url=url, auth_style=style, query_param=query_param)
            try:
                response = client.get(url, params=params, headers=headers)
            except Exception as exc:
                attempt = _record(attempt, None, f"{type(exc).__name__}: {exc}")
            else:
                attempt = _record(attempt, response)
            attempts.append(attempt)
            budget -= 1
            return attempt

        first_style, first_name = AUTH_STYLES[0]
        first_param = QUERY_PARAMS[0]

        # --- phase 0: which path actually exists -------------------------
        chosen = urls[0]
        probe = send(chosen, first_style, first_name, first_param)
        if probe.found_listings:
            return probe, attempts
        if _is_missing(probe) and len(urls) > 1:
            for url in urls[1:]:
                if budget <= 0:
                    return None, attempts
                probe = send(url, first_style, first_name, first_param)
                if probe.found_listings:
                    return probe, attempts
                if not _is_missing(probe):
                    chosen = url
                    break
            else:
                return None, attempts     # every candidate path was a dead end

        # --- phase 1: how the key must be passed -------------------------
        auth_ok: Optional[Attempt] = probe if probe.auth_accepted else None
        if auth_ok is None:
            for style, auth_name in AUTH_STYLES[1:]:
                if budget <= 0:
                    return None, attempts
                attempt = send(chosen, style, auth_name, first_param)
                if attempt.found_listings:
                    return attempt, attempts
                if attempt.auth_accepted:
                    auth_ok = attempt
                    break
        if auth_ok is None:
            return None, attempts

        # --- phase 2: what the search parameter is called ----------------
        auth_name = dict(AUTH_STYLES)[auth_ok.auth_style]
        for query_param in QUERY_PARAMS:
            if query_param == auth_ok.query_param:
                continue
            if budget <= 0:
                break
            attempt = send(auth_ok.url, auth_ok.auth_style, auth_name, query_param)
            if attempt.found_listings:
                return attempt, attempts

    return None, attempts


def _is_missing(attempt: Attempt) -> bool:
    """Whether this attempt says "no endpoint here"."""
    return bool(attempt.error) or attempt.status in NOT_FOUND_STATUS


def _candidate_urls(endpoint: str) -> list[str]:
    """Expand a bare host into the usual endpoint paths."""
    endpoint = endpoint.strip()
    if "://" not in endpoint:
        endpoint = "https://" + endpoint
    parts = urlsplit(endpoint)
    path = parts.path.rstrip("/")
    base = f"{parts.scheme}://{parts.netloc}"
    if path and path not in ("/api", "/v1"):
        return [f"{base}{path}"]
    return [f"{base}{path}{suffix}" for suffix in ENDPOINT_PATHS]


def describe_mapping(sample: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Which normalized Place fields the built-in mapper resolves from a row.

    Returns (resolved field -> value preview, unresolved field names).
    """
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    place = place_from_mapping(sample, query="probe", source="probe")
    for field_name, paths in PLACE_FIELD_PATHS.items():
        value = None
        for path in paths:
            value = dig(sample, path)
            if value not in (None, "", [], {}):
                break
        if value in (None, "", [], {}):
            unresolved.append(field_name)
        else:
            preview = str(value)
            resolved[field_name] = preview[:48] + ("…" if len(preview) > 48 else "")
    if place is None:
        unresolved.append("name (required - no name/title field found)")
    return resolved, unresolved


def build_generic_config(
    attempt: Attempt, *, name: str = "my-maps-api", page_param: str = "page"
) -> dict[str, Any]:
    """Emit a GENERIC_MAPS_CONFIG document for a successful probe."""
    auth_name = dict(AUTH_STYLES)[attempt.auth_style]
    config: dict[str, Any] = {
        "name": name,
        "method": "GET",
        "url": attempt.url,
        "headers": {},
        "query": {attempt.query_param: "{query}", page_param: "{page}"},
        "results_path": attempt.results_path,
        "pagination": {"style": "page", "param": page_param, "start": 1, "size": 20},
    }
    if attempt.auth_style.startswith("query:"):
        config["query"][auth_name] = "{api_key}"
    elif attempt.auth_style == "header:Authorization-Bearer":
        config["headers"]["Authorization"] = "Bearer {api_key}"
    else:
        config["headers"][auth_name] = "{api_key}"

    _, unresolved = describe_mapping(attempt.sample)
    if unresolved:
        config["_unmapped_fields"] = (
            "These Place fields were not found in the sample row. Add a "
            "field_map entry for any you care about, e.g. "
            '"field_map": {"website": "your_field_name"}. Available keys in '
            f"the sample: {sorted(attempt.sample)[:20]}"
        )
    return config


def redact(text: str, api_key: str) -> str:
    """Hide the key in anything printed."""
    if not api_key or len(api_key) < 8:
        return text
    return text.replace(api_key, f"{api_key[:4]}…{api_key[-2:]}")
