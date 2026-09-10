"""The launch banner: mark beside the wordmark, or stacked when the terminal is narrow."""

from gmscrape.banner import MARK_WIDTH, WIDE, banner


def test_side_by_side_when_wide_enough():
    text = banner(140)
    lines = text.splitlines()
    assert all(len(line) <= WIDE for line in lines)
    assert any("SCRAPER" not in line and "███████╗" in line for line in lines)   # wordmark present
    assert any(line.startswith(":@@@@@@") for line in lines)                      # the mark, left column
    assert "B U D D Y   2 . 0" in text and "P R O F I T   S Y S T E M S" in text
    # the wordmark sits to the right of the mark, on the same rows
    row = next(line for line in lines if "███████╗ ██████╗" in line)
    assert row.index("███████╗") >= MARK_WIDTH


def test_stacked_when_narrow():
    text = banner(80)
    lines = text.splitlines()
    assert all(len(line) <= 80 for line in lines)
    mark_rows = [i for i, line in enumerate(lines) if "@@@@" in line]
    word_rows = [i for i, line in enumerate(lines) if "███" in line]
    assert mark_rows and word_rows and max(mark_rows) < min(word_rows)              # mark above, words below
