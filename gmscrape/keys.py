"""Saved API keys: first-run setup, persistence, and the startup check.

Keys live in one place outside the project folder, so they survive a fresh
clone and work from any directory:

    ~/.config/gmscrape/config.env          (Linux / macOS)
    %APPDATA%\\gmscrape\\config.env          (Windows)

Precedence when the same key is set in more than one place:
    real environment variables  >  ./.env in the project  >  saved config

`gmscrape setup` walks through every key, showing the saved value masked;
Enter keeps it, typing replaces it, "-" clears it. `gmscrape run` shows what
it is about to use and offers to change it before spending credits.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import Settings

Prompt = Callable[[str], str]   # input() or a test stand-in


@dataclass(frozen=True)
class KeyField:
    env: str            # environment variable name
    label: str          # what it is
    hint: str           # where to get it / what it looks like
    group: str          # maps | verify | search | supabase
    secret: bool = True


KEY_FIELDS: tuple[KeyField, ...] = (
    KeyField("MCP_MAPS_URL", "Scraper Tech MCP link",
             "https://mcp.scraper.tech/<your key> - from your scraper.tech panel", "maps"),
    KeyField("SCRAPERAPI_KEY", "ScraperAPI key", "32-char hex, from scraperapi.com dashboard", "maps"),
    KeyField("SERPAPI_KEY", "SerpApi key", "serpapi.com/manage-api-key", "maps"),
    KeyField("SERPER_KEY", "Serper.dev key", "serper.dev/api-key", "maps"),
    KeyField("OUTSCRAPER_KEY", "Outscraper key", "app.outscraper.com/profile", "maps"),
    KeyField("APIFY_TOKEN", "Apify token", "console.apify.com/account/integrations", "maps"),
    KeyField("SCRAPINGDOG_KEY", "ScrapingDog key", "api.scrapingdog.com dashboard", "maps"),
    KeyField("GENERIC_MAPS_CONFIG", "Custom maps API config file",
             "path to the JSON written by `gmscrape probe-maps --write` (e.g. scraper.tech)",
             "maps", secret=False),
    KeyField("MAPS_API_KEY", "Custom maps API key", "the key for the config file above", "maps"),
    KeyField("MAILTESTER_KEY", "MailTester Ninja key", "your sub_... subscription id", "verify"),
    KeyField("MILLIONVERIFIER_KEY", "MillionVerifier key", "app.millionverifier.com/api", "verify"),
    KeyField("ZEROBOUNCE_KEY", "ZeroBounce key", "zerobounce.net/members/apikey", "verify"),
    KeyField("NEVERBOUNCE_KEY", "NeverBounce key", "app.neverbounce.com/settings/api", "verify"),
    KeyField("REOON_KEY", "Reoon key", "emailverifier.reoon.com/api-settings", "verify"),
    KeyField("EMAILLISTVERIFY_KEY", "EmailListVerify key", "emaillistverify.com/api", "verify"),
    KeyField("BOUNCER_KEY", "Bouncer key", "app.usebouncer.com/api", "verify"),
    KeyField("OPENWEBNINJA_KEY", "OpenWeb Ninja key",
             "ak_... from openwebninja.com - website discovery + owner lookup", "search"),
    KeyField("SUPABASE_URL", "Supabase project URL",
             "https://<project>.supabase.co - Project Settings → API", "supabase", secret=False),
    KeyField("SUPABASE_KEY", "Supabase project API key",
             "the secret / service_role key from Project Settings → API (never the anon one)",
             "supabase"),
    KeyField("SUPABASE_ACCESS_TOKEN", "Supabase access token (optional alternative)",
             "sbp_... account token; can create tables without the one-time SQL paste",
             "supabase"),
    KeyField("SUPABASE_PROJECT_REF", "Supabase project ref (only with a token and several projects)",
             "the id in your project URL", "supabase", secret=False),
)

GROUP_TITLES = {
    "maps": "1. Google Maps scraping (set one)",
    "verify": "2. Email verification (set one; none = local checks only)",
    "search": "3. Web search (website discovery + owner lookup; optional)",
    "supabase": "4. Supabase live table (optional)",
}


# --- storage ---------------------------------------------------------------
def user_config_path() -> Path:
    override = os.getenv("GMSCRAPE_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "gmscrape" / "config.env"


def read_saved_keys(path: Optional[Path] = None) -> dict[str, str]:
    path = path or user_config_path()
    if not path.exists():
        return {}
    saved: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        saved[key.strip()] = value.strip().strip('"').strip("'")
    return saved


def save_keys(updates: dict[str, str], path: Optional[Path] = None) -> Path:
    """Merge `updates` into the saved file. An empty value removes the key."""
    path = path or user_config_path()
    current = read_saved_keys(path)
    for key, value in updates.items():
        if value:
            current[key] = value
        else:
            current.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# gmscrape saved keys - edit with `gmscrape setup`", ""]
    lines += [f"{key}={value}" for key, value in sorted(current.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)     # keys are secrets: owner-only
    except OSError:
        pass
    return path


def load_saved_keys_into_env(path: Optional[Path] = None) -> int:
    """Apply saved keys as defaults - never overriding the real environment."""
    count = 0
    for key, value in read_saved_keys(path).items():
        if not os.getenv(key):
            os.environ[key] = value
            count += 1
    return count


# --- presentation ------------------------------------------------------------
def mask(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-3:]}"


def field_by_env(env: str) -> Optional[KeyField]:
    return next((f for f in KEY_FIELDS if f.env == env), None)


def current_values() -> dict[str, str]:
    return {f.env: os.getenv(f.env, "") for f in KEY_FIELDS}


@dataclass
class KeyStatus:
    maps: str
    verify: str
    search: str
    supabase: str

    @property
    def any_configured(self) -> bool:
        return self.maps != "none"


def key_status(settings: Settings) -> KeyStatus:
    from .providers.registry import detect_verify_provider
    from .providers.base import ProviderError
    from .providers.registry import detect_maps_provider

    try:
        maps = detect_maps_provider(settings)
    except ProviderError:
        maps = "none"
    verify = detect_verify_provider(settings)
    search = "openwebninja" if settings.web_search_configured else "off"
    supabase = "on" if settings.supabase_configured else "off"
    return KeyStatus(maps=maps, verify=verify, search=search, supabase=supabase)


# --- the wizard --------------------------------------------------------------
def run_setup(
    prompt: Prompt = input,
    echo: Callable[[str], None] = print,
    *,
    path: Optional[Path] = None,
    groups: tuple[str, ...] = ("maps", "verify", "search", "supabase"),
) -> dict[str, str]:
    """Walk through the keys; returns what was saved (env var -> value)."""
    echo("")
    echo("gmscrape setup - Enter keeps the current value, type to replace, '-' to clear.")
    echo(f"Saved to {path or user_config_path()}")
    values = current_values()
    updates: dict[str, str] = {}
    for group in groups:
        echo("")
        echo(GROUP_TITLES[group])
        for field in (f for f in KEY_FIELDS if f.group == group):
            current = values.get(field.env, "")
            shown = mask(current) if field.secret else (current or "(not set)")
            answer = prompt(f"  {field.label} [{shown}]  ({field.hint}): ").strip()
            if not answer:
                continue
            if answer == "-":
                updates[field.env] = ""
                os.environ.pop(field.env, None)
                echo(f"    cleared {field.env}")
                continue
            updates[field.env] = answer
            os.environ[field.env] = answer
            echo(f"    set {field.env} = {mask(answer) if field.secret else answer}")
    if updates:
        saved_to = save_keys(updates, path)
        echo("")
        echo(f"Saved {len(updates)} change(s) to {saved_to}")
    else:
        echo("")
        echo("No changes.")
    return updates


def interactive() -> bool:
    """Only prompt when a person is at the keyboard - never in cron or CI."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False
