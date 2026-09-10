"""Launch banner: the Profit Systems mark with the SCRAPER BUDDY wordmark
beside it - full-size on very wide terminals, half-size on normal ones, and
stacked (mark above, words below) when the window is narrow."""

from __future__ import annotations

import shutil
import sys

# The mark, cropped from the full-size art. Terminal cells are about twice as
# tall as wide, so every other row is kept; the small version also keeps every
# other column.
_MARK_LARGE = (
    '                      +++++++++++-       =+++++++++++++++++++++++++++++=:',
    '                .#@@@@@@@@@@@%.     +@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@*',
    '            *@@@@@@@@@@@@.     =@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@#      *@@-',
    '       *@@@@@@@@@@@@      -@@@@@@@@@@@@-     .@@@@@@@@@@@@*      *@@@@@@@-',
    '  #@@@@@@@@@@@@.     -@@@@@@@@@@@@=      @@@@@@@@@@@@*      *@@@@@@@@@@@@-',
    '@@@@@@@@@@:     =@@@@@@@@@@@@=     .@@@@@@@@@@@@*      *@@@@@@@@@@@@@@@@@-',
    '@@@@@@@#   -@@@@@@@@@@@@+     .#@@@@@@@@@@@#.     +@@@@@@@@@@@@-  @@@@@@@-',
    '@@@@@@@@@@@@@@@@@@@+     .#@@@@@@@@@@@#.     +@@@@@@@@@@@@-     -@@@@@@@@-',
    '@@@@@@@@@@@@@@*      *@@@@@@@@@@@%.     =@@@@@@@@@@@@-     :@@@@@@@@@@@@*',
    '@@@@@@@@@*      #@@@@@@@@@@@%.     =@@@@@@@@@@@@-     :@@@@@@@@@@@@*',
    '@@@@*      %@@@@@@@@@@@%      =@@@@@@@@@@@@-     :@@@@@@@@@@@@*      #@@@-',
    '      #@@@@@@@@@@@%.     =@@@@@@@@@@@@-     :@@@@@@@@@@@@*      *@@@@@@@@-',
    ' #@@@@@@@@@@@%:     +@@@@@@@@@@@@-     :@@@@@@@@@@@@*      #@@@@@@@@@@@@@-',
    '@@@@@@@@@:     =@@@@@@@@@@@@=     :%@@@@@@@@@@@*.     *@@@@@@@@@@@@@@@@@@-',
    '@@@@@@@#  =@@@@@@@@@@@@+     .%@@@@@@@@@@@#.     *@@@@@@@@@@@@-   @@@@@@@-',
    '@@@@@@@@@@@@@@@@@@+      #@@@@@@@@@@@%      =@@@@@@@@@@@@:     :@@@@@@@@@-',
    '@@@@@@@@@@@@@+      %@@@@@@@@@@@#      =@@@@@@@@@@@@-     .@@@@@@@@@@@@+',
    '@@@@@@@@+      %@@@@@@@@@@@%      +@@@@@@@@@@@@-     :@@@@@@@@@@@@*',
    '@@@+      #@@@@@@@@@@@@@#****%@@@@@@@@@@@@:     :@@@@@@@@@@@@+',
    '     #@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@=     :%@@@@@@@@@@@*',
    ' +**#####***********************-       ***#######*+',
)

