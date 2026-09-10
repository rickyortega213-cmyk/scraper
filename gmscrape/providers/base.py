"""Provider base classes plus the shared HTTP/normalization plumbing."""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Any, Iterator, Optional, Sequence

import httpx

from ..config import Settings
from ..models import Place, QuerySpec, VerificationResult, V_UNKNOWN
from ..util import (
    as_float,
    as_int,
    clean_phone,
    first_dig,
    has_valid_suffix,
    normalize_url,
    registered_domain,
    squeeze,
)

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524}


class ProviderError(RuntimeError):
    """Raised when a provider is misconfigured or the API refuses to answer."""


class ProviderAuthError(ProviderError):
    """The provider rejected the key or the subscription itself. Retrying is
    pointless and every further call would be wasted: the run must stop and
    the person must fix the key."""


class ApiClient:
    """Small synchronous HTTP client with retry/backoff for provider APIs."""

    def __init__(
        self,
        timeout: float = 45.0,
        retries: int = 3,
        headers: Optional[dict[str, str]] = None,
        base_backoff: float = 1.5,
    ) -> None:
        self.retries = max(0, retries)
        self.base_backoff = base_backoff
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"Accept": "application/json", **(headers or {})},
        )

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                response = self._client.request(method, url, **kwargs)
                if response.status_code in RETRY_STATUS and attempt < self.retries:
                    delay = self._retry_delay(response, attempt)
                    log.warning(
                        "%s %s -> HTTP %s, retrying in %.1fs",
                        method, url, response.status_code, delay,
                    )
                    time.sleep(delay)
                    continue
                return response
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                delay = self.base_backoff * (2 ** attempt) + random.uniform(0, 0.4)
                log.warning("%s %s failed (%s), retrying in %.1fs", method, url, exc, delay)
                time.sleep(delay)
        raise ProviderError(f"request to {url} failed: {last_error}")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self._json(self.request("GET", url, **kwargs), url)

    def post_json(self, url: str, **kwargs: Any) -> Any:
        return self._json(self.request("POST", url, **kwargs), url)

    @staticmethod
    def _json(response: httpx.Response, url: str) -> Any:
        if response.status_code >= 400:
            snippet = response.text[:300].replace("\n", " ")
            raise ProviderError(f"{url} -> HTTP {response.status_code}: {snippet}")
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"{url} returned non-JSON body: {response.text[:200]}") from exc

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return min(60.0, float(header))
            except ValueError:
                pass
        return self.base_backoff * (2 ** attempt) + random.uniform(0, 0.4)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --- field mapping ---------------------------------------------------------
# Candidate dotted paths for each normalized Place field. Providers reuse
# these so a new API usually needs no bespoke parsing code at all.
PLACE_FIELD_PATHS: dict[str, Sequence[str]] = {
    "name": ("name", "title", "business_name", "businessName", "query_name", "displayName.text"),
    "place_id": ("place_id", "placeId", "google_id", "googleId", "gmaps_id", "gmapsId",
                 "google_place_id", "maps_id", "business_id", "listing_id", "id", "cid",
                 "fid", "data_id"),
    "category": ("category", "type", "categories.0", "types.0", "primary_category",
                 "primaryType", "categoryName", "subtypes"),
    "address": ("address", "full_address", "formatted_address", "formattedAddress",
                "vicinity", "location.address", "address.full"),
    "street": ("street", "address.street", "street_address", "addressLine1"),
    "city": ("city", "address.city", "locality", "town"),
    "state": ("state", "address.state", "region", "administrative_area_level_1", "us_state"),
    "postal_code": ("postal_code", "postalCode", "zip", "zipcode", "address.postal_code"),
    "country": ("country", "country_code", "countryCode", "address.country"),
    "phone": ("phone", "phone_number", "phoneNumber", "formatted_phone_number",
              "international_phone_number", "nationalPhoneNumber", "telephone", "tel",
              "contact_phone"),
    "website": ("website", "site", "url", "web", "web_url", "website_url", "websiteUri",
                "site_url", "homepage", "domain"),
    "latitude": ("latitude", "lat", "location.lat", "location.latitude", "gps_coordinates.latitude",
                 "coordinates.lat", "geometry.location.lat"),
    "longitude": ("longitude", "lng", "lon", "location.lng", "location.longitude",
                  "gps_coordinates.longitude", "coordinates.lng", "geometry.location.lng"),
    "rating": ("rating", "avg_rating", "average_rating", "reviews_rating", "rating_value",
               "ratingValue", "stars", "star_rating", "score"),
    "reviews": ("reviews", "reviews_count", "reviewsCount", "review_count", "reviewCount",
                "num_reviews", "total_reviews", "user_ratings_total", "userRatingCount",
                "ratings_total"),
    "price_level": ("price_level", "price", "priceLevel", "price_range"),
    "hours": ("hours", "working_hours", "opening_hours.weekday_text", "workingHours"),
    "google_url": ("google_url", "maps_url", "link", "google_maps_url", "googleUrl",
                   "place_link", "url_google"),
}


