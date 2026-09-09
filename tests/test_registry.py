"""Provider auto-detection."""

from __future__ import annotations

import pytest

from gmscrape.config import Settings
from gmscrape.providers.base import ProviderError
from gmscrape.providers.registry import detect_maps_provider, detect_verify_provider


def test_maps_key_selects_its_provider():
    assert detect_maps_provider(Settings.from_env(scraperapi_key="k")) == "scraperapi"
    assert detect_maps_provider(Settings.from_env(serpapi_key="k")) == "serpapi"
    assert detect_maps_provider(Settings.from_env(places_file="p.csv")) == "file"


def test_generic_needs_its_config_file_not_just_a_key():
    """A bare MAPS_API_KEY must not select an adapter that has nothing to call."""
    with pytest.raises(ProviderError, match="No Google Maps provider configured"):
        detect_maps_provider(Settings.from_env(maps_api_key="k"))

    # A key alongside a real provider selects that provider, not `generic`.
    assert detect_maps_provider(
        Settings.from_env(maps_api_key="k", scraperapi_key="k")
    ) == "scraperapi"

    # With a config file, `generic` wins - it is the explicit choice.
    assert detect_maps_provider(
        Settings.from_env(generic_maps_config="api.json", scraperapi_key="k")
    ) == "generic"


def test_verification_falls_back_to_local():
    assert detect_verify_provider(Settings.from_env()) == "local"
    assert detect_verify_provider(Settings.from_env(verify_api_key="k")) == "local"
    assert detect_verify_provider(Settings.from_env(mailtester_key="k")) == "mailtester"
    assert detect_verify_provider(
        Settings.from_env(generic_verify_config="v.json")
    ) == "generic"


def test_unknown_provider_names_are_rejected():
    from gmscrape.providers import get_maps_provider, get_verifier

    with pytest.raises(ProviderError, match="unknown maps provider"):
        get_maps_provider(Settings.from_env(), "nope")
    with pytest.raises(ProviderError, match="unknown verification provider"):
        get_verifier(Settings.from_env(), "nope")
