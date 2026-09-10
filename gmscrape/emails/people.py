"""Find the owner / founder / top decision-maker of a local business.

Two sources, one extractor:
  * the business's own website (About / Team / Contact pages, JSON-LD)
  * web search results (Google AI Overview, knowledge panel, snippets)

The extractor is deliberately conservative. A wrong owner name turns into a
wrong email that verifies "valid" on a catch-all domain and ends up in an
outreach list, so every candidate must:
  - be pulled from an explicit ownership pattern ("Owner: Jane Doe",
    "founded by Jane Doe", "Jane Doe, DDS"), never from a bare capitalized pair;
  - look like a person (2-3 name tokens, no stopwords, no digits, not the
    business name itself);
  - for search results, appear in the same sentence as the business name.
Where the evidence names two different people with equal support, we return
nobody rather than guess.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from bs4 import BeautifulSoup

from ..models import Person
from ..util import name_tokens, normalize_name, squeeze

# --- seniority ---------------------------------------------------------------
TITLE_RANK: dict[str, int] = {
    "owner": 100, "co-owner": 98, "proprietor": 97, "founder": 95, "co-founder": 94,
    "ceo": 90, "chief executive officer": 90, "president": 85, "principal": 80,
    "managing partner": 78, "managing member": 77, "managing director": 76,
    "partner": 70, "senior partner": 72, "director": 60, "executive director": 62,
    "general manager": 55, "practice owner": 100, "practice manager": 45,
    "office manager": 40, "manager": 40, "head chef": 50, "chef owner": 100,
    "chef-owner": 100, "owner operator": 100,
    "broker owner": 100, "broker/owner": 100, "lead attorney": 65,
    "founding attorney": 95, "founding partner": 95, "medical director": 66,
    "dr": 64, "doctor": 64, "dds": 64, "dmd": 64, "md": 64, "dvm": 64, "od": 64,
    "dc": 64, "cpa": 60, "esq": 60,
    # chain / franchise roles
    "franchise owner": 100, "franchisee": 100, "operator": 90, "owner/operator": 100,
    "multi-unit franchisee": 100, "area developer": 85,
    "store manager": 58, "store director": 58, "branch manager": 56,
    "market manager": 54, "district manager": 52, "regional manager": 50,
    "location manager": 50, "restaurant manager": 48, "kitchen manager": 42,
    "assistant manager": 30, "assistant store manager": 30, "shift manager": 20,
}

# Titles that mark a practice principal in these industries.
_CREDENTIAL_TITLES = {"dds", "dmd", "md", "dvm", "od", "dc", "cpa", "esq", "dr", "doctor"}

# Credentials stripped off the end of a name: "Jane Doe, DDS", "John Roe MD, FACS"
_CREDENTIAL_RE = re.compile(
    r"(?:(?:\s*,\s*|\s+)(?:DDS|DMD|MD|DO|DVM|OD|DC|PhD|Ph\.D\.?|CPA|Esq\.?|Esquire|JD|J\.D\.?|"
    r"RN|NP|PA-C|LMT|LCSW|MBA|FACS|FAGD|MAGD|FAAD|RDH|CFP|EA|PE|AIA|CFA|LLC|Inc\.?)\.?)+$",
    re.IGNORECASE,
)
_HONORIFIC_RE = re.compile(r"^(?:dr|mr|mrs|ms|miss|prof|rev|sir|dame)\.?\s+", re.IGNORECASE)

# Name shape: capitalized tokens, allowing O'Brien, Smith-Jones, de la Cruz.
# The full-word alternative comes first so "John Kowalski" is never cut to
# "John K"; each alternative refuses to stop mid-word.
_TOKEN = r"(?:[A-Z][a-zA-Z'’\-]{1,24}(?![a-zA-Z])|[A-Z]\.(?![a-zA-Z])|[A-Z](?![a-zA-Z]))"
_PARTICLE = r"(?:de|da|del|della|di|van|von|der|la|le|al|bin|el|st\.?|mc|mac)(?![a-zA-Z])"
_NAME = rf"({_TOKEN}(?:\s+(?:{_TOKEN}|{_PARTICLE})){{0,3}})"
_DR = r"(?:Dr\.?\s+)?"
# Titles are matched case-insensitively; names stay case-sensitive so ordinary
# lowercase prose can never be mistaken for a person.
_TITLES = (
    r"(?i:co-?owner|owner[\s/-]*operator|owner|proprietor|co-?founder|founder|"
    r"founding (?:partner|attorney|member)|ceo|chief executive officer|president|"
    r"principal|managing (?:partner|member|director)|partner|(?:executive |medical )?director|"
    r"general manager|practice owner|chef[\s/-]*owner|broker[\s/-]*owner|head chef|"
    r"multi-unit franchisee|franchise owner|franchisee|operator|area developer|"
    r"store (?:manager|director)|branch manager|market manager|district manager|"
    r"regional manager|location manager|restaurant manager|kitchen manager|"
    r"assistant (?:store )?manager|shift manager|office manager|practice manager|manager)"
)

# "Owner: Jane Doe" / "Owner - Jane Doe" / "Owner Jane Doe" / "our owner, Jane Doe"
_TITLE_THEN_NAME = re.compile(
    rf"\b{_TITLES}\b(?P<sep>[\s:,\-–—]*)(?:(?i:is)\s+|(?i:and\s+operator)\s+)?{_DR}{_NAME}"
)
# "Jane Doe, Owner" / "Jane Doe - Owner" / "Jane Doe (Owner)" / "Jane Doe is the owner"
_NAME_THEN_TITLE = re.compile(
    rf"{_NAME}(?:,?\s+(?:DDS|DMD|MD|DVM|OD|DC|CPA|Esq\.?|PhD))?\s*[,\-–—(|:]?\s*"
    rf"(?:(?i:is)\s+(?:(?i:the|our)\s+)?|(?i:the|our)\s+)?{_TITLES}\b"
)
# "founded by Jane Doe" / "owned and operated by Jane Doe" / "started by Jane Doe"
_BY_NAME = re.compile(
    rf"\b(?i:founded|established|started|owned(?:\s+and\s+operated)?|run|led|opened|created)"
    rf"\s+(?:(?i:in)\s+\d{{4}}\s+)?(?i:by)\s+{_DR}{_NAME}"
)
# "The store manager of the Walmart Supercenter in Austin, TX is Dana Whitfield"
_TITLE_OF_IS_NAME = re.compile(
    rf"\b{_TITLES}\s+(?i:of|at|for)\s+[^.;|]{{0,90}}?\s(?i:is|was|:)\s+{_DR}{_NAME}"
)
# "Jane Doe owns two Subway locations" / "Jane Doe, who owns the ..."
_NAME_OWNS = re.compile(
    rf"{_NAME},?\s+(?:(?i:who)\s+)?(?i:owns and operates|owns|operates|franchises)\s+"
    rf"(?:(?i:the|a|an|two|three|four|five|six|several|multiple|\d+)\s+)?"
)
# "Dr. Jane Doe" on a medical/dental/vet/chiro site - the practice principal.
_DOCTOR_NAME = re.compile(rf"\bDr\.?\s+{_NAME}")
# "Jane Doe, DDS" / "Jane Doe DMD"
_CREDENTIALED_NAME = re.compile(rf"{_NAME},?\s+(DDS|DMD|MD|DVM|OD|DC|CPA|Esq\.?)\b")

_STOP_TOKENS = {
    # generic page words that get capitalized in headings
    "our", "the", "and", "team", "staff", "meet", "about", "contact", "home", "us",
    "welcome", "family", "friendly", "professional", "services", "service", "company",
    "business", "local", "quality", "best", "top", "new", "your", "we", "you", "all",
    "call", "today", "now", "free", "estimate", "estimates", "quote", "quotes",
    "owner", "owners", "founder", "founders", "manager", "president", "director",
    "partner", "partners", "principal", "ceo", "doctor", "doctors", "dr", "office",
    "hours", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    # brands / platforms that show up in text and look like names
    "google", "yelp", "facebook", "instagram", "linkedin", "youtube", "twitter",
    "bbb", "angi", "nextdoor", "tiktok", "amazon", "apple", "microsoft", "wordpress",
    "wix", "squarespace", "godaddy", "cloudflare", "shopify", "openai", "chatgpt",
    "ai", "overview", "reviews", "review", "rating", "ratings", "map", "maps",
    # industry nouns
    "dental", "dentistry", "dentist", "plumbing", "roofing", "hvac", "law", "legal",
    "clinic", "medical", "health", "care", "spa", "salon", "auto", "repair",
    "restaurant", "cafe", "coffee", "pizza", "bakery", "grill", "bar", "kitchen",
    "realty", "real", "estate", "insurance", "group", "associates", "llc", "inc",
    "corp", "co", "ltd", "pllc", "pc", "pa", "dba",
    # sentence starters that follow "by" in prose
    "a", "an", "this", "that", "these", "those", "his", "her", "their", "its",
    "one", "two", "three", "several", "many", "some", "local", "licensed",
    "certified", "experienced", "dedicated", "passionate", "veteran", "native",
    "husband", "wife", "brothers", "sisters", "father", "mother", "son", "daughter",
    "same", "current", "original", "previous", "former", "late", "second", "third",
    "of", "for", "at", "in", "on", "to", "with", "by", "from", "as", "or", "if",
    "response", "responses", "reply", "replies", "responded", "says", "said",
    "program", "portal", "login", "profile", "page", "site", "website", "info",
    "operator", "operated", "operations", "since", "est", "established",
    "currently", "hiring", "open", "closed", "proud", "very", "always", "still",
    "also", "yet", "not", "available", "responsible", "located", "here", "there",
    "unknown", "unavailable", "listed", "named", "usually", "typically", "often",
    "generally", "likely", "reportedly", "officially", "temporarily", "permanently",
}

# A title we matched inside a name capture ("Owner Operator") - never a person.
_TITLE_WORDS = {w for title in TITLE_RANK for w in title.replace("/", " ").replace("-", " ").split()}


@dataclass
class OwnerCandidate:
    name: str
    title: str
    rank: int
    source: str
    source_url: str = ""
    evidence: str = ""
    weight: int = 1
    mentions: int = 1
    loose: bool = False        # from a sentence shape that can also name a non-person

    @property
    def key(self) -> str:
        return normalize_name(self.name)


# --- name hygiene ----------------------------------------------------------
def clean_person_name(raw: str) -> str:
    """Strip honorifics/credentials; return '' when it's not a person name."""
    name = squeeze(raw or "").replace("’", "'")
    name = _HONORIFIC_RE.sub("", name)
    name = _CREDENTIAL_RE.sub("", name).strip(" ,.-")
    if not name or any(ch.isdigit() for ch in name):
        return ""
    tokens = name.split()
    if not 1 <= len(tokens) <= 4:
        return ""
    lowered = [t.lower().strip(".") for t in tokens]
    if lowered[0] in _STOP_TOKENS or lowered[-1] in _STOP_TOKENS:
        return ""
    if all(t in _STOP_TOKENS or t in _TITLE_WORDS for t in lowered):
        return ""
    if any(t in _TITLE_WORDS for t in lowered):
        return ""
    # Every real token must look like a name (letters, apostrophe, hyphen).
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z'\-\.]*", t) for t in tokens):
        return ""
    if len(tokens[0]) < 2:
        return ""
    return " ".join(tokens)