def place_from_mapping(
    raw: dict[str, Any],
    *,
    query: str,
    source: str,
    field_map: Optional[dict[str, Sequence[str]]] = None,
) -> Optional[Place]:
    """Normalize an arbitrary provider result dict into a Place."""
    if not isinstance(raw, dict):
        return None
    paths = dict(PLACE_FIELD_PATHS)
    if field_map:
        for key, value in field_map.items():
            paths[key] = (value,) if isinstance(value, str) else tuple(value)

    def pick(key: str) -> Any:
        return first_dig(raw, paths.get(key, ()))

    name = squeeze(str(pick("name") or ""))
    if not name:
        return None

    website_raw = pick("website")
    if isinstance(website_raw, dict):
        website_raw = website_raw.get("url") or website_raw.get("href") or ""
    website = normalize_url(str(website_raw or ""))
    domain = registered_domain(website)
    if not domain:
        # A source that already knows the mail domain (a CSV you enriched by
        # hand, an intranet test fixture) may say so explicitly.
        explicit = str(first_dig(raw, ("domain", "email_domain")) or "").strip().lower()
        if explicit and has_valid_suffix(explicit):
            domain = registered_domain(explicit) or explicit

    hours = pick("hours")
    if isinstance(hours, (list, tuple)):
        hours = "; ".join(str(h) for h in hours)
    elif isinstance(hours, dict):
        hours = "; ".join(f"{k}: {v}" for k, v in hours.items())

    category = pick("category")
    if isinstance(category, (list, tuple)):
        category = ", ".join(str(c) for c in category[:3])

    return Place(
        name=name,
        query=query,
        source=source,
        place_id=squeeze(str(pick("place_id") or "")),
        category=squeeze(str(category or "")),
        address=squeeze(str(pick("address") or "")),
        street=squeeze(str(pick("street") or "")),
        city=squeeze(str(pick("city") or "")),
        state=squeeze(str(pick("state") or "")),
        postal_code=squeeze(str(pick("postal_code") or "")),
        country=squeeze(str(pick("country") or "")),
        phone=clean_phone(pick("phone")),
        website=website,
        domain=domain,
        latitude=as_float(pick("latitude")),
        longitude=as_float(pick("longitude")),
        rating=as_float(pick("rating")),
        reviews=as_int(pick("reviews")),
        price_level=squeeze(str(pick("price_level") or "")),
        hours=squeeze(str(hours or "")),
        google_url=squeeze(str(pick("google_url") or "")),
        raw=raw,
    )


class MapsProvider(ABC):
    """Source of Google Maps business listings for a parsed query."""

    name: str = "base"
    requires_key: bool = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = ApiClient(timeout=60.0, retries=settings.http_retries + 1)

    @abstractmethod
    def search(self, spec: QuerySpec, limit: int) -> Iterator[Place]:
        """Yield up to `limit` places for the query."""

    def close(self) -> None:
        self.client.close()

    # helper for subclasses
    def _place(self, raw: dict[str, Any], spec: QuerySpec) -> Optional[Place]:
        return place_from_mapping(raw, query=spec.search_string, source=self.name)


class EmailVerifier(ABC):
    """Email deliverability check."""

    name: str = "base"
    requires_key: bool = True
    supports_catch_all_probe: bool = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = ApiClient(timeout=45.0, retries=2)

    @abstractmethod
    def verify(self, email: str) -> VerificationResult:
        """Check one address."""

    def preflight(self) -> None:
        """Cheap check that the key will work, done before any money is spent.
        Raises ProviderAuthError when it will not; other errors mean "unsure"."""
        return None

    def verify_many(self, emails: Sequence[str]) -> dict[str, VerificationResult]:
        return {email: self.verify(email) for email in emails}

    def is_catch_all(self, domain: str) -> Optional[bool]:
        """Whether a domain accepts mail for any local part (None = unknown)."""
        return None

    def close(self) -> None:
        self.client.close()

    def _unknown(self, error: str = "") -> VerificationResult:
        return VerificationResult(status=V_UNKNOWN, provider=self.name, error=error)
