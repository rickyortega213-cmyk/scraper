"""Launch banner."""

from __future__ import annotations

import sys

_LOGO = (
    "███████╗ ██████╗██████╗  █████╗ ██████╗ ███████╗██████╗ ",
    "██╔════╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔════╝██╔══██╗",
    "███████╗██║     ██████╔╝███████║██████╔╝█████╗  ██████╔╝",
    "╚════██║██║     ██╔══██╗██╔══██║██╔═══╝ ██╔══╝  ██╔══██╗",
    "███████║╚██████╗██║  ██║██║  ██║██║     ███████╗██║  ██║",
    "╚══════╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝",
)
_TAGLINES = (
    "",
    "B U D D Y  2 . 0",
    "",
    "P R O F I T   S Y S T E M S",
    "P R O P R I E T A R Y   T E C H N O L O G Y",
    "2 0 2 6",
    "",
)
WIDTH = 60


def banner() -> str:
    top = "╔" + "═" * WIDTH + "╗"
    bottom = "╚" + "═" * WIDTH + "╝"
    rows = [top, "║" + " " * WIDTH + "║"]
    rows += ["║" + line.center(WIDTH) + "║" for line in _LOGO]
    rows += ["║" + line.center(WIDTH) + "║" for line in _TAGLINES]
    rows.append(bottom)
    return "\n".join(rows)


def print_banner(console=None) -> None:
    """Show the banner on launch - only on a real terminal, never into a pipe."""
    try:
        if not sys.stdout.isatty():
            return
    except (AttributeError, ValueError):
        return
    if console is not None:
        console.print(banner(), style="bold cyan", markup=False, highlight=False)
    else:
        print(banner())
    print()
