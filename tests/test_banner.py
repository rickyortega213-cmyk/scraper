"""The launch banner: mark beside the wordmark, sized to the terminal, or stacked when narrow."""

from gmscrape.banner import WORD_WIDTH, banner


def _rows(text):
    return text.splitlines()


def test_normal_terminal_gets_the_half_size_mark_beside_the_wordmark():
    lines = _rows(banner(100))
    assert all(len(line) <= 100 for line in lines)
    scraper = next(line for line in lines if "███████╗ ██████╗" in line)
    buddy = next(line for line in lines if "██████╗ ██╗   ██╗██████╗" in line)
    assert scraper.index("███") > 30 and buddy.index("███") > 30      # both words right of the mark
    assert any(line.startswith("@@@@") for line in lines)              # the mark, left column
    assert "P R O F I T   S Y S T E M S" in "\n".join(lines)


def test_very_wide_terminal_gets_the_full_size_mark():
    lines = _rows(banner(140))
    assert all(len(line) <= 140 for line in lines)
    scraper = next(line for line in lines if "███████╗ ██████╗" in line)
    assert scraper.index("███") > 70                                    # the wide mark sits first


def test_stacked_when_narrow():
    lines = _rows(banner(80))
    assert all(len(line) <= 80 for line in lines)
    mark_rows = [i for i, line in enumerate(lines) if "@@@@" in line]
    word_rows = [i for i, line in enumerate(lines) if "███" in line]
    assert mark_rows and word_rows and max(mark_rows) < min(word_rows)
    assert WORD_WIDTH <= 80
