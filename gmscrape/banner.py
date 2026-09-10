"""Launch banner: the Profit Systems mark with the SCRAPER BUDDY wordmark
beside it (or below it when the terminal is too narrow for both)."""

from __future__ import annotations

import shutil
import sys

# The mark, cropped from the full-size art and sampled every other row so it
# keeps its shape in terminal cells (which are about twice as tall as wide).
_MARK = (
    '                  :-=========:     -=========================-:',
    '              =%@@@@@@@@@=:   -%@@@@@@@@@@@@@@@@@@@@@@@@@#:',
    '         =%@@@@@@@@%+:   -#@@@@@@@@@*:  :-*@@@@@@@@@#-   :+%@@=',
    '    -%@@@@@@@@@=    :#@@@@@@@@@*:   :*@@@@@@@@@%:    =@@@@@@@@=',
    ':@@@@@@@@%+:   -*@@@@@@@@@*-   :+%@@@@@@@@#=    =%@@@@@@@@@@@@=',
    ':@@@@@@-  :#@@@@@@@@@#:   :+@@@@@@@@@%-    -@@@@@@@@@@= #@@@@@=',
    ':@@@@@@@@@@@@@@@#-   :+%@@@@@@@@%=:   =%@@@@@@@@%+:   -#@@@@@@=',
    ':@@@@@@@@@@%-    =%@@@@@@@@%=    -%@@@@@@@@@*:   :*@@@@@@@@@#:',
    ':@@@@@#-    =%@@@@@@@@%=:   -#@@@@@@@@@+:   :*@@@@@@@@@*-   :+-',
    ':#=   :=%@@@@@@@@%+    -#@@@@@@@@@*:   :*@@@@@@@@@#-   :=%@@@@=',
    '  =%@@@@@@@@@+:   -#@@@@@@@@@*:   :*@@@@@@@@@#-    +%@@@@@@@@@=',
    ':@@@@@@@*:   -*@@@@@@@@@#-   :+@@@@@@@@@#=   :=%@@@@@@@@@@@@@@=',
    ':@@@@@@-:*@@@@@@@@@#:    +@@@@@@@@@%-    =%@@@@@@@@@=:  #@@@@@=',
    ':@@@@@@@@@@@@@#-   :+%@@@@@@@@%=    =#@@@@@@@@%+:   -*@@@@@@@@=',
    ':@@@@@@@@%-    =@@@@@@@@@%=    -%@@@@@@@@@+    :#@@@@@@@@@*:',
    '-@@@%=   :=%@@@@@@@@%+:   -#@@@@@@@@@*:   -*@@@@@@@@@#-',
    '     =%@@@@@@@@@@@@@@@@@@@@@@@@@*:   :*@@@@@@@@@%-',
    ' -==========================:    :==========-',
)
_WORDMARK = (
    "███████╗ ██████╗██████╗  █████╗ ██████╗ ███████╗██████╗ ",
    "██╔════╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔════╝██╔══██╗",
    "███████╗██║     ██████╔╝███████║██████╔╝█████╗  ██████╔╝",
    "╚════██║██║     ██╔══██╗██╔══██║██╔═══╝ ██╔══╝  ██╔══██╗",
    "███████║╚██████╗██║  ██║██║  ██║██║     ███████╗██║  ██║",
    "╚══════╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝",
    "",
    "B U D D Y   2 . 0",
    "",
    "P R O F I T   S Y S T E M S",
    "P R O P R I E T A R Y   T E C H N O L O G Y",
    "2 0 2 6",
)
_GAP = 4
MARK_WIDTH = max(len(r) for r in _MARK)
WORD_WIDTH = max(len(r) for r in _WORDMARK)
WIDE = MARK_WIDTH + _GAP + WORD_WIDTH        # columns the side-by-side layout needs


def banner(columns: int | None = None) -> str:
    """Side by side when the terminal is wide enough, stacked otherwise."""
    if columns is None:
        columns = shutil.get_terminal_size((100, 40)).columns
    if columns >= WIDE + 2:
        return _side_by_side()
    return _stacked(min(columns, 100))


def _side_by_side() -> str:
    height = max(len(_MARK), len(_WORDMARK))
    top = (height - len(_WORDMARK)) // 2
    words = [""] * top + list(_WORDMARK)
    words += [""] * (height - len(words))
    rows = []
    for i in range(height):
        left = (_MARK[i] if i < len(_MARK) else "").ljust(MARK_WIDTH)
        rows.append((left + " " * _GAP + words[i]).rstrip())
    rows.append("")
    rows.append("─" * WIDE)
    return "\n".join(rows)


def _stacked(width: int) -> str:
    rows = [r.center(width).rstrip() for r in _MARK]
    rows.append("")
    rows += [r.center(width).rstrip() for r in _WORDMARK]
    rows.append("")
    rows.append("─" * width)
    return "\n".join(rows)


def print_banner(console=None) -> None:
    """Show the banner on launch - only on a real terminal, never into a pipe."""
    try:
        if not sys.stdout.isatty():
            return
    except (AttributeError, ValueError):
        return
    if console is not None:
        console.print(banner(console.width), style="bold cyan", markup=False, highlight=False)
    else:
        print(banner())
    print()
