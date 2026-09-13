"""Command line interface.

    gmscrape setup                      # save / change your API keys (first run does this)
    gmscrape keys                       # show what is configured, masked
    gmscrape run "dentist in austin tx" "plumber in miami fl"
    gmscrape run -f queries.txt --limit 60 --format all
    gmscrape enrich --places-file leads.csv
    gmscrape verify info@acme.com sales@acme.com
    gmscrape extract https://example.com
    gmscrape guess acme.com --business-name "Joe's Plumbing"
    gmscrape probe-maps https://api.example.com/maps --key KEY
    gmscrape search "who is the owner of Joe's Plumbing in Austin"
    gmscrape owner "Joe's Plumbing" --city Austin
    gmscrape supabase-init --write supabase_schema.sql
    gmscrape run "dentist in austin tx" --supabase
    gmscrape doctor
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .config import Settings, load_env
from .core.pipeline import Pipeline, RunReport, RunStopped
from .emails.patterns import build_permutations
from .providers import get_maps_provider, get_verifier, list_maps_providers, list_verify_providers
from .providers.base import ProviderAuthError, ProviderError
from .providers.registry import detect_maps_provider, detect_verify_provider
from .query import parse_queries, read_query_file
from .store.db import Store
from .store.export import CsvAppender, export_results
from .web import unsafe

log = logging.getLogger("gmscrape")

try:
    from rich.console import Console
    from rich.table import Table
    _console: Optional["Console"] = Console()
except ImportError:  # pragma: no cover - rich is optional
    _console = None


def echo(message: str, style: str = "") -> None:
    if _console is not None:
        _console.print(message, style=style or None)
    else:
        print(_strip_markup(message))


def _strip_markup(text: str) -> str:
    import re
    return re.sub(r"\[/?[a-z0-9 _#]+\]", "", text)


# --- argument parsing ------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gmscrape",
        description="Google Maps lead scraper: find local businesses, discover their "
                    "email addresses from their website, guess and verify the rest.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"gmscrape {__version__}")

    # Shared flags, accepted either before or after the subcommand. SUPPRESS
    # keeps the subparser copy from overwriting a value given up front.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", default=argparse.SUPPRESS,
                        help="path to a .env file to load")
    common.add_argument("--log-level", default=argparse.SUPPRESS,
                        help="DEBUG | INFO | WARNING | ERROR (default INFO)")
    common.add_argument("--db", dest="db_path", default=argparse.SUPPRESS,
                        help="SQLite path (default out/gmscrape.sqlite)")
    for action in common._actions:
        parser._add_action(action)

    sub = parser.add_subparsers(dest="command", required=True)

    # --- run -------------------------------------------------------------
    run = sub.add_parser("run", help="scrape queries end to end", parents=[common])
    run.add_argument("queries", nargs="*", help='e.g. "dentist in austin tx"')
    run.add_argument("-f", "--queries-file", help="file with one query per line")
    run.add_argument("-n", "--limit", type=int, dest="results_per_query",
                     help="max businesses per query (default 40)")
    run.add_argument("--maps-provider", choices=["auto", *list_maps_providers()],
                     dest="maps_provider")
    run.add_argument("--verify-provider", choices=["auto", *list_verify_providers()],
                     dest="verify_provider")
    run.add_argument("--places-file", help="for --maps-provider file")
    run.add_argument("-y", "--yes", dest="confirm_keys_on_start", action="store_false",
                     default=None, help="start without the key check (for scripts / cron)")
    run.add_argument("--resume", metavar="RUN_ID", nargs="?", const="latest",
                     help="continue an interrupted run (default: the latest one)")
    run.add_argument("--table-name", dest="supabase_table_name",
                     help="name for this run's Supabase table (default: from the search)")
    run.add_argument("--no-supervise", dest="no_supervise", action="store_true",
                     help="run in this process without the restart-on-kill supervisor")
    run.add_argument("--hours", type=float, dest="run_hours", default=None,
                     help="time budget: addresses found on sites are always checked; guessing "
                          "owner/info@ mailboxes stops when this runs out (default 2, 0 = no limit)")
    run.add_argument("--batch-size", type=int, dest="batch_size",
                     help="businesses per checkpoint (default 100)")
    run.add_argument("-o", "--out-dir", dest="out_dir", help="export directory (default out/)")
    run.add_argument("--basename", default="leads", help="export file basename")
    run.add_argument("--format", dest="export_formats",
                     help="csv,json,jsonl,xlsx or all (default csv,json)")

    crawl = run.add_argument_group("website crawling")
    crawl.add_argument("--no-crawl", dest="crawl_websites", action="store_false", default=None,
                       help="skip website scraping entirely")
    crawl.add_argument("--max-pages", type=int, dest="max_pages_per_site",
                       help="pages per site, homepage included (default 6)")
    crawl.add_argument("--concurrency", type=int, dest="http_concurrency",
                       help="parallel site fetches (default 12)")
    crawl.add_argument("--timeout", type=float, dest="http_timeout", help="per-request seconds")
    crawl.add_argument("--no-robots", dest="obey_robots", action="store_false", default=None,
                       help="do not consult robots.txt")
    crawl.add_argument("--no-cache", dest="cache_http", action="store_false", default=None,
                       help="ignore the page cache")

    people = run.add_argument_group("website discovery & owners (needs OPENWEBNINJA_KEY)")
    people.add_argument("--no-discover", dest="discover_websites", action="store_false",
                        default=None, help="don't search for a site when Maps has none")
    people.add_argument("--no-owners", dest="find_owners", action="store_false", default=None,
                        help="skip owner lookup and owner-address guessing")
    people.add_argument("--no-owner-search", dest="owner_search", action="store_false",
                        default=None, help="find owners on the site only, never via web search")
    people.add_argument("--owner-min-confidence", type=int, dest="owner_min_confidence")
    people.add_argument("--website-min-confidence", type=int, dest="website_min_confidence")

    guess = run.add_argument_group("permutations")
    guess.add_argument("--no-permutations", dest="permutations", action="store_false",
                       default=None, help="never guess addresses")
    guess.add_argument("--tier", type=int, choices=[1, 2, 3], dest="permutation_tier",
                       help="1=info/contact/hello 2=common (default) 3=aggressive")
    guess.add_argument("--max-guesses", type=int, dest="permutation_max",
                       help="cap guesses per domain (default 12)")
    guess.add_argument("--guess-all", dest="stop_on_first_valid", action="store_false",
                       default=None, help="verify every guess instead of stopping at the first hit")
    guess.add_argument("--allow-unverified-guesses", dest="require_verified_guesses",
                       action="store_false", default=None,
                       help="let guesses that did not verify `valid` become lead rows")

    verify = run.add_argument_group("verification")
    verify.add_argument("--no-verify", dest="verify_emails", action="store_false", default=None,
                        help="skip verification")
    verify.add_argument("--verify-budget", type=int, dest="verify_budget",
                        help="max paid verification calls for the run (0 = unlimited)")
    verify.add_argument("--verify-concurrency", type=int, dest="verify_concurrency")
    verify.add_argument("--keep-invalid", dest="keep_invalid", action="store_true", default=None,
                        help="keep addresses that verified as invalid")
    verify.add_argument("--drop-risky", dest="keep_risky", action="store_false", default=None,
                        help="drop addresses that verified as risky")
    verify.add_argument("--min-confidence", type=int, dest="min_confidence",
                        help="drop emails scoring below this (0-100)")

    chains = run.add_argument_group("chains / big businesses")
    chains.add_argument("--chain-mode", choices=["flag", "skip", "only"], dest="chain_mode",
                        help="flag (default), skip national chains, or only chains")
    chains.add_argument("--no-chain-people", dest="chain_people", action="store_false",
                        default=None, help="don't look for the franchisee / store manager at chains")
    chains.add_argument("--no-chain-guesses", dest="chain_person_guesses", action="store_false",
                        default=None, help="never guess a chain person's mailbox on the corporate domain")
    chains.add_argument("--guess-chains", dest="permutations_for_chains", action="store_true",
                        default=None,
                        help="also allow generic info@/contact@ guesses on chain domains (off by default)")

    live = run.add_argument_group("live lead table")
    live.add_argument("--no-supabase", dest="supabase", action="store_false", default=None,
                      help="don't mirror this run into Supabase (on by default when keys are saved)")
    live.add_argument("--supabase", dest="supabase", action="store_true", default=None,
                      help=argparse.SUPPRESS)
    live.add_argument("--supabase-prefix", dest="supabase_prefix",
                      help="table name prefix (default gmscrape_)")

    # --- enrich ----------------------------------------------------------
    enrich = sub.add_parser(
        "enrich", help="run the email stages against a saved places file (no Maps API call)",
        parents=[common],
    )
    enrich.add_argument("--places-file", required=True, help="JSON/JSONL/CSV of places")
    enrich.add_argument("-y", "--yes", dest="confirm_keys_on_start", action="store_false",
                        default=None, help="start without the key check")
    enrich.add_argument("-o", "--out-dir", dest="out_dir")
    enrich.add_argument("--basename", default="enriched")
    enrich.add_argument("--format", dest="export_formats")
    enrich.add_argument("-n", "--limit", type=int, dest="results_per_query")
    enrich.add_argument("--no-verify", dest="verify_emails", action="store_false", default=None)
    enrich.add_argument("--no-permutations", dest="permutations", action="store_false", default=None)
    enrich.add_argument("--supabase", dest="supabase", action="store_true", default=None,
                        help="mirror leads into Supabase as the run progresses")

    # --- verify ----------------------------------------------------------
    verify_cmd = sub.add_parser("verify", help="verify addresses with the configured provider", parents=[common])
    verify_cmd.add_argument("emails", nargs="+")
    verify_cmd.add_argument("--verify-provider", choices=["auto", *list_verify_providers()],
                            dest="verify_provider")

    # --- extract ---------------------------------------------------------
    extract_cmd = sub.add_parser("extract", help="crawl one website and print the emails found", parents=[common])
    extract_cmd.add_argument("url")
    extract_cmd.add_argument("--max-pages", type=int, dest="max_pages_per_site")
    extract_cmd.add_argument("--no-robots", dest="obey_robots", action="store_false", default=None)
    extract_cmd.add_argument("--no-cache", dest="cache_http", action="store_false", default=None)

    # --- guess -----------------------------------------------------------
    guess_cmd = sub.add_parser("guess", help="show the permutations for a domain", parents=[common])
    guess_cmd.add_argument("domain")
    guess_cmd.add_argument("--business-name", default="")
    guess_cmd.add_argument("--category", default="")
    guess_cmd.add_argument("--tier", type=int, choices=[1, 2, 3], dest="permutation_tier")
    guess_cmd.add_argument("--max-guesses", type=int, dest="permutation_max")
    guess_cmd.add_argument("--no-mx-check", dest="permutation_require_mx",
                           action="store_false", default=None)

    # --- probe-maps ------------------------------------------------------
    probe_cmd = sub.add_parser(
        "probe-maps", parents=[common],
        help="work out how an unknown maps API wants to be called, and write "
             "a ready GENERIC_MAPS_CONFIG for it",
    )
    probe_cmd.add_argument("endpoint",
                           help="endpoint URL, or just the host to try common paths")
    probe_cmd.add_argument("--key", help="API key (defaults to MAPS_API_KEY from .env)")
    probe_cmd.add_argument("--query", default="dentist in austin tx",
                           help="search string to probe with")
    probe_cmd.add_argument("--max-requests", type=int, default=12,
                           help="hard cap on probe requests (default 12)")
    probe_cmd.add_argument("--param", action="append", default=[], metavar="K=V",
                           help="extra query parameter to send, repeatable")
    probe_cmd.add_argument("--write", metavar="PATH",
                           help="write the discovered config to this file")
    probe_cmd.add_argument("--name", default="scrapertech",
                           help="provider name to record in the config")
    probe_cmd.add_argument("--show-sample", action="store_true",
                           help="print the full first result row")

    # --- MCP debugging ---------------------------------------------------
    mcp_cmd = sub.add_parser("probe-mcp", parents=[common],
                             help="list the tools an MCP server offers, and try the maps search")
    mcp_cmd.add_argument("url", nargs="?", help="MCP URL (default: saved MCP_MAPS_URL)")
    mcp_cmd.add_argument("--query", default="dentist in austin tx")
    mcp_cmd.add_argument("--call", action="store_true", help="also run one search and show the result")
    mcp_cmd.add_argument("--tool", help="tool to call (default: auto-picked)")
    mcp_cmd.add_argument("--raw", action="store_true", help="dump the raw result JSON")

    # --- web search debugging --------------------------------------------
    search_cmd = sub.add_parser("search", parents=[common],
                                help="run one web search and show what the API returned")
    search_cmd.add_argument("query")
    search_cmd.add_argument("--limit", type=int, default=10)
    search_cmd.add_argument("--raw", action="store_true", help="dump the raw JSON")

    owner_cmd = sub.add_parser("owner", parents=[common],
                               help="find a business's owner via web search and show the evidence")
    owner_cmd.add_argument("name")
    owner_cmd.add_argument("--city", default="")
    owner_cmd.add_argument("--min-confidence", type=int, dest="owner_min_confidence")

    # --- supabase --------------------------------------------------------
    init_cmd = sub.add_parser(
        "supabase-init", parents=[common],
        help="print (or write) the SQL that creates the Supabase tables",
    )
    init_cmd.add_argument("--write", metavar="PATH", help="write the SQL to this file")
    init_cmd.add_argument("--prefix", dest="supabase_prefix",
                          help="table name prefix (default gmscrape_)")

    sub.add_parser("supabase-check", parents=[common],
                   help="verify the Supabase URL, key and tables")

    # --- runs ------------------------------------------------------------
    resume_cmd = sub.add_parser("resume", parents=[common],
                                help="continue an interrupted run where it stopped")
    resume_cmd.add_argument("run_id", nargs="?", help="run id (default: latest unfinished)")
    resume_cmd.add_argument("-y", "--yes", dest="confirm_keys_on_start", action="store_false",
                            default=None)
    resume_cmd.add_argument("--no-supervise", dest="no_supervise", action="store_true",
                            help="run in this process without the restart-on-kill supervisor")
    resume_cmd.add_argument("-o", "--out-dir", dest="out_dir")
    resume_cmd.add_argument("--basename", default="leads")
    resume_cmd.add_argument("--format", dest="export_formats")
    runs_cmd = sub.add_parser("runs", parents=[common], help="list recent runs and their state")
    runs_cmd.add_argument("--limit", type=int, default=15)
    publish_cmd = sub.add_parser("publish", parents=[common],
                                 help="push a finished run to Supabase (for a run made while the live table was off)")
    publish_cmd.add_argument("run_id", nargs="?", help="run id (default: the latest finished run)")
    publish_cmd.add_argument("--table", dest="table", help="Supabase table name for the run")

    # --- keys ------------------------------------------------------------
    setup_cmd = sub.add_parser("setup", parents=[common],
                               help="save or change your API keys (interactive)")
    setup_cmd.add_argument("--only", choices=["maps", "verify", "search", "supabase"],
                           help="only walk through one group")
    keys_cmd = sub.add_parser("keys", parents=[common],
                              help="show saved keys (masked), or `keys set NAME=VALUE ...`")
    keys_cmd.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    keys_cmd.add_argument("pairs", nargs="*", metavar="NAME=VALUE",
                          help="with `set`: keys to save, e.g. MAILTESTER_KEY=sub_... ; NAME= clears")

    # --- misc ------------------------------------------------------------
    sub.add_parser("providers", help="list providers and show which are configured",
                   parents=[common])
    sub.add_parser("doctor", help="check configuration and connectivity",
                   parents=[common])
    stats_cmd = sub.add_parser("stats", help="summarize what is stored in the database", parents=[common])
    stats_cmd.add_argument("--json", dest="as_json", action="store_true")
    return parser


SETTINGS_KEYS = {
    "maps_provider", "verify_provider", "places_file", "results_per_query", "out_dir",
    "export_formats", "crawl_websites", "max_pages_per_site", "http_concurrency",
    "http_timeout", "obey_robots", "cache_http", "permutations", "permutation_tier",
    "permutation_max", "permutation_require_mx", "stop_on_first_valid", "verify_emails",
    "verify_budget", "verify_concurrency", "keep_invalid", "keep_risky", "min_confidence",
    "chain_mode", "permutations_for_chains", "db_path", "log_level",
    "discover_websites", "find_owners", "owner_search", "owner_min_confidence",
    "website_min_confidence", "require_verified_guesses", "supabase", "supabase_prefix",
    "chain_people", "chain_person_guesses", "chain_crawl_pages", "confirm_keys_on_start",
    "supabase_run_tables", "supabase_table_name", "batch_size", "maps_concurrency", "run_hours",
}


def settings_from_args(args: argparse.Namespace) -> Settings:
    load_env(getattr(args, "env_file", None))
    overrides = {
        key: value for key, value in vars(args).items()
        if key in SETTINGS_KEYS and value is not None
    }
    formats = overrides.get("export_formats")
    if isinstance(formats, str):
        overrides["export_formats"] = tuple(f.strip() for f in formats.split(",") if f.strip())
    return Settings.from_env(**overrides)


def configure_logging(level: str, log_file: Optional[str] = None) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if log_file:
        add_log_file(log_file)


def add_log_file(path: str) -> None:
    """Everything the terminal shows (and the debug lines it does not) also goes
    to a file, so a run that ends without a trace still leaves one."""
    import logging.handlers

    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=20_000_000, backupCount=3,
                                                       encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
        logging.getLogger(__name__).info("log file: %s", path)


def _raise_open_file_limit() -> None:
    """macOS shells default to 256 open files; a crawl with a few hundred
    sockets in flight would die with 'Too many open files'."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = min(hard if hard != resource.RLIM_INFINITY else 8192, 8192)
        if soft < wanted:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    except (ImportError, ValueError, OSError):
        pass


