"""`scraper buddy` - the guided, non-technical way to run a scrape.

    $ scraper buddy

    [banner]
    API keys on file
      Scraper Tech (Google Maps) ....... a25e…ce8   keep it? [Y/n]
      MailTester Ninja (email check) ... sub_…ABC   keep it? [Y/n]
      OpenWeb Ninja (web search) ....... ak_f…y11   keep it? [Y/n]
      Supabase (live table) ............ not set    add it now? [y/N]

    Searches - paste them, one per line, then press Enter on an empty line.
    (Or type the path to a .txt / .csv file.)
      > dentist in austin tx
      > plumber in miami fl
      >

    2 searches, 40 businesses each. Start? [Y/n]

Everything after that is the normal pipeline: live table, terminal table,
out/leads.csv.
"""

from __future__ import annotations

import csv
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import keys as K
from .query import parse_queries

Prompt = Callable[[str], str]
Echo = Callable[[str], None]


@dataclass(frozen=True)
class BuddyKey:
    label: str            # friendly name shown to the person
    env: str              # where it is stored
    purpose: str
    optional: bool = False
    secret: bool = True


# Only the keys this product actually uses, in the order they matter.
BUDDY_KEYS: tuple[BuddyKey, ...] = (
    BuddyKey("Scraper Tech (Google Maps)", "MAPS_API_KEY", "finds the businesses"),
    BuddyKey("MailTester Ninja (email verification)", "MAILTESTER_KEY", "confirms emails are real"),
    BuddyKey("OpenWeb Ninja (web search)", "OPENWEBNINJA_KEY",
             "finds missing websites and owners", optional=True),
    BuddyKey("Supabase URL (live table)", "SUPABASE_URL", "where the live table lives",
             optional=True, secret=False),
    BuddyKey("Supabase key (live table)", "SUPABASE_KEY", "service_role key", optional=True),
    BuddyKey("Supabase access token (creates tables)", "SUPABASE_ACCESS_TOKEN",
             "sbp_... token, optional", optional=True),
)


def _yes(answer: str, default: bool) -> bool:
    answer = (answer or "").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "yeah", "yep", "sure", "ok")


def review_keys(prompt: Prompt, echo: Echo) -> dict[str, str]:
    """Walk the keys: keep / replace / add. Returns what changed."""
    echo("")
    echo("API keys on file")
    updates: dict[str, str] = {}
    width = max(len(k.label) for k in BUDDY_KEYS) + 2
    for key in BUDDY_KEYS:
        current = os.getenv(key.env, "")
        shown = (K.mask(current) if key.secret else current) if current else "not set"
        line = f"  {key.label} ".ljust(width, ".") + f" {shown}"
        if current:
            answer = prompt(f"{line}   keep it? [Y/n] ")
            if _yes(answer, default=True):
                continue
            new = prompt(f"    paste the new {key.label} (Enter to keep, '-' to remove): ").strip()
        else:
            answer = prompt(f"{line}   add it now? [{'y/N' if key.optional else 'Y/n'}] ")
            if not _yes(answer, default=not key.optional):
                continue
            new = prompt(f"    paste your {key.label}: ").strip()
        if not new:
            continue
        if new == "-":
            updates[key.env] = ""
            os.environ.pop(key.env, None)
            echo("    removed")
            continue
        updates[key.env] = new
        os.environ[key.env] = new
        echo(f"    saved ({K.mask(new) if key.secret else new})")
    if updates:
        K.save_keys(updates)
    return updates


def _queries_from_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() in (".csv", ".tsv"):
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
        if not rows:
            return []
        header = [h.strip().lower() for h in rows[0]]
        column = 0
        for name in ("query", "search", "searches", "search query", "queries"):
            if name in header:
                column = header.index(name)
                rows = rows[1:]
                break
        else:
            # No header we recognise: if the first row looks like a heading, drop it.
            if rows and rows[0] and not any(sep in rows[0][0].lower() for sep in (" in ", " near ")):
                rows = rows[1:]
        return [r[column].strip() for r in rows if len(r) > column and r[column].strip()]
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def collect_queries(prompt: Prompt, echo: Echo) -> list[str]:
    """Paste searches line by line, or give a file path."""
    echo("")
    echo("Searches - paste them, one per line (business type in location),")
    echo("then press Enter on an empty line. Or type the path to a .txt / .csv file.")
    lines: list[str] = []
    while True:
        try:
            line = prompt("  > ")
        except EOFError:
            break
        line = line.strip()
        if not line:
            if lines:
                break
            continue
        candidate = Path(line.strip("'\"")).expanduser()
        if candidate.suffix.lower() in (".txt", ".csv", ".tsv") and candidate.exists():
            found = _queries_from_file(candidate)
            echo(f"    loaded {len(found)} search{'es' if len(found) != 1 else ''} from {candidate.name}")
            lines.extend(found)
            if lines:
                break
            continue
        lines.append(line)
    return [s.search_string for s in parse_queries(lines)]


def buddy(prompt: Prompt = input, echo: Echo = print, argv: Optional[list[str]] = None) -> int:
    """The whole guided flow."""
    from .banner import print_banner
    from .cli import _console, cmd_run, build_parser, echo as rich_echo, settings_from_args

    print_banner(_console)
    K.load_saved_keys_into_env()
    review_keys(prompt, rich_echo if echo is print else echo)

    if not os.getenv("MAPS_API_KEY") and not os.getenv("GENERIC_MAPS_CONFIG"):
        echo("")
        echo("No Google Maps key saved - the scrape cannot find businesses without one.")
        return 2
    if os.getenv("MAPS_API_KEY") and not os.getenv("GENERIC_MAPS_CONFIG"):
        echo("")
        echo("The Scraper Tech key is saved but its API address is not set up yet.")
        echo("Run once:  gmscrape probe-maps https://<api host from the docs> --write scrapertech_maps.json")
        echo("then:      gmscrape setup --only maps   (put that file under 'Custom maps API config file')")
        return 2

    queries = collect_queries(prompt, echo)
    if not queries:
        echo("No searches given - nothing to do.")
        return 2

    echo("")
    for query in queries[:12]:
        echo(f"  • {query}")
    if len(queries) > 12:
        echo(f"  … and {len(queries) - 12} more")
    per = prompt(f"\n{len(queries)} search{'es' if len(queries) != 1 else ''}. Businesses per search [40]: ").strip()
    try:
        limit = max(1, int(per)) if per else 40
    except ValueError:
        limit = 40
    if not _yes(prompt("Start? [Y/n] "), default=True):
        echo("cancelled")
        return 0

    args = build_parser().parse_args(["run", "-y", "-n", str(limit), *queries])
    settings_from_args(args)      # loads .env + saved keys for the run
    return cmd_run(args)


def main(argv: Optional[list[str]] = None) -> int:
    """`scraper` entry point: `scraper buddy` (or bare `scraper`) is the guided
    flow; anything else is passed through to the full gmscrape CLI."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("buddy", "start", "go"):
        try:
            return buddy()
        except KeyboardInterrupt:
            print("\ncancelled")
            return 130
    from .cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
