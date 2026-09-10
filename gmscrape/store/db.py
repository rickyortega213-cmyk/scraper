"""SQLite persistence: page cache, verification cache, and run results.

Two jobs:
  * cache - never re-fetch a page or re-verify an address you already paid for
  * results - a durable, queryable record so runs can be resumed and exported
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ..models import BusinessResult, EmailCandidate, Person, Place, VerificationResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url         TEXT PRIMARY KEY,
    final_url   TEXT,
    status      INTEGER,
    html        TEXT,
    fetched_at  REAL
);
CREATE TABLE IF NOT EXISTS verifications (
    email       TEXT PRIMARY KEY,
    status      TEXT,
    provider    TEXT,
    score       REAL,
    sub_status  TEXT,
    is_catch_all INTEGER,
    is_disposable INTEGER,
    is_role     INTEGER,
    free        INTEGER,
    mx_found    INTEGER,
    error       TEXT,
    raw         TEXT,
    checked_at  REAL
);
CREATE TABLE IF NOT EXISTS web_searches (
    query_key   TEXT PRIMARY KEY,
    provider    TEXT,
    query       TEXT,
    payload     TEXT,
    fetched_at  REAL
);
CREATE TABLE IF NOT EXISTS run_queries (
    run_id      TEXT,
    query       TEXT,
    status      TEXT,
    found       INTEGER,
    updated_at  REAL,
    PRIMARY KEY (run_id, query)
);
CREATE TABLE IF NOT EXISTS maps_cache (
    query_key   TEXT PRIMARY KEY,
    provider    TEXT,
    query       TEXT,
    places      TEXT,
    complete    INTEGER,
    fetched_at  REAL
);
CREATE TABLE IF NOT EXISTS domain_facts (
    domain       TEXT PRIMARY KEY,
    has_mx       INTEGER,
    is_catch_all INTEGER,
    updated_at   REAL
);
CREATE TABLE IF NOT EXISTS businesses (
    key          TEXT PRIMARY KEY,
    run_id       TEXT,
    query        TEXT,
    name         TEXT,
    place_id     TEXT,
    category     TEXT,
    address      TEXT,
    city         TEXT,
    state        TEXT,
    postal_code  TEXT,
    phone        TEXT,
    website      TEXT,
    domain       TEXT,
    rating       REAL,
    reviews      INTEGER,
    latitude     REAL,
    longitude    REAL,
    google_url   TEXT,
    is_chain     INTEGER,
    chain_score  INTEGER,
    chain_reasons TEXT,
    website_status TEXT,
    pages_crawled TEXT,
    domain_has_mx INTEGER,
    permutations_skipped_reason TEXT,
    notes        TEXT,
    place_raw    TEXT,
    updated_at   REAL
);
CREATE TABLE IF NOT EXISTS emails (
    business_key TEXT,
    email        TEXT,
    source       TEXT,
    source_url   TEXT,
    pattern      TEXT,
    context      TEXT,
    is_role      INTEGER,
    is_personal_domain INTEGER,
    is_low_value INTEGER,
    on_business_domain INTEGER,
    status       TEXT,
    sub_status   TEXT,
    provider     TEXT,
    score        REAL,
    confidence   INTEGER,
    notes        TEXT,
    updated_at   REAL,
    PRIMARY KEY (business_key, email)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    started_at REAL,
    finished_at REAL,
    queries    TEXT,
    settings   TEXT,
    stats      TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_business ON emails(business_key);
CREATE INDEX IF NOT EXISTS idx_businesses_run ON businesses(run_id);
CREATE INDEX IF NOT EXISTS idx_businesses_domain ON businesses(domain);
"""