def _launch() -> None:
    _raise_open_file_limit()
    from .banner import print_banner

    print_banner(_console)


def cmd_setup(args: argparse.Namespace) -> int:
    from .keys import run_setup

    _launch()
    settings_from_args(args)             # loads .env + saved keys so current values show
    groups = (args.only,) if getattr(args, "only", None) else ("maps", "verify", "search", "supabase")
    run_setup(echo=lambda m: echo(m), groups=groups)
    _print_key_status(settings_from_args(args))
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    from .keys import KEY_FIELDS, current_values, mask, set_keys, user_config_path

    if getattr(args, "action", "show") == "set":
        pairs: dict[str, str] = {}
        for item in args.pairs:
            name, sep, value = item.partition("=")
            if not sep or not name.strip():
                echo(f"[red]expected NAME=VALUE, got {item!r}[/red]")
                return 2
            pairs[name.strip()] = value
        if not pairs:
            echo("[red]nothing to set - usage: gmscrape keys set NAME=VALUE ...[/red]")
            return 2
        try:
            saved, warnings = set_keys(pairs)
        except KeyError as exc:
            echo(f"[red]unknown key {exc.args[0]} - known names: "
                 f"{', '.join(f.env for f in KEY_FIELDS)}[/red]")
            return 2
        for name, value in saved.items():
            field = next(f for f in KEY_FIELDS if f.env == name)
            echo(f"  {name} = {(mask(value) if field.secret else value) if value else 'cleared'}")
        for warning in warnings:
            echo(f"  [yellow]warning: {warning}[/yellow]")
        echo(f"saved to {user_config_path()}")

    from .keys import check_value

    settings = settings_from_args(args)
    values = current_values()
    _print_table(
        f"Keys (saved in {user_config_path()})",
        ("key", "value", "used for"),
        [
            (f.env, (mask(values[f.env]) if f.secret else values[f.env] or "(not set)"), f.group)
            for f in KEY_FIELDS if values[f.env]
        ] or [("(none)", "", "run `gmscrape setup`")],
    )
    problems = [(f.env, check_value(f.env, values[f.env])) for f in KEY_FIELDS if values[f.env]]
    for env, warning in problems:
        if warning:
            echo(f"  [yellow]{env}: {warning}[/yellow]")
    _print_key_status(settings)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """Push a finished run's leads to Supabase after the fact - for a run that
    was scraped while the live table was off or broken."""
    from .store.sinks import STATUS_DONE

    settings = settings_from_args(args)
    settings.supabase = True
    with Store(settings.db_path) as store:
        runs = store.list_runs(500)
        if args.run_id:
            run = next((r for r in runs if r["run_id"] == args.run_id), None)
        else:
            run = next((r for r in runs if r["status"] == "done"), None) or (runs[0] if runs else None)
        if run is None:
            echo("[red]no such run - `gmscrape runs` lists them[/red]")
            return 2
        if args.table:
            settings.supabase_table_name = args.table
        elif run["run_table"]:
            settings.supabase_table_name = run["run_table"]
        if not settings.supabase_configured:
            echo("[red]Supabase is not set up - run `scraper keys set SUPABASE_URL=... SUPABASE_KEY=...`[/red]")
            return 2
        sinks = build_sinks(settings, run["queries"])
        if not sinks:
            return 1
        sink = sinks[0]
        sink.start_run(run["run_id"], {"queries": run["queries"]})
        sent = 0
        batch: list = []
        for result in store.iter_run_businesses(run["run_id"]):
            batch.append(result)
            if len(batch) >= 200:
                sink.upsert(batch, STATUS_DONE)
                sent += len(batch)
                batch = []
        if batch:
            sink.upsert(batch, STATUS_DONE)
            sent += len(batch)
        sink.finish_run(run["run_id"], {"businesses": sent, "republished": True})
        sink.close()
        run_table = getattr(sink, "run_table", "")
        if run_table:
            store.set_run_state(run["run_id"], run["status"], run_table=run_table)
    stats = getattr(sink, "stats", None)
    if stats is not None and stats.failures and not stats.leads_written:
        echo(f"[red]nothing was written: {stats.last_error}[/red]")
        return 1
    echo(f"published {sent} businesses from run [cyan]{run['run_id']}[/cyan]"
         + (f" to [green]{run_table}[/green]" if run_table else ""))
    if stats is not None:
        echo(f"  lead rows: {stats.leads_written}   email rows: {stats.emails_written}"
             + (f"   failures: {stats.failures} ({stats.last_error})" if stats.failures else ""))
    return 0


