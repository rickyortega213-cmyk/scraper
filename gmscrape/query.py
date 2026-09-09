"""Parse user search queries of the form 'business type in location'.

The input format is deliberately loose: people type "dentists in Austin, TX",
"plumber near Miami FL", "coffee shop - Boulder CO" or just "med spa 90210".
Everything is normalized to a QuerySpec(business_type, location).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Iterator

from .models import QuerySpec
from .util import squeeze

# Separators between the "what" and the "where", longest first.
_SPLITTERS = (
    r"\s+in\s+",
    r"\s+near\s+me\s+in\s+",
    r"\s+near\s+",
    r"\s+around\s+",
    r"\s+within\s+",
    r"\s+located\s+in\s+",
    r"\s+based\s+in\s+",
    r"\s+@\s*",
    r"\s+-\s+",
    r"\s+–\s+",
    r"\s*\|\s*",
)
_SPLIT_RE = re.compile("|".join(_SPLITTERS), re.IGNORECASE)

_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_STATE_RE = re.compile(
    r"\b(a[klrz]|c[aot]|d[ce]|fl|ga|hi|i[adln]|k[sy]|la|m[adeinost]|"
    r"n[cdehjmvy]|o[hkr]|pa|ri|s[cd]|t[nx]|ut|v[at]|w[aivy])\b",
    re.IGNORECASE,
)
_COMMENT_RE = re.compile(r"^\s*#")


def parse_query(raw: str) -> QuerySpec:
    """Split one raw query string into business type + location."""
    text = squeeze(raw)
    if not text:
        raise ValueError("empty query")

    match = _SPLIT_RE.search(text)
    if match:
        business = squeeze(text[: match.start()])
        location = squeeze(text[match.end():])
        if business and location:
            return QuerySpec(raw=text, business_type=business, location=location)

    # No explicit separator: try "<what>, <City>, <ST>" / "<what> <City>, <ST>"
    # or a trailing ZIP code.
    comma_parts = [squeeze(p) for p in text.split(",") if squeeze(p)]
    if len(comma_parts) >= 2 and _STATE_RE.fullmatch(comma_parts[-1] or ""):
        if len(comma_parts) >= 3:
            # "Roofing Companies, Dallas, TX" -> what | "Dallas, TX"
            return QuerySpec(
                raw=text,
                business_type=comma_parts[0],
                location=squeeze(", ".join(comma_parts[1:])),
            )
        # "Roofing Companies Dallas, TX" -> last word of the head is the city
        head_words = comma_parts[0].split(" ")
        if len(head_words) >= 2:
            business = squeeze(" ".join(head_words[:-1]))
            location = squeeze(f"{head_words[-1]}, {comma_parts[-1]}")
            return QuerySpec(raw=text, business_type=business, location=location)

    zip_match = _ZIP_RE.search(text)
    if zip_match and zip_match.start() > 0:
        business = squeeze(text[: zip_match.start()])
        location = squeeze(text[zip_match.start():])
        if business:
            return QuerySpec(raw=text, business_type=business, location=location)

    # Give up on splitting - pass the whole string to Maps as-is.
    return QuerySpec(raw=text, business_type=text, location="")


def parse_queries(items: Iterable[str]) -> list[QuerySpec]:
    specs: list[QuerySpec] = []
    seen: set[str] = set()
    for item in items:
        for line in str(item).splitlines():
            line = line.strip()
            if not line or _COMMENT_RE.match(line):
                continue
            spec = parse_query(line)
            key = spec.search_string.lower()
            if key not in seen:
                seen.add(key)
                specs.append(spec)
    return specs


def read_query_file(path: str | Path) -> Iterator[str]:
    """Yield query lines from a text or CSV file (one query per line)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"query file not found: {p}")
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip().strip('"').strip("'")
        if line and not _COMMENT_RE.match(line):
            yield line
