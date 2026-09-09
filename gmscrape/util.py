"""Small shared helpers: text/domain normalization, dotted-path lookups, DNS."""

from __future__ import annotations

import functools
import re
import unicodedata
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

import tldextract

# Keep tldextract offline-friendly: use the bundled snapshot instead of
# fetching the public suffix list on first use (which fails in sandboxes).
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())

_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_PUNCT_EDGE_RE = re.compile(r"^[^\w]+|[^\w]+$")


def squeeze(text: str) -> str:
    """Collapse all whitespace runs to single spaces."""
    return _WS_RE.sub(" ", text or "").strip()


def normalize_name(name: str) -> str:
    """Lowercase, strip accents and punctuation - for brand comparisons.

    >>> normalize_name("McDonald's #1234")
    'mcdonalds 1234'
    """
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = ascii_only.lower().replace("&", " and ").replace("'", "").replace("’", "")
    return squeeze(_NON_ALNUM_RE.sub(" ", lowered))


def name_tokens(name: str) -> list[str]:
    return [t for t in normalize_name(name).split(" ") if t]


def normalize_url(url: str) -> str:
    """Add a scheme when missing, drop fragments, and trim tracking noise."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    parts = urlsplit(url)
    if not parts.netloc:
        return ""
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc  # keep www; the host may only answer on it
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def registered_domain(url_or_host: str) -> str:
    """Return the registrable domain ('shop.acme.co.uk' -> 'acme.co.uk')."""
    value = (url_or_host or "").strip()
    if not value:
        return ""
    if "://" in value:
        value = urlsplit(value).netloc
    value = value.split("@")[-1].split(":")[0].strip().strip(".").lower()
    if not value:
        return ""
    ext = _EXTRACT(value)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return ""


def hostname(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    return urlsplit(url).netloc.split(":")[0].lower()


def has_valid_suffix(host: str) -> bool:
    """True when the host ends in a real public suffix (filters junk matches)."""
    ext = _EXTRACT((host or "").strip().lower())
    return bool(ext.domain and ext.suffix)


def same_site(a: str, b: str) -> bool:
    """Same registrable domain - or the same host for IP/intranet addresses."""
    da, db = registered_domain(a), registered_domain(b)
    if da and db:
        return da == db
    ha, hb = hostname(a), hostname(b)
    return bool(ha) and ha == hb


def dig(obj: Any, path: str, default: Any = None) -> Any:
    """Look up a dotted path in nested dicts/lists.

    Supports list indices and a bare ``[]`` to mean "first element":
    ``"results.0.address.city"``, ``"data[].website"``.
    """
    if not path:
        return obj
    cur = obj
    for part in path.replace("[]", ".0").split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


def first_dig(obj: Any, paths: Iterable[str], default: Any = None) -> Any:
    """First non-empty value among several candidate dotted paths."""
    for path in paths:
        value = dig(obj, path)
        if value not in (None, "", [], {}):
            return value
    return default


def as_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> Optional[int]:
    f = as_float(re.sub(r"[^\d.,-]", "", str(value)) if value not in (None, "") else value)
    return int(f) if f is not None else None


def clean_phone(value: Any) -> str:
    if not value:
        return ""
    return squeeze(str(value))


def trim_context(text: str, index: int, width: int = 60) -> str:
    """A short single-line snippet of `text` centred on `index`."""
    start = max(0, index - width)
    end = min(len(text), index + width)
    return squeeze(text[start:end])


@functools.lru_cache(maxsize=4096)
def mx_hosts(domain: str) -> tuple[str, ...]:
    """MX hostnames for a domain (falls back to A/AAAA per RFC 5321)."""
    if not domain:
        return ()
    try:
        import dns.resolver  # imported lazily so DNS stays optional
    except ImportError:  # pragma: no cover
        return ()
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5.0
    resolver.timeout = 5.0
    try:
        answers = resolver.resolve(domain, "MX")
        hosts = tuple(str(r.exchange).rstrip(".").lower() for r in answers if str(r.exchange) != ".")
        if hosts:
            return hosts
    except Exception:
        pass
    for rtype in ("A", "AAAA"):
        try:
            resolver.resolve(domain, rtype)
            return (domain,)
        except Exception:
            continue
    return ()


def domain_has_mx(domain: str) -> bool:
    return bool(mx_hosts(domain))


def strip_edge_punct(token: str) -> str:
    return _PUNCT_EDGE_RE.sub("", token or "")


def dedupe_preserving_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