def _print_key_status(settings: Settings) -> None:
    from .keys import key_status

    status = key_status(settings)
    colour = lambda v, bad: f"[red]{v}[/red]" if v == bad else f"[green]{v}[/green]"  # noqa: E731
    echo(
        f"maps: {colour(status.maps, 'none')}   verification: {colour(status.verify, 'local')}"
        f"   web search: {colour(status.search, 'off')}   supabase: {status.supabase}"
    )


def _startup_key_check(settings: Settings, args: argparse.Namespace) -> Optional[Settings]:
    """Show the keys a run will use; offer to change them. Returns the settings
    to run with, or None if the user backed out."""
    from .keys import interactive, key_status, run_setup

    status = key_status(settings)
    if not status.any_configured:
        if not interactive():
            echo("[red]No Google Maps provider configured.[/red] Run `gmscrape setup` "
                 "or set a key in .env")
            return None
        echo("[yellow]No API keys saved yet - let's set them up.[/yellow]")
        run_setup(echo=lambda m: echo(m))
        settings = settings_from_args(args)
        if not key_status(settings).any_configured:
            echo("[red]Still no Google Maps provider - cannot run.[/red]")
            return None
        return settings

    _print_key_status(settings)
    if not settings.confirm_keys_on_start or not interactive():
        return settings
    try:
        answer = input("Press Enter to start, or type k to change keys: ").strip().lower()
    except EOFError:
        return settings
    if answer in ("k", "keys", "c", "change", "setup"):
        run_setup(echo=lambda m: echo(m))
        settings = settings_from_args(args)
        _print_key_status(settings)
    elif answer in ("q", "quit", "n", "no"):
        echo("cancelled")
        return None
    return settings


# --- the supervisor: a run that gets killed picks itself back up --------------
SUPERVISE_MAX_RESTARTS = 200
SUPERVISE_PAUSE = 15.0
SUPERVISE_FAST_FAIL = 60.0           # a child dying inside a minute, repeatedly, is not making progress
SUPERVISE_FAST_FAILS_MAX = 5
_CLEAN_EXITS = {0, 2, 130}          # done / refused to start / Ctrl-C: the person decides


def _supervised(args: argparse.Namespace) -> bool:
    """Only a real terminal session gets a supervisor; the child, tests and
    scripts run the pipeline directly."""
    if os.environ.get("GMSCRAPE_CHILD") or getattr(args, "no_supervise", False):
        return False
    argv = getattr(args, "_argv", None)
    if not argv:
        return False
    return _is_tty()


def _is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _common_argv(args: argparse.Namespace) -> list[str]:
    out: list[str] = []
    for flag, attr in (("--env-file", "env_file"), ("--db", "db_path"), ("--log-level", "log_level")):
        value = getattr(args, attr, None)
        if value:
            out += [flag, str(value)]
    return out


def _resume_argv(args: argparse.Namespace) -> list[str]:
    argv = ["resume", "-y"] + _common_argv(args)
    for flag, attr in (("-o", "out_dir"), ("--basename", "basename")):
        value = getattr(args, attr, None)
        if value:
            argv += [flag, str(value)]
    return argv


