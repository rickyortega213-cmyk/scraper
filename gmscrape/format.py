"""Presentation rules for the client-facing output.

Everything a person reads - company, city, address, names, business type -
is title-cased with the exceptions people expect (McDonald's, LLC, DDS, "of"),
and phone numbers are normalized to (956)-324-6856.
"""

from __future__ import annotations

import re

# Never re-cased: acronyms, credentials, state codes, ordinal suffixes.
_KEEP_UPPER = {
    "llc", "inc", "co", "ltd", "llp", "pllc", "pc", "pa", "dds", "dmd", "md", "do",
    "dvm", "od", "dc", "cpa", "esq", "phd", "rn", "np", "mba", "jd", "cfp", "pe",
    "hvac", "ac", "usa", "us", "uk", "tx", "ca", "ny", "fl", "il", "wa", "az", "nv",
    "ga", "nc", "sc", "va", "pa", "oh", "mi", "mn", "wi", "co", "or", "ut", "id",
    "mt", "wy", "nd", "sd", "ne", "ks", "ok", "ar", "la", "ms", "al", "tn", "ky",
    "wv", "md", "de", "nj", "ct", "ri", "ma", "vt", "nh", "me", "hi", "ak", "dc",
    "ii", "iii", "iv", "atx", "dfw", "nyc", "sf", "bbq", "cbd", "spa", "atm",
    "ymca", "ups", "cvs", "kfc", "ihop", "h-e-b", "heb", "at&t", "tv", "diy", "rv",
    "suv", "ev",
}
# Lower-case inside a phrase (but capitalized when first).
_KEEP_LOWER = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on",
               "or", "the", "to", "vs", "via", "de", "del", "la", "y", "e"}
_MC_RE = re.compile(r"^(mc|mac)([a-z])(.*)$")
_ROMAN_RE = re.compile(r"^(?:ii|iii|iv|vi|vii|viii|ix)$")


def _cap_word(word: str, first: bool) -> str:
    lower = word.lower()
    if not word:
        return word
    if lower in _KEEP_UPPER or _ROMAN_RE.match(lower):
        return word.upper() if lower not in {"spa", "co"} or first else (
            "Spa" if lower == "spa" else "Co")
    if lower in _KEEP_LOWER and not first:
        return lower
    # Ordinals: 1st, 2nd, 3rd, 4th
    if re.fullmatch(r"\d+(st|nd|rd|th)", lower):
        return lower
    # Already-mixed brand casing with internal capitals (iPhone, eBay, McDonald's)
    if any(ch.isupper() for ch in word[1:]) and not word.isupper():
        return word
    match = _MC_RE.match(lower)
    if match and len(match.group(3)) >= 2:
        return match.group(1).capitalize() + match.group(2).upper() + match.group(3)
    # O'Brien, D'Angelo
    if "'" in lower[:3] and len(lower) > 2:
        head, _, tail = lower.partition("'")
        if len(head) == 1:
            return head.upper() + "'" + tail.capitalize()
    return lower.capitalize()


def smart_title(text: str) -> str:
    """Title case that people would agree with.

    >>> smart_title("JOE'S PLUMBING & HEATING LLC")
    "Joe's Plumbing & Heating LLC"
    >>> smart_title("mcdonald's of round rock")
    "McDonald's of Round Rock"
    """
    text = (text or "").strip()
    if not text:
        return ""
    out: list[str] = []
    letters = [ch for ch in text if ch.isalpha()]
    shouting = bool(letters) and all(ch.isupper() for ch in letters)
    # Split on spaces but keep hyphenated / slashed parts cased individually.
    for index, token in enumerate(re.split(r"(\s+)", text)):
        if not token or token.isspace():
            out.append(token)
            continue
        bare = token.strip("&,.!()")
        if (not shouting and bare.isalpha() and bare.isupper() and 2 <= len(bare) <= 4):
            out.append(token)               # IHG, KFC, DDS: an acronym the source wrote in caps
            continue
        parts = re.split(r"([-/])", token)
        cased = []
        for i, part in enumerate(parts):
            if part in ("-", "/"):
                cased.append(part)
            elif len(part) == 1 and len(parts) > 1:
                cased.append(part.upper())          # H-E-B, A/C
            else:
                cased.append(_cap_word(part, first=(index == 0 and i == 0)))
        out.append("".join(cased))
    result = "".join(out)
    # "'S" after title-casing possessives: Joe'S -> Joe's
    result = re.sub(r"'S\b", "'s", result)
    return result


def format_phone(raw: str) -> str:
    """Any US/Canada phone -> (956)-324-6856. Others are returned cleaned."""
    text = (raw or "").strip()
    if not text:
        return ""
    ext = ""
    match = re.search(r"(?:ext\.?|x|extension)\s*(\d{1,6})\s*$", text, re.IGNORECASE)
    if match:
        ext = match.group(1)
        text = text[: match.start()]
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        formatted = f"({digits[:3]})-{digits[3:6]}-{digits[6:]}"
        return f"{formatted} ext. {ext}" if ext else formatted
    return re.sub(r"\s+", " ", raw.strip())


def split_name(full_name: str) -> tuple[str, str]:
    """'Dr. Priya K. Patel' -> ('Priya', 'Patel'); 'Cher' -> ('Cher', '')."""
    name = re.sub(r"^(?:dr|mr|mrs|ms|miss|prof)\.?\s+", "", (full_name or "").strip(), flags=re.I)
    tokens = [t for t in name.replace(",", " ").split() if t]
    # drop middle initials like "K." / "K"
    tokens = [t for i, t in enumerate(tokens) if not (0 < i < len(tokens) - 1 and len(t.rstrip(".")) == 1)]
    if not tokens:
        return "", ""
    first = smart_title(tokens[0])
    last = smart_title(" ".join(tokens[1:])) if len(tokens) > 1 else ""
    return first, last


def clean_state(state: str) -> str:
    state = (state or "").strip()
    if len(state) == 2:
        return state.upper()
    return smart_title(state)