def looks_like_person(name: str, business_name: str = "") -> bool:
    cleaned = clean_person_name(name)
    if not cleaned:
        return False
    tokens = [t for t in name_tokens(cleaned) if len(t) > 1]
    if not tokens:
        return False
    if business_name:
        biz = set(name_tokens(business_name))
        # "Kowalski Roofing" naming its owner "Jan Kowalski" is fine; a "name"
        # made entirely of business-name words ("Austin Family") is not.
        if all(t in biz for t in tokens):
            return False
    return True


def _normalize_title(raw: str) -> tuple[str, int]:
    title = squeeze(raw or "").lower().replace("–", "-").replace("—", "-")
    title = re.sub(r"\s*/\s*", "/", title)
    for key in sorted(TITLE_RANK, key=len, reverse=True):
        if key in title:
            return key, TITLE_RANK[key]
    return title, 30


# --- extraction from text --------------------------------------------------
def _iter_text_matches(text: str, source: str, source_url: str = "") -> Iterable[OwnerCandidate]:
    text = squeeze(text)
    if not text:
        return
    for match in _TITLE_THEN_NAME.finditer(text):
        name = match.group(1)
        # "Owner Jane" is a review-widget header as often as a person; without a
        # ":"/"-" separator insist on a first and last name.
        if not re.search(r"[:\-–—,]", match.group("sep") or "") and len(name.split()) < 2:
            continue
        title, rank = _normalize_title(match.group(0)[: match.start(1) - match.start(0)])
        yield OwnerCandidate(name, title, rank, source, source_url,
                             _context(text, match.start(), match.end()))
    for match in _NAME_THEN_TITLE.finditer(text):
        tail = text[match.end(1): match.end()]
        title, rank = _normalize_title(tail)
        yield OwnerCandidate(match.group(1), title, rank, source, source_url,
                             _context(text, match.start(), match.end()))
    for match in _BY_NAME.finditer(text):
        verb = match.group(0).split()[0].lower()
        title = "founder" if verb in ("founded", "established", "started", "opened", "created") else "owner"
        yield OwnerCandidate(match.group(1), title, TITLE_RANK[title], source, source_url,
                             _context(text, match.start(), match.end()))
    for match in _TITLE_OF_IS_NAME.finditer(text):
        # "...the store manager of X is Dana Whitfield" - but also "...is
        # Currently Hiring". This shape only counts when something else on the
        # web names the same person (see _confidence).
        title, rank = _normalize_title(match.group(0)[: match.start(1) - match.start(0)])
        yield OwnerCandidate(match.group(1), title, rank, source, source_url,
                             _context(text, match.start(), match.end()), loose=True)
    for match in _NAME_OWNS.finditer(text):
        verb = match.group(0)[match.end(1) - match.start():].lower()
        title = "franchise owner" if "franchis" in verb else "owner"
        yield OwnerCandidate(match.group(1), title, TITLE_RANK[title], source, source_url,
                             _context(text, match.start(), match.end()))
    for match in _CREDENTIALED_NAME.finditer(text):
        cred = match.group(2).lower().strip(".")
        yield OwnerCandidate(match.group(1), cred, TITLE_RANK.get(cred, 60), source, source_url,
                             _context(text, match.start(), match.end()))