def _run_child(argv: list[str], console_path: Optional[str] = None) -> int:
    """Run the pipeline in a process of its own session - not a foreground job
    of the terminal, no controlling terminal, no prompts - writing what it would
    have printed to out/console.txt, which this process shows live. A kill
    aimed at the terminal-attached process leaves the worker running; Ctrl-C
    here is forwarded so the worker checkpoints and stops cleanly."""
    import signal
    import subprocess
    import time as _time

    env = {**os.environ, "GMSCRAPE_CHILD": "1"}
    if not any(a in ("-y", "--yes") for a in argv):
        argv = list(argv) + ["-y"]                       # nothing to ask once detached
    path = Path(console_path or "out/console.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as sink:
        offset = sink.tell()
        proc = subprocess.Popen(
            [sys.executable, "-m", "gmscrape.cli", *argv], env=env,
            stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    echo(f"[dim]worker pid {proc.pid} · output also in {path}[/dim]")
    with path.open("rb") as follow:
        follow.seek(offset)
        try:
            while True:
                chunk = follow.read()
                if chunk:
                    sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                code = proc.poll()
                if code is not None:
                    tail = follow.read()
                    if tail:
                        sys.stdout.write(tail.decode("utf-8", errors="replace"))
                        sys.stdout.flush()
                    return code
                _time.sleep(0.5)
        except KeyboardInterrupt:
            echo("\n[yellow]stopping the worker cleanly (it checkpoints first)…[/yellow]")
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=120)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                proc.kill()
            tail = follow.read()
            if tail:
                sys.stdout.write(tail.decode("utf-8", errors="replace"))
            return 130


def supervise(args: argparse.Namespace, run_child=_run_child, pause: float = SUPERVISE_PAUSE,
              console_path: Optional[str] = None) -> int:
    """Run the command in a detached worker and, if that worker is killed or
    dies with an error, resume it - up to SUPERVISE_MAX_RESTARTS times. A
    finished run, a refusal to start and Ctrl-C end the loop."""
    import time as _time

    argv = list(args._argv)
    out_dir = str(Path(console_path).parent) if console_path else ""
    fast_fails = 0
    for attempt in range(SUPERVISE_MAX_RESTARTS + 1):
        started = _time.monotonic()
        code = run_child(argv, console_path)
        if code in _CLEAN_EXITS:
            return code
        lasted = _time.monotonic() - started
        fast_fails = fast_fails + 1 if lasted < SUPERVISE_FAST_FAIL else 0
        if fast_fails >= SUPERVISE_FAST_FAILS_MAX:
            echo(f"[red]the run died within a minute {fast_fails} times in a row - something is stopping it "
                 "from starting at all. See out/scraper.log; `scraper resume` continues it once fixed.[/red]")
            return code
        if attempt == SUPERVISE_MAX_RESTARTS:
            echo(f"[red]the run stopped {attempt + 1} times; giving up. `scraper resume` continues it.[/red]")
            return code
        why = f"killed by signal {-code}" if code < 0 else f"exit code {code}"
        if code < 0:
            echo("[yellow]the computer stopped the worker.[/yellow] On a Mac this is the "
                 "\"Malicious Script Blocked\" notice: a website the scrape visited is on "
                 "Apple's unsafe list. That site is skipped from here on; the run continues.")
        blocked = unsafe.quarantine_leftovers(out_dir) if out_dir else []
        if blocked:
            echo(f"[dim]{len(blocked)} website{'s' if len(blocked) != 1 else ''} added to "
                 f"{Path(out_dir) / unsafe.BLOCKED_FILE}[/dim]")
        echo(f"[yellow]the run stopped ({why}). Nothing finished is lost; resuming in "
             f"{int(pause)}s (restart {attempt + 1}/{SUPERVISE_MAX_RESTARTS}).[/yellow]")
        logging.getLogger(__name__).warning("run stopped (%s); resuming", why)
        _time.sleep(pause)
        argv = _resume_argv(args)
    return code


def cmd_run(args: argparse.Namespace) -> int:
    supervised = _supervised(args)
    if not (supervised and getattr(args, "_launched", False)):
        _launch()
    settings = settings_from_args(args)
    queries: list[str] = list(args.queries or [])
    if args.queries_file:
        queries.extend(read_query_file(args.queries_file))
    if not queries:
        echo("[red]No queries given.[/red] Pass them as arguments or use -f queries.txt")
        echo('Example: gmscrape run "dentist in austin tx" "plumber in miami fl"')
        return 2

    specs = parse_queries(queries)
    echo(f"[bold]{len(specs)}[/bold] quer{'y' if len(specs) == 1 else 'ies'} to run:")
    for spec in specs[:12]:
        echo(f"  • [cyan]{spec.business_type}[/cyan] in [magenta]{spec.location or 'anywhere'}[/magenta]")
    if len(specs) > 12:
        echo(f"  … and {len(specs) - 12} more")

    checked = _startup_key_check(settings, args)
    if checked is None:
        return 2
    settings = checked
    if supervised:
        # Everything a person could be asked has been asked here, on the terminal;
        # the work itself runs detached and is resumed if it is killed.
        return supervise(args, console_path=str(Path(settings.out_dir) / "console.txt"))
    return _execute_run(settings, [s.search_string for s in specs], basename=args.basename,
                        run_id=None, resume_id=getattr(args, "resume", None))


def _skip_unsafe_sites(out_dir: str) -> list[str]:
    """Before a start: block the sites the previous worker was visiting when it
    was cut off (it leaves out/inflight.json behind only then)."""
    blocked = unsafe.quarantine_leftovers(out_dir)
    if blocked:
        shown = ", ".join(blocked[:5]) + (" …" if len(blocked) > 5 else "")
        echo(f"[yellow]the last run was cut off while visiting {len(blocked)} website"
             f"{'s' if len(blocked) != 1 else ''} ({shown}); they will be skipped from now on "
             f"- see {Path(out_dir) / unsafe.BLOCKED_FILE}[/yellow]")
    return blocked


def _execute_run(settings: Settings, queries: list[str], *, basename: str,
                 run_id: Optional[str], resume_id: Optional[str]) -> int:
    """Run (or resume) the pipeline, exporting after every batch and on any exit."""
    resume = False
    if resume_id:
        with Store(settings.db_path) as store:
            previous = (store.latest_unfinished_run() if resume_id == "latest"
                        else next((r for r in store.list_runs(200) if r["run_id"] == resume_id), None))
        if previous is None:
            echo("[yellow]nothing to resume[/yellow]" if resume_id == "latest"
                 else f"[yellow]no run {resume_id}[/yellow]")
            return 2
        run_id, queries, resume = previous["run_id"], previous["queries"], True
        if previous.get("run_table") and not settings.supabase_table_name:
            settings.supabase_table_name = previous["run_table"]     # same live table, not a new one
        echo(f"resuming run [cyan]{run_id}[/cyan]: {previous['done']}/{previous['total']} businesses "
             f"already done, {len(queries)} search{'es' if len(queries) != 1 else ''}")

    add_log_file(str(Path(settings.out_dir) / "scraper.log"))
    _skip_unsafe_sites(settings.out_dir)
    exporter = _Exporter(settings, basename)
    maps = None
    if resume:
        # The listings were cached by the original run; no provider is needed.
        from .providers.maps.file_provider import CacheOnlyMaps
        from .providers.registry import detect_maps_provider

        try:
            detect_maps_provider(settings)
        except ProviderError:
            maps = CacheOnlyMaps(settings)
    try:
        pipeline = Pipeline(settings, progress=_make_progress(exporter),
                            sinks=build_sinks(settings, queries), maps=maps)
    except ProviderError as exc:
        echo(f"[red]{exc}[/red]")
        return 2

    lean = settings.lean_memory or len(queries) > settings.lean_threshold_queries
    unsafe.open_inflight(settings.out_dir)      # which sites are on the wire, for a cut-off run
    try:
        with pipeline:
            echo(f"maps: [green]{pipeline.maps.name}[/green]   "
                 f"verification: [green]{pipeline.verifier.name}[/green]   "
                 f"web search: [green]{pipeline.web_search.name if pipeline.web_search else 'off'}[/green]   "
                 f"batches of {settings.batch_size}" + ("   [dim]large-run mode[/dim]" if lean else ""))
            if settings.verify_emails and getattr(pipeline.verifier, "requires_key", False):
                # Prove the verification key works before a single Maps credit is spent.
                try:
                    pipeline.verifier.preflight()
                except ProviderAuthError as exc:
                    echo(f"[red]{exc}[/red]")
                    echo("Nothing was spent. Fix the key and start again.")
                    return 2
                except ProviderError as exc:
                    echo(f"[yellow]could not check the verification key up front ({exc}); continuing[/yellow]")
            if lean:
                exporter.begin_lean(pipeline.store, run_id or "", resumed=resume)
            try:
                report = pipeline.run(queries, run_id=run_id, resume=resume)
            except RunStopped as stopped:
                report = stopped.report
                paths = exporter.finish(report)
                echo("")
                if isinstance(stopped.cause, KeyboardInterrupt):
                    echo(f"[yellow]Stopped.[/yellow] {report.done}/{report.total} businesses were "
                         f"finished and are in {paths[0] if paths else 'the export'}.")
                elif isinstance(stopped.cause, ProviderAuthError):
                    echo(f"[red]Stopped: {stopped.cause}[/red]")
                    echo(f"{report.done}/{report.total} businesses were finished and are in "
                         f"{paths[0] if paths else 'the export'}. Fix the key, then pick it up with:  "
                         f"[cyan]scraper resume[/cyan]   (run id {report.run_id})")
                    return 2
                else:
                    echo(f"[red]The run hit an error:[/red] {stopped.cause}")
                    echo(f"{report.done}/{report.total} businesses were finished and are in "
                         f"{paths[0] if paths else 'the export'}. Nothing already paid for will be "
                         "re-bought on resume.")
                echo(f"Pick it up where it stopped with:  [cyan]scraper resume[/cyan]   "
                     f"(run id {report.run_id})")
                return 130 if isinstance(stopped.cause, KeyboardInterrupt) else 1
            paths = exporter.finish(report, getattr(pipeline, "store", None))
    finally:
        unsafe.close_inflight()          # a clean exit leaves nothing to quarantine
    _print_report(report, paths)
    if getattr(pipeline, "safety", None) is not None and pipeline.safety.stats["checked"]:
        stats = pipeline.safety.stats
        echo(f"[dim]unsafe-site check: {stats['checked']} sites asked about, {stats['flagged']} skipped[/dim]")
    _print_final_table(report)
    _print_supabase_link(settings, report)
    return 0


class _Exporter:
    """Writes the exports after every batch so a partial CSV always exists.

    Small runs rewrite the full export each time (and get JSON/XLSX at the
    end). Large runs append rows per batch instead - the file is never
    rewritten, whatever the size."""

    def __init__(self, settings: Settings, basename: str) -> None:
        self.settings = settings
        self.basename = basename
        self.appender: Optional[CsvAppender] = None

    def begin_lean(self, store: Store, run_id: str, resumed: bool) -> None:
        self.appender = CsvAppender(self.settings.out_dir, self.basename, _today())
        self.appender.start(fresh=True)
        if resumed:
            # Rebuild what earlier attempts finished, streaming from the database.
            buffer: list = []
            for result in store.iter_run_businesses(run_id):
                buffer.append(result)
                if len(buffer) >= 500:
                    self.appender.append(buffer)
                    buffer = []
            self.appender.append(buffer)

    def export(self, results) -> list[Path]:
        return export_results(results, self.settings.out_dir, basename=self.basename,
                              formats=self.settings.export_formats, run_date=_today())

    def on_batch(self, data: dict) -> None:
        if self.appender is not None:
            self.appender.append(data.get("batch_results") or [])
        else:
            self.export(data["results"])

    def finish(self, report: RunReport, store: Optional[Store] = None) -> list[Path]:
        if self.appender is not None:
            if store is not None and report.guesses_checked:
                # The guess pass changed rows already written: rebuild from the database.
                self.appender.start(fresh=True)
                buffer: list = []
                for result in store.iter_run_businesses(report.run_id):
                    buffer.append(result)
                    if len(buffer) >= 500:
                        self.appender.append(buffer)
                        buffer = []
                self.appender.append(buffer)
            return self.appender.written()
        return self.export(report.results)


def cmd_resume(args: argparse.Namespace) -> int:
    _launch()
    settings = settings_from_args(args)
    if _supervised(args):
        return supervise(args, console_path=str(Path(settings.out_dir) / "console.txt"))
    return _execute_run(settings, [], basename=args.basename, run_id=None,
                        resume_id=args.run_id or "latest")


def cmd_runs(args: argparse.Namespace) -> int:
    from datetime import datetime

    settings = settings_from_args(args)
    with Store(settings.db_path) as store:
        runs = store.list_runs(args.limit)
    if not runs:
        echo("no runs yet")
        return 0
    _print_table(
        "Runs",
        ("run id", "started", "status", "done", "searches", "table"),
        [
            (r["run_id"], datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M"),
             r["status"], f"{r['done']}/{r['total']}" if r["total"] else "",
             (r["queries"][0] + (f" +{len(r['queries']) - 1}" if len(r["queries"]) > 1 else ""))[:40],
             r["run_table"])
            for r in runs
        ],
    )
    unfinished = next((r for r in runs if r["status"] in ("interrupted", "failed", "running")), None)
    if unfinished:
        echo("resume the latest unfinished one with [cyan]scraper resume[/cyan]")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    _launch()
    args.maps_provider = "file"
    settings = settings_from_args(args)
    settings.maps_provider = "file"
    settings.places_file = args.places_file
    if settings.confirm_keys_on_start:
        _print_key_status(settings)
    try:
        pipeline = Pipeline(settings, progress=_make_progress(),
                            sinks=build_sinks(settings, ["enrich " + Path(args.places_file).stem]))
    except ProviderError as exc:
        echo(f"[red]{exc}[/red]")
        return 2
    with pipeline:
        report = pipeline.run(["*"])
        paths = export_results(
            report.results, settings.out_dir,
            basename=args.basename, formats=settings.export_formats,
            run_date=_today(),
        )
    _print_report(report, paths)
    _print_final_table(report)
    _print_supabase_link(settings, report)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    with Store(settings.db_path) as store:
        verifier = get_verifier(settings)
        echo(f"provider: [green]{verifier.name}[/green]")
        rows = []
        for email in args.emails:
            email = email.strip().lower()
            cached = store.get_verification(email)
            try:
                result = cached or verifier.verify(email)
            except ProviderAuthError as exc:
                echo(f"[red]{exc}[/red]")
                verifier.close()
                return 2
            if cached is None:
                store.put_verification(email, result)
            rows.append((
                email, result.status,
                result.sub_status or ("cached" if cached else ""),
                "" if result.score is None else str(result.score),
                "yes" if result.is_catch_all else "",
                result.error[:40],
            ))
        verifier.close()
    _print_table(
        "Verification", ("email", "status", "detail", "score", "catch-all", "error"), rows
    )
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    from .web.crawl import scrape_site
    from .web.fetch import Fetcher

    async def go() -> None:
        with Store(settings.db_path) as store:
            async with Fetcher(settings, cache=store) as fetcher:
                scrape = await scrape_site(fetcher, args.url, settings)
        echo(f"status: [green]{scrape.status}[/green]   pages: {len(scrape.pages)}")
        for page in scrape.pages:
            echo(f"  [dim]{page}[/dim]")
        if scrape.errors:
            for error in scrape.errors[:5]:
                echo(f"  [yellow]{error}[/yellow]")
        rows = [
            (
                c.email, c.source,
                "role" if c.is_role else ("personal" if c.is_personal_domain else ""),
                "yes" if c.on_business_domain else "",
                c.context[:48],
            )
            for c in sorted(scrape.candidates, key=lambda c: c.email)
        ]
        if rows:
            _print_table("Emails found", ("email", "source", "kind", "on-domain", "context"), rows)
        else:
            echo("[yellow]No emails found on this site.[/yellow]")

    asyncio.run(go())
    return 0


def cmd_guess(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    plan = build_permutations(
        args.domain,
        business_name=args.business_name,
        category=args.category,
        tier=settings.permutation_tier,
        max_candidates=settings.permutation_max,
        require_mx=settings.permutation_require_mx,
    )
    if not plan.allowed:
        echo(f"[yellow]No guesses for {plan.domain}: {plan.skipped_reason}[/yellow]")
        return 0
    _print_table(
        f"Permutations for {plan.domain}",
        ("email", "pattern"),
        [(c.email, c.pattern) for c in plan.candidates],
    )
    return 0


def cmd_probe_maps(args: argparse.Namespace) -> int:
    from .probe import (
        build_generic_config,
        describe_mapping,
        probe_maps_api,
        redact,
    )

    settings = settings_from_args(args)
    api_key = args.key or settings.maps_api_key
    if not api_key:
        echo("[red]No API key.[/red] Pass --key or set MAPS_API_KEY in .env")
        return 2

    extra: dict[str, str] = {}
    for item in args.param:
        if "=" not in item:
            echo(f"[red]--param must look like key=value, got {item!r}[/red]")
            return 2
        key, value = item.split("=", 1)
        extra[key] = value

    echo(f"probing [cyan]{args.endpoint}[/cyan] with query [magenta]{args.query!r}[/magenta]")
    echo("[dim]each request may consume an API credit; "
         f"capped at {args.max_requests}[/dim]")

    winner, attempts = probe_maps_api(
        args.endpoint, api_key, args.query,
        max_requests=args.max_requests, extra_params=extra,
    )

    _print_table(
        f"Probe attempts ({len(attempts)} request{'s' if len(attempts) != 1 else ''})",
        ("url", "auth", "search param", "status", "rows", "detail"),
        [
            (
                redact(a.url, api_key).replace("https://", ""),
                a.auth_style,
                a.query_param,
                str(a.status or "-"),
                str(a.rows) if a.rows else "",
                redact(a.error or a.body_snippet, api_key)[:60],
            )
            for a in attempts
        ],
    )

    if winner is None:
        from .probe import looks_like_website

        if looks_like_website(attempts):
            echo(f"[yellow]{args.endpoint} is a website, not an API host[/yellow] - every reply "
                 "was an HTML page. Open the provider's docs or API playground, copy the request "
                 "URL from a code sample (it is usually on a different host, e.g. api.<domain>), "
                 "and re-run probe-maps with that URL.")
            return 1
        echo("[yellow]No request shape returned business listings.[/yellow]")
        echo("The status codes above usually say why:")
        echo("  • 401/403 everywhere → the key is not being accepted in any of "
             "the styles tried; check how the docs pass it")
        echo("  • 400/422 → auth worked, but a required parameter is missing; "
             "add it with --param key=value and re-run")
        echo("  • 404 on every path → pass the exact endpoint URL instead of the host")
        return 1

    echo(f"\n[green]✓ found a working shape[/green] after {len(attempts)} request(s)")
    echo(f"  endpoint     [cyan]{winner.url}[/cyan]")
    echo(f"  auth         {winner.auth_style}")
    echo(f"  search param {winner.query_param}")
    echo(f"  listings at  {winner.results_path or '(top-level array)'} "
         f"({winner.rows} row{'s' if winner.rows != 1 else ''})")

    resolved, unresolved = describe_mapping(winner.sample)
    _print_table(
        "Fields recognized in the first listing",
        ("place field", "value"),
        list(resolved.items()),
    )
    if unresolved:
        echo(f"[dim]not found: {', '.join(unresolved)}[/dim]")
        echo(f"[dim]row keys: {', '.join(sorted(winner.sample)[:24])}[/dim]")
    if args.show_sample:
        echo(json.dumps(winner.sample, indent=2, default=str)[:4000])

    config = build_generic_config(winner, name=args.name)
    rendered = json.dumps(config, indent=2)
    if args.write:
        path = Path(args.write)
        path.write_text(rendered + "\n", encoding="utf-8")
        echo(f"\n[bold]Wrote[/bold] [green]{path}[/green]. Use it with:")
        echo(f"  GENERIC_MAPS_CONFIG={path}")
        echo("  MAPS_API_KEY=<your key>")
        echo(f"\nThen: [cyan]gmscrape run \"{args.query}\" -n 5[/cyan]")
    else:
        echo("\n[bold]Config for GENERIC_MAPS_CONFIG:[/bold]")
        echo(rendered)
        echo("[dim]re-run with --write scrapertech_maps.json to save it[/dim]")
    return 0


def supabase_config(settings: Settings):
    """The Supabase connection - from the access token alone when that is all
    we have (the project URL and service key are looked up, then remembered)."""
    from .keys import save_keys
    from .store.supabase import SupabaseConfig, SupabaseError, resolve_from_token

    if settings.supabase_url and settings.supabase_key:
        config = SupabaseConfig(
            url=settings.supabase_url, key=settings.supabase_key,
            schema=settings.supabase_schema, prefix=settings.supabase_prefix,
            access_token=settings.supabase_access_token,
        )
        problems = config.problems()
        if not problems:
            return config
        if not settings.supabase_access_token:
            raise SupabaseError(problems[0])
        # The saved key is unusable (a masked copy from the dashboard, say) but
        # the account token can fetch the real one - do that and repair the file.
        echo(f"[yellow]Supabase: {problems[0]}[/yellow]")
        echo("[yellow]fetching the project key with the access token instead[/yellow]")
    config = resolve_from_token(settings.supabase_access_token, settings.supabase_project_ref)
    config.schema = settings.supabase_schema
    config.prefix = settings.supabase_prefix
    settings.supabase_url, settings.supabase_key = config.url, config.key
    try:
        save_keys({"SUPABASE_URL": config.url, "SUPABASE_KEY": config.key,
                   "SUPABASE_PROJECT_REF": config.project_ref})
    except OSError:
        pass
    return config


def build_sinks(settings: Settings, queries: Sequence[str] = ()) -> list:
    """The live sinks a run should publish to. Supabase is on whenever an
    access token (or URL + key) is saved; the shared tables are created on
    first use and a fresh per-run table for this run."""
    if not settings.supabase or not settings.supabase_configured:
        return []
    from .store.supabase import (
        SupabaseError, SupabaseSink, ensure_schema, run_label, run_table_name,
    )

    try:
        config = supabase_config(settings)
        run_table = ""
        if settings.supabase_run_tables and (config.access_token or config.key):
            run_table = (run_table_name(settings.supabase_table_name)
                         if settings.supabase_table_name
                         else run_table_name(run_label(list(queries))))
        ready, detail = ensure_schema(config, run_table=run_table)
    except SupabaseError as exc:
        echo(f"[yellow]Supabase: {exc}[/yellow]")
        echo("[yellow]continuing without the live table[/yellow]")
        return []
    if not ready:
        echo(f"[yellow]Supabase: {detail}[/yellow]")
        echo("[yellow]continuing without the live table[/yellow]")
        return []
    if detail != "tables present":
        echo(f"Supabase: [green]{detail}[/green]")
    if run_table and "paste" in detail:            # the per-run table could not be created
        run_table = ""
    echo(f"live table: [green]{run_table or config.table('latest')}[/green]  "
         f"{config.table_editor_url or settings.supabase_url}")
    return [SupabaseSink(config, run_table=run_table)]


def cmd_supabase_init(args: argparse.Namespace) -> int:
    from .store.supabase import bootstrap_sql

    settings = settings_from_args(args)
    sql = bootstrap_sql(settings.supabase_prefix)
    if args.write:
        path = Path(args.write)
        path.write_text(sql, encoding="utf-8")
        echo(f"[bold]Wrote[/bold] [green]{path}[/green]")
        echo("Next (one time):")
        echo("  1. open your Supabase project → SQL Editor → paste the file → Run")
        echo("  2. [cyan]gmscrape supabase-check[/cyan]")
        echo("After that every run creates its own table with just the project API key.")
    else:
        print(sql)
    return 0


def cmd_supabase_check(args: argparse.Namespace) -> int:
    from .store.supabase import SupabaseError, check_connection

    settings = settings_from_args(args)
    if not settings.supabase_configured:
        echo("[red]No Supabase credentials saved.[/red] Run `gmscrape setup --only supabase` "
             "and paste the project URL and project API key from Project Settings → API")
        return 2
    from .store.supabase import ensure_schema

    try:
        config = supabase_config(settings)
        echo(f"project: [green]{config.project_ref or config.url}[/green]")
        ready, detail = ensure_schema(config)
        if ready:
            tables = check_connection(config)
        else:
            echo(f"[yellow]{detail}[/yellow]")
            return 1
    except SupabaseError as exc:
        echo(f"[red]{exc}[/red]")
        return 1
    _print_table("Supabase", ("table", "status"), list(tables.items()))
    if detail != "tables present":
        echo(f"[green]{detail}[/green]")
    from .store.supabase import runner_installed

    if config.key and runner_installed(config):
        echo("[green]✓[/green] per-run tables: on (runner installed)")
    elif config.access_token:
        echo("[green]✓[/green] per-run tables: on (via access token)")
    else:
        echo("[yellow]per-run tables: off[/yellow] - paste `gmscrape supabase-init` once "
             f"into {config.sql_editor_url or 'the SQL editor'} to turn them on")
    echo(f"[green]✓[/green] ready — every run streams into "
         f"[bold]{config.table('table')}[/bold]  ({config.table_editor_url or settings.supabase_url})")
    return 0


def cmd_probe_mcp(args: argparse.Namespace) -> int:
    from .probe import describe_mapping, find_result_rows
    from .providers.maps.mcp_provider import build_arguments, pick_maps_tool
    from .providers.mcp import MCPClient, MCPError
    from .query import parse_query

    settings = settings_from_args(args)
    url = args.url or settings.mcp_maps_url
    if not url:
        echo("[red]No MCP URL.[/red] Pass it, or save it with `gmscrape setup --only maps`")
        return 2
    client = MCPClient(url)
    try:
        info = client.initialize()
        tools = client.list_tools()
    except MCPError as exc:
        echo(f"[red]{exc}[/red]")
        return 1
    echo(f"server: [green]{info.get('name', '?')} {info.get('version', '')}[/green]   "
         f"{len(tools)} tool{'s' if len(tools) != 1 else ''}")
    picked = pick_maps_tool(tools, args.tool or settings.mcp_maps_tool)
    _print_table(
        "Tools",
        ("tool", "parameters", "description"),
        [
            (("→ " if picked and t.name == picked.name else "  ") + t.name,
             ", ".join(f"{p}{'*' if p in t.required else ''}" for p in t.properties)[:60],
             t.description[:70])
            for t in tools
        ],
    )
    if picked is None:
        echo("[yellow]no tool looks like a maps search - pass --tool <name>[/yellow]")
        return 1
    spec = parse_query(args.query)
    arguments = build_arguments(picked, spec, 5, 1)
    echo(f"\nwould call [cyan]{picked.name}[/cyan] with {json.dumps(arguments)}")
    if not args.call:
        echo("[dim]add --call to run it (uses one credit) and see the listings[/dim]")
        return 0
    try:
        payload = client.call_tool(picked.name, arguments)
    except MCPError as exc:
        echo(f"[red]{exc}[/red]")
        return 1
    finally:
        client.close()
    if args.raw:
        print(json.dumps(payload, indent=2, default=str)[:20000])
        return 0
    path, rows = find_result_rows(payload)
    if not rows:
        echo("[yellow]no listings found in the result[/yellow] - run with --raw to see it")
        return 1
    echo(f"[green]✓ {len(rows)} listings[/green] at {path or '(top level)'}")
    resolved, unresolved = describe_mapping(rows[0])
    _print_table("Fields recognized in the first listing", ("place field", "value"), list(resolved.items()))
    if unresolved:
        echo(f"[dim]not found: {', '.join(unresolved)}   row keys: {', '.join(sorted(rows[0])[:24])}[/dim]")
    echo("\nSave the link with [cyan]gmscrape setup --only maps[/cyan] (or `scraper buddy`) and you're set.")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .providers import get_web_search

    settings = settings_from_args(args)
    provider = get_web_search(settings)
    if provider is None:
        echo("[red]No web search provider configured.[/red] Set OPENWEBNINJA_KEY in .env")
        return 2
    response = provider.search(args.query, limit=args.limit)
    provider.close()
    if response.error:
        echo(f"[red]search failed:[/red] {response.error}")
        return 1
    if args.raw:
        print(json.dumps(response.raw, indent=2)[:20000])
        return 0
    echo(f"provider: [green]{provider.name}[/green]   "
         f"top-level keys: {', '.join(list(response.raw)[:12])}")
    if response.ai_overview:
        echo(f"\n[bold]AI overview:[/bold] {response.ai_overview[:700]}")
    if response.answer:
        echo(f"\n[bold]Answer box:[/bold] {response.answer[:400]}")
    if response.knowledge:
        echo("\n[bold]Knowledge panel:[/bold]")
        for key, value in list(response.knowledge.items())[:12]:
            echo(f"  {key}: {str(value)[:100]}")
    if response.hits:
        _print_table(
            f"Organic results ({len(response.hits)})",
            ("#", "domain", "title", "snippet"),
            [(str(h.position), h.domain, h.title[:50], h.snippet[:70]) for h in response.hits],
        )
    else:
        echo("[yellow]no organic results parsed[/yellow] - run with --raw to see the shape")
    return 0


def cmd_owner(args: argparse.Namespace) -> int:
    from .emails.people import choose_owner, owner_candidates_from_search, owner_local_parts
    from .providers import get_web_search

    settings = settings_from_args(args)
    provider = get_web_search(settings)
    if provider is None:
        echo("[red]No web search provider configured.[/red] Set OPENWEBNINJA_KEY in .env")
        return 2
    query = f"who is the owner of {args.name}" + (f" in {args.city}" if args.city else "")
    echo(f"searching: [cyan]{query}[/cyan]")
    response = provider.search(query, limit=10)
    provider.close()
    if response.error:
        echo(f"[red]search failed:[/red] {response.error}")
        return 1
    candidates = owner_candidates_from_search(response.all_text_blocks(), args.name, args.city)
    if candidates:
        _print_table(
            "Owner mentions (only where the business is named)",
            ("name", "title", "rank", "source", "evidence"),
            [(c.name, c.title, str(c.rank), c.source, c.evidence[:70]) for c in candidates],
        )
    else:
        echo("[yellow]no ownership statements mention this business[/yellow]")
    person = choose_owner(candidates, min_confidence=settings.owner_min_confidence)
    if person is None:
        echo("[yellow]→ no confident single owner; nothing would be guessed[/yellow]")
        return 0
    echo(f"\n[green]→ {person.name}[/green] ({person.title}, {person.confidence}% via {person.source})")
    echo(f"  would try: {', '.join(l + '@<domain>' for l in owner_local_parts(person)[:6])}")
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    maps_keys = settings.configured_maps_keys()
    verify_keys = settings.configured_verify_keys()
    try:
        auto_maps = detect_maps_provider(settings)
    except ProviderError:
        auto_maps = "none configured"
    auto_verify = detect_verify_provider(settings)

    _print_table(
        "Google Maps providers",
        ("provider", "configured", "auto-selected"),
        [
            (name, "yes" if maps_keys.get(name) else "no",
             "<--" if name == auto_maps else "")
            for name in list_maps_providers()
        ],
    )
    _print_table(
        "Email verification providers",
        ("provider", "configured", "auto-selected"),
        [
            (name, "yes" if (verify_keys.get(name) or name == "local") else "no",
             "<--" if name == auto_verify else "")
            for name in list_verify_providers()
        ],
    )
    _print_table(
        "Web search (website discovery + owner lookup)",
        ("provider", "configured", "auto-selected"),
        [("openwebninja", "yes" if settings.openwebninja_key else "no",
          "<--" if settings.web_search_configured else "")],
    )
    if not settings.web_search_configured:
        echo("[dim]no OPENWEBNINJA_KEY - website discovery and owner search are off[/dim]")
    if auto_maps == "none configured":
        echo("\n[yellow]No Maps provider configured yet.[/yellow] Set a key in .env, or "
             "describe your API in a JSON file and point GENERIC_MAPS_CONFIG at it "
             "(see examples/maps_api.example.json).")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    checks: list[tuple[str, str, str]] = []

    try:
        maps = get_maps_provider(settings)
        checks.append(("maps provider", "ok", maps.name))
        maps.close()
    except ProviderError as exc:
        checks.append(("maps provider", "FAIL", str(exc)[:110]))

    try:
        verifier = get_verifier(settings)
        checks.append(("verification provider", "ok", verifier.name))
        verifier.close()
    except ProviderError as exc:
        checks.append(("verification provider", "FAIL", str(exc)[:110]))

    try:
        with Store(settings.db_path) as store:
            checks.append(("database", "ok", f"{settings.db_path} {store.stats()}"))
    except Exception as exc:
        checks.append(("database", "FAIL", str(exc)[:110]))

    try:
        from .util import domain_has_mx
        checks.append(("dns/mx lookup", "ok" if domain_has_mx("gmail.com") else "FAIL", "gmail.com"))
    except Exception as exc:
        checks.append(("dns/mx lookup", "FAIL", str(exc)[:110]))

    try:
        import httpx
        response = httpx.get("https://example.com", timeout=15.0,
                             headers={"User-Agent": settings.user_agent})
        checks.append(("outbound https", "ok" if response.status_code < 400 else "FAIL",
                       f"example.com -> {response.status_code}"))
    except Exception as exc:
        checks.append(("outbound https", "FAIL", str(exc)[:110]))

    _print_table("Doctor", ("check", "result", "detail"), checks)
    return 0 if all(row[1] == "ok" for row in checks) else 1


def cmd_stats(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    with Store(settings.db_path) as store:
        stats = store.stats()
    if getattr(args, "as_json", False):
        print(json.dumps(stats, indent=2))
    else:
        _print_table("Database", ("metric", "value"), [(k, str(v)) for k, v in stats.items()])
    return 0


# --- presentation ----------------------------------------------------------
def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _make_progress(exporter: Optional["_Exporter"] = None):
    state = {"sites": 0}

    def hook(event: str, data: dict) -> None:
        if event == "query_done":
            if data["found"]:
                echo(f"  [green]✓[/green] {data['query']}: {data['found']} businesses")
            else:
                echo(f"  [yellow]○[/yellow] {data['query']}: no businesses returned")
        elif event == "classified":
            echo(f"  [bold]{data['businesses']}[/bold] unique businesses to process")
        elif event == "site_done":
            state["sites"] += 1
            if state["sites"] % 25 == 0 or data["done"] == data["total"]:
                echo(f"  crawled {data['done']}/{data['total']} websites")
        elif event == "resumed":
            echo(f"  restored {data['businesses']} finished businesses, {data['queries']} finished searches")
        elif event == "guess_pass_start":
            budget = (f", {_fmt_duration(data['time_left'])} of the time budget left"
                      if data["time_left"] != float("inf") else "")
            echo(f"  [bold]guess pass:[/bold] checking owner / info@ mailboxes for "
                 f"{data['businesses']:,} businesses{budget}")
        elif event == "guess_pass":
            if data["done"] % 500 == 0 or data["done"] == data["total"]:
                eta = f" · ~{_fmt_duration(data['eta'])} left" if data["eta"] else ""
                echo(f"  guesses {data['done']:,}/{data['total']:,} · {_fmt_duration(data['elapsed'])} elapsed{eta}")
        elif event == "guess_pass_cut":
            echo(f"  [yellow]time budget used up:[/yellow] {data['left']:,} businesses still have unchecked "
                 f"guesses (found addresses are all done). `scraper resume` continues them.")
        elif event == "batch_done":
            if not data.get("eta_settled", True):
                eta = " · estimating pace…"
            elif data["eta"]:
                eta = f" · ~{_fmt_duration(data['eta'])} left"
            else:
                eta = ""
            echo(f"  [bold]checkpoint[/bold] {data['done']:,}/~{data['total']:,} businesses · "
                 f"searches {data['queries_done']}/{data['queries']} · "
                 f"{int(data['rate_per_hour']):,}/h · {_fmt_duration(data['elapsed'])} elapsed{eta}")
            state["batches"] = state.get("batches", 0) + 1
            stages = data.get("stages") or {}
            if stages and state["batches"] % 5 == 0:
                n = state["batches"]
                gap = f", {stages['check_gap']:.2f}s apart" if stages.get("check_gap") else ""
                keys = f" on {stages['keys']} key{'s' if stages['keys'] != 1 else ''}" if stages.get("checks") else ""
                echo(f"  [dim]pace per batch: crawl+search {_fmt_duration(stages['crawl'] / n)} "
                     f"(pages {stages['fetch_avg']:.1f}s avg, searches {stages['search_avg']:.1f}s avg, "
                     f"{stages.get('search_slots', 0)} at a time) · "
                     f"verify {_fmt_duration(stages['verify'] / n)} ({stages.get('checks', 0) // n} checks{gap}{keys}) · "
                     f"maps {_fmt_duration(stages['maps'])} total · memory {int(stages.get('memory_now_mb', 0)):,} MB "
                     f"(peak {int(stages.get('memory_mb', 0)):,}, limit {int(stages.get('memory_limit_mb', 0)):,})[/dim]")
                logging.getLogger(__name__).info("pace: %s", {k: round(v, 2) if isinstance(v, float) else v
                                                              for k, v in stages.items()})
                if stages.get("memory_mb", 0) > 3000 and not state.get("memory_warned"):
                    state["memory_warned"] = True
                    echo("  [yellow]memory is above 3 GB - if the Mac warns about application memory, "
                         "lower PREPARE_AHEAD (e.g. 4) and HTTP_CONCURRENCY (e.g. 128) and resume.[/yellow]")
            budget = data.get("time_left")
            if (data.get("eta_settled") and data["eta"] and budget not in (None, float("inf"))
                    and data["eta"] > budget and not state.get("budget_warned")):
                state["budget_warned"] = True
                echo(f"  [yellow]at this pace the found-address pass alone needs ~{_fmt_duration(data['eta'])}, "
                     f"more than the time budget; it will finish anyway, but guessing will be cut.[/yellow] "
                     "Look at the pace line: the slowest stage is what to speed up.")
            if exporter is not None:
                try:
                    exporter.on_batch(data)
                except Exception as exc:  # noqa: BLE001 - never let an export break a run
                    echo(f"  [yellow]partial export failed: {exc}[/yellow]")
    return hook


def _print_table(title: str, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    if _console is None:
        print(f"\n== {title} ==")
        print(" | ".join(columns))
        for row in rows:
            print(" | ".join(str(cell) for cell in row))
        return
    table = Table(title=title, title_style="bold", header_style="bold cyan", show_lines=False)
    for column in columns:
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(*[str(cell) for cell in row])
    _console.print(table)


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


MAX_TERMINAL_ROWS = 200


def _print_final_table(report: RunReport) -> None:
    """The finished leads, as they appear in the CSV."""
    from .store.export import clean_rows

    rows = [row for r in report.results for row in clean_rows(r, _today())]
    if not rows:
        return
    shown = rows[-MAX_TERMINAL_ROWS:] if report.lean else rows[:MAX_TERMINAL_ROWS]
    total_rows = report.counters.get("lead_rows", len(rows))
    _print_table(
        f"Leads ({total_rows} row{'s' if total_rows != 1 else ''}"
        + (f", last {len(shown)} shown" if report.lean else "") + ")",
        ("Company", "City", "State", "Phone", "Verified Email", "First", "Last",
         "Title", "Business Type"),
        [
            (
                r["company_name"], r["city"], r["state"], r["phone_number"],
                r["verified_email"] or (f"[dim]{r['email']}[/dim]" if r["email"] else ""),
                r["contact_first_name"], r["contact_last_name"], r["contact_title"],
                r["business_type"],
            )
            for r in shown
        ],
    )
    if total_rows > len(shown):
        echo(f"[dim]… {total_rows - len(shown)} more rows in the CSV[/dim]")


def _print_supabase_link(settings: Settings, report: RunReport) -> None:
    if not report.sinks:
        return
    config = supabase_config(settings)
    sink = report.sinks[0]
    if getattr(sink, "stats", None) and sink.stats.failures and not sink.stats.leads_written:
        return
    echo("")
    echo("[bold]Live table:[/bold]")
    run_table = getattr(sink, "run_table", "")
    if run_table:
        echo(f"  [cyan]{run_table}[/cyan] - this run's table")
    if config.table_editor_url:
        echo(f"  {config.table_editor_url}")
    echo(f"  every run: [cyan]{config.table('table')}[/cyan]   "
         f"[dim]where run_id = '{report.run_id}'[/dim]")


def _print_report(report: RunReport, paths: Sequence[Path]) -> None:
    stats = report.stats()
    _print_table(
        "Run summary", ("metric", "value"), [(k, str(v)) for k, v in stats.items()]
    )
    if report.errors:
        echo("[yellow]Errors:[/yellow]")
        for error in report.errors[:10]:
            echo(f"  • {error}")
    if report.empty_queries:
        shown = report.empty_queries[:5]
        echo(f"[yellow]{len(report.empty_queries)} search(es) returned no businesses:[/yellow] "
             + "; ".join(shown) + (" …" if len(report.empty_queries) > 5 else ""))
        echo(f"  see the raw answer with:  [cyan]gmscrape probe-mcp --call --raw --query \"{shown[0]}\"[/cyan]")
    for sink in getattr(report, "sinks", []) or []:
        stats_obj = getattr(sink, "stats", None)
        if stats_obj is not None:
            detail = (f"{stats_obj.leads_written} leads, {stats_obj.emails_written} emails"
                      f" in {stats_obj.requests} request(s)")
            if stats_obj.failures:
                echo(f"[yellow]{sink.name}: {detail}, {stats_obj.failures} failed "
                     f"— {stats_obj.last_error}[/yellow]")
            else:
                echo(f"[green]{sink.name}:[/green] {detail}")
    if paths:
        echo("[bold]Exported:[/bold]")
        for path in paths:
            echo(f"  • [green]{path}[/green]")

    return


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args._argv = list(argv if argv is not None else sys.argv[1:])
    configure_logging(getattr(args, "log_level", None) or "INFO")
    handlers = {
        "run": cmd_run,
        "enrich": cmd_enrich,
        "verify": cmd_verify,
        "extract": cmd_extract,
        "guess": cmd_guess,
        "probe-maps": cmd_probe_maps,
        "probe-mcp": cmd_probe_mcp,
        "setup": cmd_setup,
        "resume": cmd_resume,
        "publish": cmd_publish,
        "runs": cmd_runs,
        "keys": cmd_keys,
        "search": cmd_search,
        "owner": cmd_owner,
        "supabase-init": cmd_supabase_init,
        "supabase-check": cmd_supabase_check,
        "providers": cmd_providers,
        "doctor": cmd_doctor,
        "stats": cmd_stats,
    }
    handler = handlers[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        echo("\n[yellow]interrupted[/yellow]")
        return 130
    except ProviderError as exc:
        echo(f"[red]provider error:[/red] {exc}")
        return 2
    except FileNotFoundError as exc:
        echo(f"[red]{exc}[/red]")
        return 2


if __name__ == "__main__":
    sys.exit(main())
