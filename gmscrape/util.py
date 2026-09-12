"""Small shared helpers: text/domain normalization, dotted-path lookups, DNS."""

from __future__ import annotations

import threading
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


_MX_CACHE: dict[str, tuple[str, ...]] = {}
_MX_CACHE_MAX = 200_000
_MX_LOCK = threading.Lock()


def mx_lookup(domain: str) -> Optional[tuple[str, ...]]:
    """MX hostnames for a domain (A/AAAA fallback per RFC 5321).

    Returns () when DNS says the domain cannot receive mail, and None when
    DNS could not answer (timeout, server failure) - which is not the same
    thing, and must never be cached as "no MX". Only definite answers are
    remembered, so a transient DNS hiccup is retried the next time the
    domain comes up instead of silently disabling permutations for it.
    """
    if not domain:
        return ()
    domain = domain.lower()
    with _MX_LOCK:
        if domain in _MX_CACHE:
            return _MX_CACHE[domain]
    answer = _mx_lookup_uncached(domain)
    if answer is not None:
        with _MX_LOCK:
            if len(_MX_CACHE) >= _MX_CACHE_MAX:
                _MX_CACHE.clear()
            _MX_CACHE[domain] = answer
    return answer


def _mx_lookup_uncached(domain: str) -> Optional[tuple[str, ...]]:
    try:
        import dns.resolver  # imported lazily so DNS stays optional
        from dns.exception import Timeout
    except ImportError:  # pragma: no cover
        return None
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 4.0
    resolver.timeout = 2.0
    indeterminate = False
    try:
        answers = resolver.resolve(domain, "MX")
        hosts = tuple(str(r.exchange).rstrip(".").lower() for r in answers if str(r.exchange) != ".")
        if hosts:
            return hosts
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        pass
    except Timeout:
        indeterminate = True
    except Exception:
        indeterminate = True
    for rtype in ("A", "AAAA"):
        try:
            resolver.resolve(domain, rtype)
            return (domain,)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            continue
        except Exception:
            indeterminate = True
    return None if indeterminate else ()


def mx_hosts(domain: str) -> tuple[str, ...]:
    return mx_lookup(domain) or ()


def domain_has_mx(domain: str) -> bool:
    return bool(mx_lookup(domain))


def prefetch_mx(domains: Iterable[str], workers: int = 16) -> None:
    """Warm the MX cache for many domains at once (DNS is latency-bound)."""
    from concurrent.futures import ThreadPoolExecutor

    unique = [d for d in dedupe_preserving_order(d for d in domains if d)]
    if not unique:
        return
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(unique)))) as pool:
        list(pool.map(mx_lookup, unique))


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


_HOUSE_NUMBER_RE = re.compile(r"^\d+[A-Za-z]?\b")


def city_from_address(address: str) -> str:
    """'220 Oak Ave, Austin, TX 78702' -> 'Austin'  (best effort, '' if unsure).

    The city is the segment right after the street (the first segment that
    starts with a house number); anything else - "Austin, TX" alone, a bare
    region - is not confident enough to gate search results on.
    """
    parts = [squeeze(p) for p in (address or "").split(",") if squeeze(p)]
    for index, part in enumerate(parts[:-1]):
        if _HOUSE_NUMBER_RE.match(part):
            candidate = parts[index + 1]
            candidate = re.sub(r"\s+\d{4,}(?:-\d{4})?$", "", candidate)
            if candidate and not any(ch.isdigit() for ch in candidate) and len(candidate) > 2:
                return candidate
            return ""
    return ""


def current_rss_mb() -> float:
    """Resident memory of this process right now, in MB (0 when unknown)."""
    import os
    import sys

    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/statm", encoding="ascii") as handle:
                pages = int(handle.read().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
        import subprocess

        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return float(out) / 1024 if out else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def total_ram_mb() -> float:
    import os

    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        return 0.0
