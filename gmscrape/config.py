"""Runtime configuration, loaded from environment / .env with CLI overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

DEFAULT_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    try:
        return int(str(raw).strip()) if raw not in (None, "") else default
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    try:
        return float(str(raw).strip()) if raw not in (None, "") else default
    except ValueError:
        return default


def load_env(env_file: Optional[str] = None) -> None:
    """Load .env once, before Settings is built."""
    if load_dotenv is None:
        return
    if env_file:
        load_dotenv(env_file, override=False)
        return
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)
            return


@dataclass
class Settings:
    """Everything tunable. Env var names match the uppercased field name."""

    # --- providers ---------------------------------------------------------
    maps_provider: str = "auto"       # auto|serpapi|serper|outscraper|apify|scrapingdog|generic|file
    verify_provider: str = "auto"     # auto|local|millionverifier|zerobounce|neverbounce|reoon|emaillistverify|bouncer|generic
    maps_api_key: str = ""            # generic/unspecified provider key
    verify_api_key: str = ""

    serpapi_key: str = ""
    serper_key: str = ""
    outscraper_key: str = ""
    apify_token: str = ""
    apify_actor: str = "compass~crawler-google-places"
    scrapingdog_key: str = ""
    scraperapi_key: str = ""

    mailtester_key: str = ""
    millionverifier_key: str = ""
    zerobounce_key: str = ""
    neverbounce_key: str = ""
    reoon_key: str = ""
    emaillistverify_key: str = ""
    bouncer_key: str = ""

    # Web search (website discovery + owner lookup)
    web_search_provider: str = "auto"   # auto|openwebninja|none
    openwebninja_key: str = ""
    discover_websites: bool = True      # search for a site when Maps has none
    find_owners: bool = True            # look for the owner on the website
    owner_search: bool = True           # ...and via web search when the site is silent
    web_search_concurrency: int = 6
    search_cache_ttl_hours: int = 720
    website_min_confidence: int = 60    # accept a discovered site at/above this
    owner_min_confidence: int = 60      # guess owner addresses at/above this

    # Generic (config-driven) adapters - see providers/maps/generic.py
    generic_maps_config: str = ""     # path to JSON mapping file
    generic_verify_config: str = ""
    places_file: str = ""             # for maps_provider=file

    # --- search ------------------------------------------------------------
    results_per_query: int = 40
    language: str = "en"
    country: str = "us"
    maps_max_pages: int = 5

    # --- website crawling --------------------------------------------------
    crawl_websites: bool = True
    max_pages_per_site: int = 6
    http_timeout: float = 20.0
    http_concurrency: int = 12
    per_host_concurrency: int = 2
    http_retries: int = 2
    http_max_bytes: int = 3_000_000
    obey_robots: bool = True
    user_agent: str = DEFAULT_USER_AGENTS[0]
    rotate_user_agent: bool = True
    crawl_delay: float = 0.0
    follow_social_profiles: bool = False

    # --- permutations ------------------------------------------------------
    permutations: bool = True
    permutation_tier: int = 2         # 1=safest few, 2=common, 3=aggressive
    permutation_max: int = 12
    permutation_require_mx: bool = True
    permutations_for_chains: bool = False
    stop_on_first_valid: bool = True
    owner_permutation_max: int = 8
    require_verified_guesses: bool = True   # a guess must verify `valid` to become a lead row

    # --- verification ------------------------------------------------------
    verify_emails: bool = True
    verify_found: bool = True
    verify_permutations: bool = True
    verify_concurrency: int = 4
    verify_budget: int = 0            # 0 = unlimited API calls for the run
    keep_risky: bool = True
    keep_invalid: bool = False

    # --- chains ------------------------------------------------------------
    chain_mode: str = "flag"          # flag|skip|only
    chain_review_threshold: int = 1500

    # --- Supabase live table -----------------------------------------------
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_schema: str = "public"
    supabase_prefix: str = "gmscrape_"
    supabase: bool = False

    # --- output / storage --------------------------------------------------
    db_path: str = "out/gmscrape.sqlite"
    out_dir: str = "out"
    cache_http: bool = True
    cache_ttl_hours: int = 168
    export_formats: tuple[str, ...] = ("csv", "json")
    min_confidence: int = 0
    log_level: str = "INFO"

    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides: Any) -> "Settings":
        """Build settings from env vars, then apply non-None CLI overrides."""
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name == "extra":
                continue
            key = f.name.upper()
            if f.type == "bool" or isinstance(getattr(cls, f.name, None), bool):
                values[f.name] = _env_bool(key, getattr(cls, f.name))
            elif isinstance(getattr(cls, f.name, None), int) and not isinstance(getattr(cls, f.name), bool):
                values[f.name] = _env_int(key, getattr(cls, f.name))
            elif isinstance(getattr(cls, f.name, None), float):
                values[f.name] = _env_float(key, getattr(cls, f.name))
            elif isinstance(getattr(cls, f.name, None), tuple):
                raw = os.getenv(key)
                values[f.name] = tuple(x.strip() for x in raw.split(",") if x.strip()) if raw else getattr(cls, f.name)
            else:
                values[f.name] = os.getenv(key, getattr(cls, f.name))
        for key, value in overrides.items():
            if value is None:
                continue
            if key in values:
                values[key] = value
            else:
                values.setdefault("extra", {})
                values["extra"][key] = value
        return cls(**values)

    # --- convenience -------------------------------------------------------
    def ensure_dirs(self) -> None:
        Path(self.out_dir).mkdir(parents=True, exist_ok=True)
        db_parent = Path(self.db_path).expanduser().parent
        if str(db_parent):
            db_parent.mkdir(parents=True, exist_ok=True)

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    @property
    def web_search_configured(self) -> bool:
        return bool(self.openwebninja_key) and self.web_search_provider != "none"

    def configured_maps_keys(self) -> dict[str, str]:
        return {
            "serpapi": self.serpapi_key,
            "serper": self.serper_key,
            "outscraper": self.outscraper_key,
            "apify": self.apify_token,
            "scrapingdog": self.scrapingdog_key,
            "scraperapi": self.scraperapi_key,
            "generic": self.generic_maps_config,
            "file": self.places_file,
        }

    def configured_verify_keys(self) -> dict[str, str]:
        return {
            "mailtester": self.mailtester_key,
            "millionverifier": self.millionverifier_key,
            "zerobounce": self.zerobounce_key,
            "neverbounce": self.neverbounce_key,
            "reoon": self.reoon_key,
            "emaillistverify": self.emaillistverify_key,
            "bouncer": self.bouncer_key,
            "generic": self.generic_verify_config,
        }
