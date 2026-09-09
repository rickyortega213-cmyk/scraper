"""Provider lookup and auto-detection."""

from __future__ import annotations

import logging
from typing import Type

from ..config import Settings
from .base import EmailVerifier, MapsProvider, ProviderError
from .maps.apify import ApifyMaps
from .maps.file_provider import FileMaps
from .maps.generic import GenericMaps
from .maps.outscraper import OutscraperMaps
from .maps.scrapingdog import ScrapingDogMaps
from .maps.serpapi import SerpApiMaps
from .maps.serper import SerperMaps
from .verify.generic import GenericVerifier
from .verify.local import LocalVerifier
from .verify.vendors import VENDOR_CLASSES

log = logging.getLogger(__name__)

MAPS_PROVIDERS: dict[str, Type[MapsProvider]] = {
    SerpApiMaps.name: SerpApiMaps,
    SerperMaps.name: SerperMaps,
    OutscraperMaps.name: OutscraperMaps,
    ApifyMaps.name: ApifyMaps,
    ScrapingDogMaps.name: ScrapingDogMaps,
    "generic": GenericMaps,
    "file": FileMaps,
}

VERIFY_PROVIDERS: dict[str, Type[EmailVerifier]] = {
    "local": LocalVerifier,
    "generic": GenericVerifier,
    **VENDOR_CLASSES,
}

# Auto-detection order: whichever credential is present wins, most specific
# (a real maps API) before the local-file fallback.
MAPS_AUTO_ORDER = ("generic", "serpapi", "serper", "outscraper", "apify", "scrapingdog", "file")
VERIFY_AUTO_ORDER = (
    "generic", "millionverifier", "zerobounce", "neverbounce", "reoon",
    "emaillistverify", "bouncer",
)


def list_maps_providers() -> list[str]:
    return sorted(MAPS_PROVIDERS)


def list_verify_providers() -> list[str]:
    return sorted(VERIFY_PROVIDERS)


def detect_maps_provider(settings: Settings) -> str:
    keys = settings.configured_maps_keys()
    for name in MAPS_AUTO_ORDER:
        if keys.get(name):
            return name
    raise ProviderError(
        "No Google Maps provider configured. Set one of "
        "SERPAPI_KEY / SERPER_KEY / OUTSCRAPER_KEY / APIFY_TOKEN / "
        "SCRAPINGDOG_KEY, or describe your own API with GENERIC_MAPS_CONFIG "
        "(see examples/maps_api.example.json), or pass "
        "--maps-provider file --places-file <path>."
    )


def detect_verify_provider(settings: Settings) -> str:
    keys = settings.configured_verify_keys()
    for name in VERIFY_AUTO_ORDER:
        if keys.get(name):
            return name
    return "local"


def get_maps_provider(settings: Settings, name: str | None = None) -> MapsProvider:
    chosen = (name or settings.maps_provider or "auto").lower()
    if chosen == "auto":
        chosen = detect_maps_provider(settings)
        log.info("auto-selected maps provider: %s", chosen)
    if chosen not in MAPS_PROVIDERS:
        raise ProviderError(
            f"unknown maps provider {chosen!r}; available: {', '.join(list_maps_providers())}"
        )
    return MAPS_PROVIDERS[chosen](settings)


def get_verifier(settings: Settings, name: str | None = None) -> EmailVerifier:
    chosen = (name or settings.verify_provider or "auto").lower()
    if chosen == "auto":
        chosen = detect_verify_provider(settings)
        log.info("auto-selected verification provider: %s", chosen)
    if chosen not in VERIFY_PROVIDERS:
        raise ProviderError(
            f"unknown verification provider {chosen!r}; available: "
            f"{', '.join(list_verify_providers())}"
        )
    return VERIFY_PROVIDERS[chosen](settings)