_MARK_SMALL = (
    '           ++++++    +++++++++++++++:',
    '        .@@@@@@.  +@@@@@@@@@@@@@@@*',
    '      *@@@@@@   @@@@@@@@@@@@@@@@   *@',
    '    @@@@@@   -@@@@@@   @@@@@@*   @@@@',
    ' #@@@@@@   @@@@@@=   @@@@@@   *@@@@@@',
    '@@@@@:  =@@@@@@   @@@@@@*   @@@@@@@@@',
    '@@@@  @@@@@@+  .@@@@@@.  +@@@@@@ @@@@',
    '@@@@@@@@@@   #@@@@@#   @@@@@@-  -@@@@',
    '@@@@@@@*   @@@@@@.  =@@@@@@   @@@@@@*',
    '@@@@@   #@@@@@%   @@@@@@-  :@@@@@@',
    '@@*   @@@@@@   =@@@@@@   @@@@@@*   @@',
    '   #@@@@@%   @@@@@@-  :@@@@@@   *@@@@',
    ' @@@@@@:  +@@@@@@   @@@@@@*   @@@@@@@',
    '@@@@@   @@@@@@=  :@@@@@@.  *@@@@@@@@@',
    '@@@@ =@@@@@@   %@@@@@#   @@@@@@- @@@@',
    '@@@@@@@@@+   @@@@@@   =@@@@@@   @@@@@',
    '@@@@@@@   %@@@@@#   @@@@@@-  .@@@@@@',
    '@@@@+   @@@@@@   +@@@@@@   @@@@@@*',
    '@@   #@@@@@@#**@@@@@@:  :@@@@@@',
    '   @@@@@@@@@@@@@@@@   %@@@@@*',
    ' *###***********-   **###*',
)

_WORDMARK = (
    '███████╗ ██████╗██████╗  █████╗ ██████╗ ███████╗██████╗',
    '██╔════╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔════╝██╔══██╗',
    '███████╗██║     ██████╔╝███████║██████╔╝█████╗  ██████╔╝',
    '╚════██║██║     ██╔══██╗██╔══██║██╔═══╝ ██╔══╝  ██╔══██╗',
    '███████║╚██████╗██║  ██║██║  ██║██║     ███████╗██║  ██║',
    '╚══════╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝',
    '',
    '██████╗ ██╗   ██╗██████╗ ██████╗ ██╗   ██╗',
    '██╔══██╗██║   ██║██╔══██╗██╔══██╗╚██╗ ██╔╝',
    '██████╔╝██║   ██║██║  ██║██║  ██║ ╚████╔╝',
    '██╔══██╗██║   ██║██║  ██║██║  ██║  ╚██╔╝',
    '██████╔╝╚██████╔╝██████╔╝██████╔╝   ██║',
    '╚═════╝  ╚═════╝ ╚═════╝ ╚═════╝    ╚═╝',
)

_TAGLINES = (
    "",
    "P R O F I T   S Y S T E M S",
    "P R O P R I E T A R Y   T E C H N O L O G Y",
    "2 0 2 6",
)
_GAP = 4
WORD_BLOCK = tuple(_WORDMARK) + _TAGLINES
WORD_WIDTH = max(len(r) for r in WORD_BLOCK)


def _width(rows) -> int:
    return max(len(r) for r in rows)


def banner(columns: int | None = None) -> str:
    """Pick the layout for the terminal width."""
    if columns is None:
        columns = shutil.get_terminal_size((100, 40)).columns
    for mark in (_MARK_LARGE, _MARK_SMALL):
        if columns >= _width(mark) + _GAP + WORD_WIDTH + 2:
            return _side_by_side(mark)
    return _stacked(_MARK_SMALL, min(columns, 100))


def _side_by_side(mark) -> str:
    mark_width = _width(mark)
    height = max(len(mark), len(WORD_BLOCK))
    top = (height - len(WORD_BLOCK)) // 2
    words = [""] * top + list(WORD_BLOCK)
    words += [""] * (height - len(words))
    rows = []
    for i in range(height):
        left = (mark[i] if i < len(mark) else "").ljust(mark_width)
        rows.append((left + " " * _GAP + words[i]).rstrip())
    rows.append("")
    rows.append("─" * (mark_width + _GAP + WORD_WIDTH))
    return "\n".join(rows)


def _stacked(mark, width: int) -> str:
    rows = [r.center(width).rstrip() for r in mark]
    rows.append("")
    rows += [r.center(width).rstrip() for r in WORD_BLOCK]
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