def _context(text: str, start: int, end: int, width: int = 80) -> str:
    return squeeze(text[max(0, start - width): min(len(text), end + width)])


def _iter_jsonld(html: str, source_url: str) -> Iterable[OwnerCandidate]:
    soup = BeautifulSoup(html or "", "lxml")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        yield from _walk_jsonld(data, source_url)


def _walk_jsonld(node: Any, source_url: str, role_hint: str = "") -> Iterable[OwnerCandidate]:
    if isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item, source_url, role_hint)
        return
    if not isinstance(node, dict):
        return
    node_type = str(node.get("@type") or "").lower()
    for key in ("founder", "founders", "owner", "owners", "employee", "employees",
                "member", "members", "author"):
        if key in node:
            hint = "founder" if key.startswith("founder") else (
                "owner" if key.startswith("owner") else "employee")
            value = node[key]
            if isinstance(value, str):
                if hint != "employee":
                    yield OwnerCandidate(value, hint, TITLE_RANK[hint], "site_jsonld", source_url, f"jsonld:{key}")
            else:
                yield from _walk_jsonld(value, source_url, hint)
    if node_type == "person" or ("name" in node and ("jobtitle" in {k.lower() for k in node})):
        name = str(node.get("name") or "")
        job = str(node.get("jobTitle") or node.get("jobtitle") or "")
        if job:
            title, rank = _normalize_title(job)
            if rank >= 40:
                yield OwnerCandidate(name, title, rank, "site_jsonld", source_url, f"jsonld:{job}")
        elif role_hint in ("founder", "owner"):
            yield OwnerCandidate(name, role_hint, TITLE_RANK[role_hint], "site_jsonld", source_url, f"jsonld:{role_hint}")
    for key, value in node.items():
        if key in ("founder", "founders", "owner", "owners", "employee", "employees",
                   "member", "members", "author"):
            continue
        if isinstance(value, (dict, list)):
            yield from _walk_jsonld(value, source_url, "")


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "template", "nav", "footer"]):
        tag.decompose()
    # Headings and list items are where "Jane Doe, Owner" lives; keep boundaries.
    return squeeze(soup.get_text(" | ", strip=True))


