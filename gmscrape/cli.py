"""Command line interface.

    gmscrape run "dentist in austin tx" "plumber in miami fl"
    gmscrape run -f queries.txt --limit 60 --format all
    gmscrape enrich --places-file leads.csv
    gmscrape verify info@acme.com sales@acme.com
    gmscrape extract https://example.com
    gmscrape guess acme.com --business-name "Joe's Plumbing"
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
        pipeline = Pipeline(settings, progress=_make_progress())
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
        pipeline = Pipeline(settings, progress=_make_progress())
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
