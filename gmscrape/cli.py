"""Command line interface.

    gmscrape run "dentist in austin tx" "plumber in miami fl"
    gmscrape run -f queries.txt --limit 60 --format all
    gmscrape enrich --places-file leads.csv
    gmscrape verify info@acme.com sales@acme.com
    gmscrape extract https://example.com
    gmscrape guess acme.com --business-name "Joe's Plumbing"
    gmscrape probe-maps https://api.example.com/maps --key KEY
    gmscrape supabase-init --write supabase_schema.sql
    gmscrape run "dentist in austin tx" --supabase
    gmscrape doctor
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .config import Settings, load_env
from .core.pipeline import Pipeline, RunReport
from .emails.patterns import build_permutations
from .providers import get_maps_provider, get_verifier, list_maps_providers, list_verify_providers
from .providers.base import ProviderError
from .providers.registry import detect_maps_provider, detect_verify_provider
from .query import parse_queries, read_query_file
from .store.db import Store
from .store.export import export_results

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

    guess = run.add_argument_group("permutations")
    guess.add_argument("--no-permutations", dest="permutations", action="store_false",
                       default=None, help="never guess addresses")
    guess.add_argument("--tier", type=int, choices=[1, 2, 3], dest="permutation_tier",
                       help="1=info/contact/hello 2=common (default) 3=aggressive")
    guess.add_argument("--max-guesses", type=int, dest="permutation_max",
                       help="cap guesses per domain (default 12)")
    guess.add_argument("--guess-all", dest="stop_on_first_valid", action="store_false",
                       default=None, help="verify every guess instead of stopping at the first hit")

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
    chains.add_argument("--guess-chains", dest="permutations_for_chains", action="store_true",
                        default=None, help="allow guessing on chain domains too")

    live = run.add_argument_group("live lead table")
    live.add_argument("--supabase", dest="supabase", action="store_true", default=None,
                      help="mirror leads into Supabase as the run progresses")
    live.add_argument("--supabase-prefix", dest="supabase_prefix",
                      help="table name prefix (default gmscrape_)")

    # --- enrich ----------------------------------------------------------
    enrich = sub.add_parser(
        "enrich", help="run the email stages against a saved places file (no Maps API call)",
        parents=[common],
    )
    enrich.add_argument("--places-file", required=True, help="JSON/JSONL/CSV of places")
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


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# --- commands --------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
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
    for spec in specs:
        echo(f"  • [cyan]{spec.business_type}[/cyan] in [magenta]{spec.location or 'anywhere'}[/magenta]")

    try:
        pipeline = Pipeline(settings, progress=_make_progress(), sinks=build_sinks(settings))
    except ProviderError as exc:
        echo(f"[red]{exc}[/red]")
        return 2

    with pipeline:
        echo(f"maps: [green]{pipeline.maps.name}[/green]   "
             f"verification: [green]{pipeline.verifier.name}[/green]")
        report = pipeline.run([s.search_string for s in specs])
        paths = export_results(
            report.results, settings.out_dir,
            basename=args.basename, formats=settings.export_formats,
        )
    _print_report(report, paths)
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    args.maps_provider = "file"
    settings = settings_from_args(args)
    settings.maps_provider = "file"
    settings.places_file = args.places_file
    try:
        pipeline = Pipeline(settings, progress=_make_progress(), sinks=build_sinks(settings))
    except ProviderError as exc:
        echo(f"[red]{exc}[/red]")
        return 2
    with pipeline:
        report = pipeline.run(["*"])
        paths = export_results(
            report.results, settings.out_dir,
            basename=args.basename, formats=settings.export_formats,
        )
    _print_report(report, paths)
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
            result = cached or verifier.verify(email)
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


def build_sinks(settings: Settings) -> list:
    """The live sinks a run should publish to, based on configuration."""
    if not settings.supabase:
        return []
    from .store.supabase import SupabaseConfig, SupabaseSink

    if not settings.supabase_configured:
        echo("[yellow]--supabase given but SUPABASE_URL / SUPABASE_KEY are not set;"
             " continuing without it.[/yellow]")
        return []
    config = SupabaseConfig(
        url=settings.supabase_url,
        key=settings.supabase_key,
        schema=settings.supabase_schema,
        prefix=settings.supabase_prefix,
    )
    echo(f"live table: [green]{settings.supabase_url}[/green] "
         f"({config.table('leads')})")
    return [SupabaseSink(config)]


def cmd_supabase_init(args: argparse.Namespace) -> int:
    from .store.supabase import schema_sql

    settings = settings_from_args(args)
    sql = schema_sql(settings.supabase_prefix)
    if args.write:
        path = Path(args.write)
        path.write_text(sql, encoding="utf-8")
        echo(f"[bold]Wrote[/bold] [green]{path}[/green]")
        echo("Next:")
        echo("  1. open your Supabase project → SQL Editor → paste the file → Run")
        echo("  2. put SUPABASE_URL and SUPABASE_KEY (service_role) in .env")
        echo("  3. [cyan]gmscrape supabase-check[/cyan]")
        echo('  4. [cyan]gmscrape run "dentist in austin tx" -n 5 --supabase[/cyan]')
    else:
        print(sql)
    return 0


def cmd_supabase_check(args: argparse.Namespace) -> int:
    from .store.supabase import SupabaseConfig, SupabaseError, check_connection

    settings = settings_from_args(args)
    if not settings.supabase_configured:
        echo("[red]SUPABASE_URL and SUPABASE_KEY are not both set.[/red]")
        echo("Add them to .env, then re-run. The service_role key is the one to use "
             "for writes from your own machine.")
        return 2
    config = SupabaseConfig(
        url=settings.supabase_url, key=settings.supabase_key,
        schema=settings.supabase_schema, prefix=settings.supabase_prefix,
    )
    try:
        tables = check_connection(config)
    except SupabaseError as exc:
        echo(f"[red]{exc}[/red]")
        return 1
    _print_table("Supabase", ("table", "status"), list(tables.items()))
    echo(f"[green]✓[/green] ready — run with [cyan]--supabase[/cyan] to stream leads into "
         f"[bold]{config.table('table')}[/bold]")
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
def _make_progress():
    state = {"sites": 0}

    def hook(event: str, data: dict) -> None:
        if event == "query_done":
            echo(f"  [green]✓[/green] {data['query']}: {data['found']} businesses")
        elif event == "classified":
            echo(f"  [bold]{data['businesses']}[/bold] unique businesses to process")
        elif event == "site_done":
            state["sites"] += 1
            if state["sites"] % 10 == 0 or data["done"] == data["total"]:
                echo(f"  crawled {data['done']}/{data['total']} websites")
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


def _print_report(report: RunReport, paths: Sequence[Path]) -> None:
    stats = report.stats()
    _print_table(
        "Run summary", ("metric", "value"), [(k, str(v)) for k, v in stats.items()]
    )
    if report.errors:
        echo("[yellow]Errors:[/yellow]")
        for error in report.errors[:10]:
            echo(f"  • {error}")
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

    top = [r for r in report.results if r.best_email][:10]
    if top:
        _print_table(
            "Sample leads",
            ("business", "best email", "source", "status", "conf"),
            [
                (
                    r.place.name[:34],
                    r.best_email.email,
                    r.best_email.source,
                    r.best_email.status,
                    str(r.best_email.confidence),
                )
                for r in top
            ],
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(getattr(args, "log_level", None) or "INFO")
    handlers = {
        "run": cmd_run,
        "enrich": cmd_enrich,
        "verify": cmd_verify,
        "extract": cmd_extract,
        "guess": cmd_guess,
        "probe-maps": cmd_probe_maps,
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
