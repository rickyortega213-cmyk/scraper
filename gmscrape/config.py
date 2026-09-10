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
    """Load configuration sources, lowest priority last.

    Real environment variables always win, then a project .env, then the keys
    saved by `gmscrape setup` (~/.config/gmscrape/config.env).
    """
    if load_dotenv is not None:
        if env_file:
            load_dotenv(env_file, override=False)
        elif (Path.cwd() / ".env").exists():
            load_dotenv(Path.cwd() / ".env", override=False)
    from .keys import load_saved_keys_into_env   # local import: keys imports Settings

    load_saved_keys_into_env()


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
    mcp_maps_url: str = ""            # e.g. https://mcp.scraper.tech/<key>
    mcp_maps_tool: str = ""           # override the auto-picked tool name
    mcp_maps_args: str = ""           # JSON template overriding the argument mapping

    mailtester_key: str = ""
    mailtester_auth: str = "auto"     # auto | direct (key= on every call) | token (exchange first)
    mailtester_rate: int = 57         # requests per 10 s: Ultimate plan (11 Pro, 5 Starter); 0 = no limit
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
    web_search_concurrency: int = 64    # ceiling; backs off by itself on HTTP 429
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
    maps_concurrency: int = 16         # queries fetched in parallel
    maps_cache_ttl_hours: int = 168    # a crash never re-buys the same search
    batch_size: int = 100              # businesses per checkpoint
    query_chunk_size: int = 25         # queries fetched, then enriched, at a time (streaming)
    lean_threshold_queries: int = 200  # above this: append-only exports, no page cache, no full in-memory results
    lean_memory: bool = False          # force the large-run mode

    # --- website crawling --------------------------------------------------
    crawl_websites: bool = True
    max_pages_per_site: int = 5
    http_timeout: float = 10.0
    http_concurrency: int = 256
    per_host_concurrency: int = 2
    http_retries: int = 1              # a dead host costs one retry, not a minute
    http_max_bytes: int = 1_000_000    # contact details live in the first megabyte; 256 sockets x 3 MB was a memory spike
    obey_robots: bool = True
    user_agent: str = DEFAULT_USER_AGENTS[0]
    rotate_user_agent: bool = True
    crawl_delay: float = 0.0
    site_timeout: float = 45.0        # whole-site budget: discover + crawl + owner search, then move on
    follow_social_profiles: bool = False

    # --- permutations ------------------------------------------------------
    permutations: bool = True
    permutation_tier: int = 2         # 1=safest few, 2=common, 3=aggressive
    permutation_max: int = 3          # info@, contact@, hello@ - only when a site published nothing
    permutation_require_mx: bool = True
    permutations_for_chains: bool = False
    stop_on_first_valid: bool = True
    owner_permutation_max: int = 2    # first@, first.last@ - only when the owner's address was not published
    require_verified_guesses: bool = True   # a guess must verify `valid` to become a lead row

    # --- verification ------------------------------------------------------
    verify_emails: bool = True
    verify_found: bool = True
    verify_found_max: int = 1         # found addresses checked per business per contact type, best first (0 = all)
    prepare_ahead: int = 8            # batches crawled at once, ahead of verification; one slow site
                                      # only holds up its own batch, and HTTP_CONCURRENCY is shared
    verify_permutations: bool = True
    verify_concurrency: int = 16      # enough in flight to use the Ultimate rate
    verify_budget: int = 0            # 0 = unlimited API calls for the run
    keep_risky: bool = True
    keep_invalid: bool = False

    # --- chains ------------------------------------------------------------
    chain_mode: str = "flag"          # flag|skip|only
    chain_review_threshold: int = 1500
    chain_people: bool = True         # look for the local manager / franchisee at chains
    chain_person_guesses: bool = True # guess that person's mailbox on the corporate domain
    chain_crawl_pages: int = 2        # corporate sites rarely name local staff - stay shallow

    # --- Supabase live table -----------------------------------------------
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_schema: str = "public"
    supabase_prefix: str = "gmscrape_"
    supabase_access_token: str = ""      # the one credential needed: sbp_... access token
    supabase_project_ref: str = ""       # which project, when the token can see several
    supabase_run_tables: bool = True     # a fresh table per run, named after the search + date
    supabase_table_name: str = ""        # name for this run's table (default: from the search)
    supabase: bool = True                # on whenever URL + key are configured

    # --- output / storage --------------------------------------------------
    db_path: str = "out/gmscrape.sqlite"
    out_dir: str = "out"
    cache_http: bool = True
    cache_ttl_hours: int = 168
    cache_max_page_bytes: int = 400_000   # stored per page (compressed); the crawl still reads up to http_max_bytes
    export_formats: tuple[str, ...] = ("csv", "json")
    min_confidence: int = 0
    log_level: str = "INFO"
    run_hours: float = 2.0            # time budget: guessing stops when it runs out (0 = no limit)
    confirm_keys_on_start: bool = True   # show keys before a run and offer to change them

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
        """Either the access token alone, or an explicit URL + service key."""
        return bool(self.supabase_access_token) or bool(self.supabase_url and self.supabase_key)

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
            "mcp": self.mcp_maps_url,
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



# Rough metered checks per business, by what they buy - used only for the plan
# shown before a run. Measured on local-business lists; yours will differ.
CHECKS_FOUND = 0.30          # ~30% of businesses publish an address; one check each
CHECKS_OWNER_GUESS = 0.15    # owner named but no owner address: first@, first.last@
CHECKS_GENERIC_GUESS = 0.45  # site published nothing: info@, contact@, hello@


def run_plan(businesses: int, keys: int, checks_per_10s: int, hours: float) -> dict[str, float]:
    """What a time budget buys. Found addresses are always checked; the
    guesses are checked afterwards, most valuable first, until time runs out."""
    per_hour = max(1, keys) * max(1, checks_per_10s) * 360
    capacity = per_hour * hours if hours > 0 else float("inf")
    found = businesses * CHECKS_FOUND
    owner = businesses * CHECKS_OWNER_GUESS
    generic = businesses * CHECKS_GENERIC_GUESS
    return {
        "capacity": capacity,
        "checks_per_hour": per_hour,
        "found": found,
        "owner_guesses": owner,
        "generic_guesses": generic,
        "hours_found": found / per_hour,
        "hours_all": (found + owner + generic) / per_hour,
        "keys_for_all": max(1, -(-(found + owner + generic) // (max(1, checks_per_10s) * 360 * hours))) if hours > 0 else 1,
    }