def owner_candidates_from_html(
    html: str, page_url: str, business_name: str, *, medical: bool = False
) -> list[OwnerCandidate]:
    """Every plausible owner mention on one page."""
    out: list[OwnerCandidate] = []
    for cand in _iter_jsonld(html, page_url):
        out.append(cand)
    text = _visible_text(html)
    for cand in _iter_text_matches(text, "site_text", page_url):
        out.append(cand)
    if medical:
        # On a practice site "Dr. Jane Doe" is the principal unless we see a
        # stronger title elsewhere; weight it below explicit ownership.
        for match in _DOCTOR_NAME.finditer(text):
            out.append(OwnerCandidate(match.group(1), "dr", TITLE_RANK["dr"], "site_text",
                                      page_url, _context(text, match.start(), match.end())))
    return [c for c in out if _accept(c, business_name)]


def _accept(cand: OwnerCandidate, business_name: str) -> bool:
    cleaned = clean_person_name(cand.name)
    if not cleaned or not looks_like_person(cleaned, business_name):
        return False
    cand.name = cleaned
    return True


# --- extraction from search ------------------------------------------------
def owner_candidates_from_search(
    blocks: Iterable[tuple[str, str]],
    business_name: str,
    city: str = "",
    *,
    require_location: bool = False,
) -> list[OwnerCandidate]:
    """Owner mentions from search text, only where the business is named.

    With `require_location`, the text must also mention the city: "Walmart"
    appears in every snippet on the web, so for a chain the location is what
    ties a manager to *this* store.
    """
    biz_norm = normalize_name(business_name)
    city_norm = normalize_name(city)
    brand_tokens = set(name_tokens(business_name)) - _STOP_TOKENS
    city_tokens = set(city_norm.split()) if city_norm else set()
    biz_tokens = [t for t in name_tokens(business_name) if len(t) > 2 and t not in _STOP_TOKENS]
    weights = {"search_ai_overview": 3, "search_answer": 3, "search_knowledge": 3,
               "search_snippet": 2, "search_related": 1}
    out: list[OwnerCandidate] = []
    for source, text in blocks:
        text = squeeze(text)
        if not text:
            continue
        lowered = normalize_name(text)
        names_the_business = biz_norm and biz_norm in lowered
        if not names_the_business and biz_tokens:
            # Allow a loose match: most distinctive tokens present.
            hits = sum(1 for t in biz_tokens if t in lowered)
            names_the_business = hits >= max(1, (len(biz_tokens) + 1) // 2)
        if not names_the_business:
            continue
        if require_location and city_norm and city_norm not in lowered:
            continue
        # Knowledge-panel attributes are already keyed: "attributes.founder: Jane".
        if source == "search_knowledge":
            for match in re.finditer(
                r"(?:founder|founders|owner|owners|ceo|president|proprietor)s?\s*[:=]\s*([^;|]+)", text, re.IGNORECASE
            ):
                title, rank = _normalize_title(match.group(0).split(":")[0])
                for name in re.split(r",|\band\b|&", match.group(1)):
                    cand = OwnerCandidate(name.strip(), title, rank, source, "", match.group(0), weight=3)
                    if _accept(cand, business_name):
                        out.append(cand)
        for cand in _iter_text_matches(text, source):
            cand.weight = weights.get(source, 1)
            if not _accept(cand, business_name):
                continue
            tokens = set(name_tokens(cand.name))
            # "Austin McDonald's franchisee ..." reads as a name; it is the
            # city plus the brand. In search text, a person's name never
            # borrows either.
            if tokens & city_tokens or (require_location and tokens & brand_tokens):
                continue
            out.append(cand)
    return out


# --- choosing one person ---------------------------------------------------
def choose_owner(
    candidates: Iterable[OwnerCandidate],
    *,
    min_confidence: int = 60,
    preferred_titles: Sequence[str] = (),
) -> Optional[Person]:
    """Collapse mentions into one Person, or None when the evidence is unsafe.

    `preferred_titles` (best first) re-ranks roles for the situation: at a
    corporate store the store manager outranks a regional VP quoted in a press
    release; at a franchise the franchisee outranks the general manager.
    """
    groups: dict[str, list[OwnerCandidate]] = defaultdict(list)
    for cand in candidates:
        if preferred_titles:
            reranked = _reranked(cand, preferred_titles)
            if reranked is None:
                continue          # a corporate CEO is never the store's contact
            cand = reranked
        groups[cand.key].append(cand)
    if not groups:
        return None

    scored: list[tuple[int, int, str, OwnerCandidate]] = []
    for key, mentions in groups.items():
        best = max(mentions, key=lambda c: (c.rank, c.weight))
        support = sum(c.weight for c in mentions)
        distinct_sources = len({c.source for c in mentions})
        # rank dominates; support and multi-source corroboration break ties
        score = best.rank * 10 + support * 4 + distinct_sources * 3
        scored.append((score, support, key, best))
    scored.sort(key=lambda t: (-t[0], t[2]))

    top_score, top_support, _, top = scored[0]
    if len(scored) > 1:
        second_score, second_support, _, second = scored[1]
        # Two different people with the same rank and equal support: ambiguous.
        if second.rank == top.rank and second_support == top_support and \
                _first_name(top.name) != _first_name(second.name):
            return None

    # Rivals are other people claiming the *same* role; a district manager
    # named alongside the store manager is context, not competition.
    rivals = sum(1 for score, _, _, cand in scored if cand.rank == top.rank)
    top_mentions = groups[scored[0][2]]
    all_loose = all(c.loose for c in top_mentions)
    confidence = _confidence(top, len(top_mentions), rivals, all_loose)
    if confidence < min_confidence:
        return None
    return Person(
        name=top.name, title=top.title, rank=top.rank, source=top.source,
        source_url=top.source_url, confidence=confidence, evidence=top.evidence[:240],
    )


def _reranked(cand: OwnerCandidate, preferred: Sequence[str]) -> Optional[OwnerCandidate]:
    """Copy of `cand` ranked by its position in the preferred titles, or None
    when its role is not one we are looking for at this kind of business."""
    title = cand.title
    for index, wanted in enumerate(preferred):
        if title == wanted or (wanted == "manager" and title.endswith("manager")):
            # Every preferred title is a good answer; the spacing only orders them.
            rank = 100 - index * 3
            return OwnerCandidate(cand.name, title, rank, cand.source, cand.source_url,
                                  cand.evidence, cand.weight, cand.mentions, cand.loose)
    return None


def _first_name(name: str) -> str:
    tokens = name_tokens(name)
    return tokens[0] if tokens else ""


def _confidence(best: OwnerCandidate, mentions: int, rivals: int, all_loose: bool = False) -> int:
    """0-100. `mentions` is how many separate statements named this person -
    a single weighty source is still a single statement."""
    base = {
        "site_jsonld": 85, "site_text": 75,
        "search_knowledge": 80, "search_ai_overview": 72, "search_answer": 72,
        "search_snippet": 58, "search_related": 45,
    }.get(best.source, 50)
    base += min(15, (mentions - 1) * 5)
    if best.rank >= 85:
        base += 5           # the role we were actually looking for
    elif best.rank < 60:
        base -= 10
    if len(name_tokens(best.name)) < 2:
        base -= 20          # first name only - weak for permutations
    if rivals > 1:
        base -= 5 * (rivals - 1)
    if all_loose:
        base -= 25          # one loosely-shaped sentence is not evidence
    return max(0, min(100, base))


def owner_from_html(html: str, page_url: str, business_name: str, *,
                    medical: bool = False, min_confidence: int = 60) -> Optional[Person]:
    return choose_owner(
        owner_candidates_from_html(html, page_url, business_name, medical=medical),
        min_confidence=min_confidence,
    )


def is_medical(category: str, business_type: str = "") -> bool:
    haystack = f"{category} {business_type}".lower()
    return bool(re.search(
        r"dent|orthodont|doctor|physician|clinic|medical|pediatric|derma|chiro|"
        r"optomet|veterinar|animal hospital|urgent care|surgeon|podiatr|psychiatr|"
        r"therap|counsel|wellness|health", haystack,
    ))


def owner_local_parts(person: Person) -> list[str]:
    """Most-common-first mailbox patterns for a named person.

    firstname@, firstname.lastname@, firstinitiallastname@, firstnamelastinitial@,
    firstname_lastname@, firstnamelastname@, lastname@, firstinitial.lastname@
    """
    first, last = person.first, person.last
    first = re.sub(r"[^a-z]", "", first)
    last = re.sub(r"[^a-z]", "", last)
    if not first:
        return []
    if not last:
        return [first]
    patterns = [
        first,
        f"{first}.{last}",
        f"{first[0]}{last}",
        f"{first}{last[0]}",
        f"{first}_{last}",
        f"{first}{last}",
        last,
        f"{first[0]}.{last}",
        f"{first}-{last}",
        f"{last}.{first}",
    ]
    seen: set[str] = set()
    out: list[str] = []
    for local in patterns:
        if local and local not in seen and len(local) >= 2:
            seen.add(local)
            out.append(local)
    return out


def email_matches_person(local_part: str, person: Person) -> bool:
    """Does an address found on the site belong to this person?"""
    local = re.sub(r"[^a-z]", "", local_part.lower())
    if not local:
        return False
    return local in {re.sub(r"[^a-z]", "", p) for p in owner_local_parts(person)}


_EMAIL_IN_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9][A-Za-z0-9._%+\-]{0,63})@"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})(?![A-Za-z0-9\-])"
)


def emails_for_person_in_text(text: str, person: Person) -> list[tuple[str, str]]:
    """Addresses in `text` that spell this person's name - (email, context).

    Used on search snippets and press releases: an address is attributed to the
    person only when its mailbox name matches them, never by proximity alone.
    """
    out: list[tuple[str, str]] = []
    for match in _EMAIL_IN_TEXT_RE.finditer(text or ""):
        local, domain = match.group(1), match.group(2)
        if email_matches_person(local, person):
            email = f"{local}@{domain}".lower()
            out.append((email, _context(text, match.start(), match.end())))
    return out