class Store:
    """Thin SQLite wrapper.

    The verification stage runs in a thread pool and the crawler runs in an
    event loop, so every statement is serialized behind one lock rather than
    opening a connection per thread (the cache is small and writes are short).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        parent = Path(self.path).expanduser().parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        # Earlier versions remembered "0 businesses" as a final answer for a week.
        self.conn.execute("DELETE FROM maps_cache WHERE complete = 1 AND places IN ('[]', '')")
        self.conn.commit()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        wanted = {
            "runs": {"status": "TEXT", "run_table": "TEXT", "total": "INTEGER", "done": "INTEGER",
                     "updated_at": "REAL"},
            "businesses": {"stage": "TEXT", "website_source": "TEXT", "website_confidence": "INTEGER",
                           "owner_name": "TEXT", "owner_title": "TEXT", "owner_source": "TEXT",
                           "owner_confidence": "INTEGER", "chain_kind": "TEXT", "target_role": "TEXT",
                           "domain_is_catch_all": "INTEGER"},
            "emails": {"contact_type": "TEXT", "contact_name": "TEXT", "contact_title": "TEXT",
                       "lead_eligible": "INTEGER", "is_personal_domain": "INTEGER"},
        }
        for table, columns in wanted.items():
            existing = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, ctype in columns.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")

    # --- page cache --------------------------------------------------------
    MAX_PAGE_BYTES = 400_000

    def get_page(self, url: str, ttl_hours: int) -> Optional[tuple[int, str, str]]:
        with self._lock:
            cutoff = time.time() - max(0, ttl_hours) * 3600
            row = self.conn.execute(
                "SELECT status, final_url, html FROM pages WHERE url = ? AND fetched_at >= ?",
                (url, cutoff),
            ).fetchone()
            if row is None:
                return None
            body = row["html"]
            if isinstance(body, bytes):
                try:
                    body = zlib.decompress(body).decode("utf-8", errors="replace")
                except zlib.error:
                    return None
            return int(row["status"] or 0), str(row["final_url"] or ""), str(body or "")

    def put_page(self, url: str, status: int, final_url: str, html: str) -> None:
        """Cache a page, compressed and capped - thousands of sites must not
        turn the database into gigabytes."""
        payload = zlib.compress((html or "")[: self.MAX_PAGE_BYTES].encode("utf-8"), 6)
        with self._lock:
            self.conn.execute(
                "INSERT INTO pages(url, final_url, status, html, fetched_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET final_url=excluded.final_url, "
                "status=excluded.status, html=excluded.html, fetched_at=excluded.fetched_at",
                (url, final_url, status, payload, time.time()),
            )
            self.conn.commit()

    def prune(self, page_ttl_hours: int = 168, search_ttl_hours: int = 720) -> int:
        """Drop expired cache rows; returns how many went."""
        with self._lock:
            now = time.time()
            removed = self.conn.execute(
                "DELETE FROM pages WHERE fetched_at < ?", (now - page_ttl_hours * 3600,)
            ).rowcount
            removed += self.conn.execute(
                "DELETE FROM web_searches WHERE fetched_at < ?", (now - search_ttl_hours * 3600,)
            ).rowcount
            self.conn.commit()
            return int(removed or 0)

    # --- verification cache ------------------------------------------------
    def get_verification(self, email: str, ttl_hours: int = 720) -> Optional[VerificationResult]:
        with self._lock:
            cutoff = time.time() - max(0, ttl_hours) * 3600
            row = self.conn.execute(
                "SELECT * FROM verifications WHERE email = ? AND checked_at >= ?",
                (email.lower(), cutoff),
            ).fetchone()
            if row is None:
                return None
            return VerificationResult(
                status=str(row["status"] or "unknown"),
                provider=str(row["provider"] or ""),
                score=row["score"],
                sub_status=str(row["sub_status"] or ""),
                is_catch_all=bool(row["is_catch_all"]),
                is_disposable=bool(row["is_disposable"]),
                is_role=bool(row["is_role"]),
                free=bool(row["free"]),
                mx_found=None if row["mx_found"] is None else bool(row["mx_found"]),
                error=str(row["error"] or ""),
                checked_at=float(row["checked_at"] or 0.0),
                raw=json.loads(row["raw"]) if row["raw"] else {},
            )

    def put_verification(self, email: str, result: VerificationResult) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO verifications(email, status, provider, score, sub_status, "
                "is_catch_all, is_disposable, is_role, free, mx_found, error, raw, checked_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET status=excluded.status, provider=excluded.provider, "
                "score=excluded.score, sub_status=excluded.sub_status, is_catch_all=excluded.is_catch_all, "
                "is_disposable=excluded.is_disposable, is_role=excluded.is_role, free=excluded.free, "
                "mx_found=excluded.mx_found, error=excluded.error, raw=excluded.raw, "
                "checked_at=excluded.checked_at",
                (
                    email.lower(), result.status, result.provider, result.score, result.sub_status,
                    int(result.is_catch_all), int(result.is_disposable), int(result.is_role),
                    int(result.free),
                    None if result.mx_found is None else int(result.mx_found),
                    result.error, json.dumps(result.raw)[:20000], result.checked_at,
                ),
            )
            self.conn.commit()

    # --- web search cache --------------------------------------------------
    @staticmethod
    def _search_key(provider: str, query: str) -> str:
        return f"{provider}:{' '.join(query.lower().split())}"

    def get_search(self, provider: str, query: str, ttl_hours: int) -> Optional[dict]:
        with self._lock:
            cutoff = time.time() - max(0, ttl_hours) * 3600
            row = self.conn.execute(
                "SELECT payload FROM web_searches WHERE query_key = ? AND fetched_at >= ?",
                (self._search_key(provider, query), cutoff),
            ).fetchone()
            if row is None or not row["payload"]:
                return None
            try:
                return json.loads(row["payload"])
            except ValueError:
                return None

    def put_search(self, provider: str, query: str, payload: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO web_searches(query_key, provider, query, payload, fetched_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(query_key) DO UPDATE SET payload=excluded.payload, "
                "fetched_at=excluded.fetched_at",
                (self._search_key(provider, query), provider, query,
                 json.dumps(payload)[:200000], time.time()),
            )
            self.conn.commit()

    # --- maps results cache (a crash must never cost the same search twice) ---
    def get_maps(self, provider: str, query: str, ttl_hours: int) -> Optional[tuple[list[dict], bool]]:
        with self._lock:
            cutoff = time.time() - max(0, ttl_hours) * 3600
            row = self.conn.execute(
                "SELECT places, complete FROM maps_cache WHERE query_key = ? AND fetched_at >= ?",
                (self._search_key(provider, query), cutoff),
            ).fetchone()
            if row is None or not row["places"]:
                return None
            try:
                return json.loads(row["places"]), bool(row["complete"])
            except ValueError:
                return None

    def get_maps_any(self, query: str, ttl_hours: int) -> Optional[tuple[list[dict], bool]]:
        """Cached listings for a query from whichever provider fetched them."""
        with self._lock:
            cutoff = time.time() - max(0, ttl_hours) * 3600
            row = self.conn.execute(
                "SELECT places, complete FROM maps_cache WHERE query = ? AND fetched_at >= ? "
                "ORDER BY fetched_at DESC LIMIT 1", (query, cutoff),
            ).fetchone()
            if row is None or not row["places"]:
                return None
            try:
                return json.loads(row["places"]), bool(row["complete"])
            except ValueError:
                return None

    def put_maps(self, provider: str, query: str, places: list[dict], complete: bool) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO maps_cache(query_key, provider, query, places, complete, fetched_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(query_key) DO UPDATE SET places=excluded.places, "
                "complete=excluded.complete, fetched_at=excluded.fetched_at",
                (self._search_key(provider, query), provider, query, json.dumps(places, default=str),
                 int(complete), time.time()),
            )
            self.conn.commit()

    # --- domain facts ------------------------------------------------------
    def get_domain_facts(self, domain: str) -> Optional[tuple[Optional[bool], Optional[bool]]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT has_mx, is_catch_all FROM domain_facts WHERE domain = ?", (domain.lower(),)
            ).fetchone()
            if row is None:
                return None
            has_mx = None if row["has_mx"] is None else bool(row["has_mx"])
            catch_all = None if row["is_catch_all"] is None else bool(row["is_catch_all"])
            return has_mx, catch_all

    def put_domain_facts(
        self, domain: str, has_mx: Optional[bool] = None, is_catch_all: Optional[bool] = None
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO domain_facts(domain, has_mx, is_catch_all, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(domain) DO UPDATE SET "
                "has_mx=COALESCE(excluded.has_mx, domain_facts.has_mx), "
                "is_catch_all=COALESCE(excluded.is_catch_all, domain_facts.is_catch_all), "
                "updated_at=excluded.updated_at",
                (
                    domain.lower(),
                    None if has_mx is None else int(has_mx),
                    None if is_catch_all is None else int(is_catch_all),
                    time.time(),
                ),
            )
            self.conn.commit()

    # --- results -----------------------------------------------------------
    def save_businesses(self, results: Sequence[BusinessResult], run_id: str, stage: str = "done") -> None:
        """Save a whole batch in one transaction (one fsync instead of one per business)."""
        with self._lock:
            for result in results:
                self.save_business(result, run_id, stage, commit=False)
            self.conn.commit()

    def save_business(self, result: BusinessResult, run_id: str, stage: str = "done",
                      commit: bool = True) -> None:
        with self._lock:
            place = result.place
            key = place.dedupe_key()
            owner = result.owner
            self.conn.execute(
                "INSERT INTO businesses(key, run_id, query, name, place_id, category, address, city, "
                "state, postal_code, phone, website, domain, rating, reviews, latitude, longitude, "
                "google_url, is_chain, chain_score, chain_reasons, website_status, pages_crawled, "
                "domain_has_mx, permutations_skipped_reason, notes, place_raw, updated_at, "
                "stage, website_source, website_confidence, owner_name, owner_title, owner_source, "
                "owner_confidence, chain_kind, target_role, domain_is_catch_all) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET run_id=excluded.run_id, query=excluded.query, "
                "name=excluded.name, website=excluded.website, domain=excluded.domain, "
                "is_chain=excluded.is_chain, chain_score=excluded.chain_score, "
                "chain_reasons=excluded.chain_reasons, website_status=excluded.website_status, "
                "pages_crawled=excluded.pages_crawled, domain_has_mx=excluded.domain_has_mx, "
                "permutations_skipped_reason=excluded.permutations_skipped_reason, "
                "notes=excluded.notes, updated_at=excluded.updated_at, stage=excluded.stage, "
                "website_source=excluded.website_source, "
                "website_confidence=excluded.website_confidence, owner_name=excluded.owner_name, "
                "owner_title=excluded.owner_title, owner_source=excluded.owner_source, "
                "owner_confidence=excluded.owner_confidence, chain_kind=excluded.chain_kind, "
                "target_role=excluded.target_role, domain_is_catch_all=excluded.domain_is_catch_all",
                (
                    key, run_id, place.query, place.name, place.place_id, place.category,
                    place.address, place.city, place.state, place.postal_code, place.phone,
                    place.website, place.domain, place.rating, place.reviews, place.latitude,
                    place.longitude, place.google_url, int(result.is_chain), result.chain_score,
                    json.dumps(result.chain_reasons), result.website_status,
                    json.dumps(result.pages_crawled[:20]),
                    None if result.domain_has_mx is None else int(result.domain_has_mx),
                    result.permutations_skipped_reason, json.dumps(result.notes),
                    json.dumps(place.raw)[:40000], time.time(),
                    stage, result.website_source, result.website_confidence,
                    owner.name if owner else None, owner.title if owner else None,
                    owner.source if owner else None, owner.confidence if owner else None,
                    result.chain_kind, result.target_role,
                    None if result.domain_is_catch_all is None else int(result.domain_is_catch_all),
                ),
            )
            # Replace the address list wholesale: candidates dropped by filtering
            # must not linger from an earlier save.
            self.conn.execute("DELETE FROM emails WHERE business_key = ?", (key,))
            for candidate in result.emails:
                verification = candidate.verification
                self.conn.execute(
                    "INSERT INTO emails(business_key, email, source, source_url, pattern, context, "
                    "is_role, is_personal_domain, is_low_value, on_business_domain, status, "
                    "sub_status, provider, score, confidence, notes, updated_at, "
                    "contact_type, contact_name, contact_title, lead_eligible) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key, candidate.email, candidate.source, candidate.source_url,
                        candidate.pattern, candidate.context[:300], int(candidate.is_role),
                        int(candidate.is_personal_domain), int(candidate.is_low_value),
                        int(candidate.on_business_domain), candidate.status,
                        verification.sub_status if verification else "",
                        verification.provider if verification else "",
                        verification.score if verification else None,
                        candidate.confidence, json.dumps(candidate.notes), time.time(),
                        candidate.contact_type, candidate.contact_name, candidate.contact_title,
                        int(candidate.lead_eligible),
                    ),
                )
            if commit:
                self.conn.commit()

    def done_business_keys(self, run_id: str) -> set[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT key FROM businesses WHERE run_id = ? AND stage = 'done'", (run_id,)
            ).fetchall()
            return {str(r["key"]) for r in rows}

    def load_business(self, key: str) -> Optional[BusinessResult]:
        """Rebuild a finished business - enough to export and publish it again."""
        with self._lock:
            row = self.conn.execute("SELECT * FROM businesses WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            email_rows = self.conn.execute(
                "SELECT * FROM emails WHERE business_key = ? ORDER BY confidence DESC", (key,)
            ).fetchall()
        try:
            raw = json.loads(row["place_raw"] or "{}")
        except ValueError:
            raw = {}
        place = Place(
            name=row["name"] or "", query=row["query"] or "", source="db",
            place_id=row["place_id"] or "", category=row["category"] or "",
            address=row["address"] or "", city=row["city"] or "", state=row["state"] or "",
            postal_code=row["postal_code"] or "", phone=row["phone"] or "",
            website=row["website"] or "", domain=row["domain"] or "",
            latitude=row["latitude"], longitude=row["longitude"], rating=row["rating"],
            reviews=row["reviews"], google_url=row["google_url"] or "", raw=raw,
        )
        owner = None
        if row["owner_name"]:
            owner = Person(name=row["owner_name"], title=row["owner_title"] or "",
                           source=row["owner_source"] or "", confidence=row["owner_confidence"] or 0)
        result = BusinessResult(
            place=place, is_chain=bool(row["is_chain"]), chain_score=row["chain_score"] or 0,
            chain_reasons=json.loads(row["chain_reasons"] or "[]"),
            chain_kind=row["chain_kind"] or "", target_role=row["target_role"] or "",
            website_status=row["website_status"] or "", website_source=row["website_source"] or "",
            website_confidence=row["website_confidence"] or 0, owner=owner,
            pages_crawled=json.loads(row["pages_crawled"] or "[]"),
            domain_has_mx=None if row["domain_has_mx"] is None else bool(row["domain_has_mx"]),
            domain_is_catch_all=None if row["domain_is_catch_all"] is None else bool(row["domain_is_catch_all"]),
            permutations_skipped_reason=row["permutations_skipped_reason"] or "",
            notes=json.loads(row["notes"] or "[]"),
        )
        for e in email_rows:
            verification = None
            if e["status"] and e["status"] != "skipped":
                verification = VerificationResult(status=e["status"], provider=e["provider"] or "",
                                                  score=e["score"], sub_status=e["sub_status"] or "")
            result.emails.append(EmailCandidate(
                email=e["email"], source=e["source"] or "", source_url=e["source_url"] or "",
                pattern=e["pattern"] or "", context=e["context"] or "",
                is_role=bool(e["is_role"]), is_personal_domain=bool(e["is_personal_domain"]),
                is_low_value=bool(e["is_low_value"]), on_business_domain=bool(e["on_business_domain"]),
                verification=verification, confidence=e["confidence"] or 0,
                contact_type=e["contact_type"] or "general", contact_name=e["contact_name"] or "",
                contact_title=e["contact_title"] or "",
                lead_eligible=bool(e["lead_eligible"]) if e["lead_eligible"] is not None else True,
                notes=json.loads(e["notes"] or "[]"),
            ))
        return result

    def add_run_queries(self, run_id: str, queries: list[str]) -> None:
        with self._lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO run_queries(run_id, query, status, found, updated_at) "
                "VALUES(?,?,'pending',0,?)",
                [(run_id, q, time.time()) for q in queries],
            )
            self.conn.commit()

    def mark_queries_done(self, run_id: str, queries: list[str], found: dict[str, int]) -> None:
        with self._lock:
            self.conn.executemany(
                "UPDATE run_queries SET status='done', found=?, updated_at=? WHERE run_id=? AND query=?",
                [(found.get(q, 0), time.time(), run_id, q) for q in queries],
            )
            self.conn.commit()

    def done_queries(self, run_id: str) -> set[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT query FROM run_queries WHERE run_id = ? AND status = 'done'", (run_id,)
            ).fetchall()
            return {str(r["query"]) for r in rows}

    def run_counters(self, run_id: str) -> dict[str, int]:
        """The run's headline numbers straight from the database - what a
        resumed large run reports without holding results in memory."""
        with self._lock:
            def count(sql: str, *params: Any) -> int:
                return int(self.conn.execute(sql, params).fetchone()[0])

            return {
                "businesses": count("SELECT COUNT(*) FROM businesses WHERE run_id=? AND stage='done'", run_id),
                "with_website": count("SELECT COUNT(*) FROM businesses WHERE run_id=? AND stage='done' AND website<>''", run_id),
                "websites_discovered": count("SELECT COUNT(*) FROM businesses WHERE run_id=? AND website_source='search' AND website<>''", run_id),
                "owners_found": count("SELECT COUNT(*) FROM businesses WHERE run_id=? AND owner_name IS NOT NULL AND owner_name<>''", run_id),
                "owners_from_site": count("SELECT COUNT(*) FROM businesses WHERE run_id=? AND owner_source LIKE 'site%'", run_id),
                "owners_from_search": count("SELECT COUNT(*) FROM businesses WHERE run_id=? AND owner_source LIKE 'search%'", run_id),
                "chains_flagged": count("SELECT COUNT(*) FROM businesses WHERE run_id=? AND is_chain=1", run_id),
                "with_any_email": count(
                    "SELECT COUNT(DISTINCT b.key) FROM businesses b JOIN emails e ON e.business_key=b.key "
                    "WHERE b.run_id=? AND e.lead_eligible=1", run_id),
                "owner_emails": count(
                    "SELECT COUNT(DISTINCT b.key) FROM businesses b JOIN emails e ON e.business_key=b.key "
                    "WHERE b.run_id=? AND e.lead_eligible=1 AND e.contact_type IN ('owner','manager')", run_id),
                "best_email_verified_valid": count(
                    "SELECT COUNT(DISTINCT b.key) FROM businesses b JOIN emails e ON e.business_key=b.key "
                    "WHERE b.run_id=? AND e.lead_eligible=1 AND e.status='valid'", run_id),
                "total_emails": count(
                    "SELECT COUNT(*) FROM emails e JOIN businesses b ON e.business_key=b.key WHERE b.run_id=?", run_id),
            }

    def deferred_guess_keys(self, run_id: str) -> list[str]:
        """Businesses of a run whose guessed addresses were planned but not yet
        checked - the ones that name an owner first (their guesses are the
        most valuable), the most confidently named owner first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT b.key, "
                "MAX(CASE WHEN e.contact_type IN ('owner','manager') THEN 1 ELSE 0 END) AS owner_guess, "
                "COALESCE(b.owner_confidence, 0) AS oc "
                "FROM businesses b JOIN emails e ON e.business_key = b.key "
                "WHERE b.run_id = ? AND e.notes LIKE '%deferred_guess%' "
                "GROUP BY b.key ORDER BY owner_guess DESC, oc DESC, b.updated_at",
                (run_id,),
            ).fetchall()
        return [str(r["key"]) for r in rows]

    def iter_run_businesses(self, run_id: str):
        """Stream every finished business of a run (for re-exporting on resume)."""
        with self._lock:
            keys = [str(r["key"]) for r in self.conn.execute(
                "SELECT key FROM businesses WHERE run_id = ? AND stage = 'done' ORDER BY updated_at", (run_id,)
            ).fetchall()]
        for key in keys:
            result = self.load_business(key)
            if result is not None:
                yield result

    def set_run_state(self, run_id: str, status: str, *, total: Optional[int] = None,
                      done: Optional[int] = None, run_table: Optional[str] = None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE runs SET status = ?, total = COALESCE(?, total), done = COALESCE(?, done), "
                "run_table = COALESCE(?, run_table), updated_at = ? WHERE run_id = ?",
                (status, total, done, run_table, time.time(), run_id),
            )
            self.conn.commit()

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT run_id, started_at, finished_at, queries, status, total, done, run_table "
                "FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            try:
                queries = json.loads(r["queries"] or "[]")
            except ValueError:
                queries = []
            out.append({"run_id": r["run_id"], "started_at": r["started_at"] or 0.0,
                        "finished_at": r["finished_at"], "queries": queries,
                        "status": r["status"] or ("done" if r["finished_at"] else "unknown"),
                        "total": r["total"] or 0, "done": r["done"] or 0,
                        "run_table": r["run_table"] or ""})
        return out

    def latest_unfinished_run(self) -> Optional[dict]:
        for run in self.list_runs(50):
            if run["status"] in ("running", "interrupted", "failed"):
                return run
        return None

    def seen_business_keys(self, run_id: Optional[str] = None) -> set[str]:
        with self._lock:
            if run_id:
                rows = self.conn.execute(
                    "SELECT key FROM businesses WHERE run_id = ?", (run_id,)
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT key FROM businesses").fetchall()
            return {str(r["key"]) for r in rows}

    def start_run(self, run_id: str, queries: Iterable[str], settings: dict[str, Any]) -> None:
        with self._lock:
            safe_settings = {
                k: ("***" if ("key" in k or "token" in k) and v else v)
                for k, v in settings.items()
            }
            self.conn.execute(
                "INSERT OR REPLACE INTO runs(run_id, started_at, queries, settings) VALUES(?,?,?,?)",
                (run_id, time.time(), json.dumps(list(queries)), json.dumps(safe_settings, default=str)),
            )
            self.conn.commit()

    def finish_run(self, run_id: str, stats: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, stats = ?, status = 'done' WHERE run_id = ?",
                (time.time(), json.dumps(stats, default=str), run_id),
            )
            self.conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            def count(sql: str) -> int:
                return int(self.conn.execute(sql).fetchone()[0])

            return {
                "businesses": count("SELECT COUNT(*) FROM businesses"),
                "emails": count("SELECT COUNT(*) FROM emails"),
                "valid_emails": count("SELECT COUNT(*) FROM emails WHERE status='valid'"),
                "cached_pages": count("SELECT COUNT(*) FROM pages"),
                "cached_verifications": count("SELECT COUNT(*) FROM verifications"),
            "cached_searches": count("SELECT COUNT(*) FROM web_searches"),
            "cached_maps_queries": count("SELECT COUNT(*) FROM maps_cache"),
            }

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
