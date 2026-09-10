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
from pathlib import Path
from typing import Any, Iterable, Optional

from ..models import BusinessResult, VerificationResult

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
        self.conn.commit()

    # --- page cache --------------------------------------------------------
    def get_page(self, url: str, ttl_hours: int) -> Optional[tuple[int, str, str]]:
        with self._lock:
            cutoff = time.time() - max(0, ttl_hours) * 3600
            row = self.conn.execute(
                "SELECT status, final_url, html FROM pages WHERE url = ? AND fetched_at >= ?",
                (url, cutoff),
            ).fetchone()
            if row is None:
                return None
            return int(row["status"] or 0), str(row["final_url"] or ""), str(row["html"] or "")

    def put_page(self, url: str, status: int, final_url: str, html: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO pages(url, final_url, status, html, fetched_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET final_url=excluded.final_url, "
                "status=excluded.status, html=excluded.html, fetched_at=excluded.fetched_at",
                (url, final_url, status, html, time.time()),
            )
            self.conn.commit()

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
    def save_business(self, result: BusinessResult, run_id: str) -> None:
        with self._lock:
            place = result.place
            key = place.dedupe_key()
            self.conn.execute(
                "INSERT INTO businesses(key, run_id, query, name, place_id, category, address, city, "
                "state, postal_code, phone, website, domain, rating, reviews, latitude, longitude, "
                "google_url, is_chain, chain_score, chain_reasons, website_status, pages_crawled, "
                "domain_has_mx, permutations_skipped_reason, notes, place_raw, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET run_id=excluded.run_id, query=excluded.query, "
                "name=excluded.name, website=excluded.website, domain=excluded.domain, "
                "is_chain=excluded.is_chain, chain_score=excluded.chain_score, "
                "chain_reasons=excluded.chain_reasons, website_status=excluded.website_status, "
                "pages_crawled=excluded.pages_crawled, domain_has_mx=excluded.domain_has_mx, "
                "permutations_skipped_reason=excluded.permutations_skipped_reason, "
                "notes=excluded.notes, updated_at=excluded.updated_at",
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
                ),
            )
            for candidate in result.emails:
                verification = candidate.verification
                self.conn.execute(
                    "INSERT INTO emails(business_key, email, source, source_url, pattern, context, "
                    "is_role, is_personal_domain, is_low_value, on_business_domain, status, "
                    "sub_status, provider, score, confidence, notes, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(business_key, email) DO UPDATE SET source=excluded.source, "
                    "source_url=excluded.source_url, status=excluded.status, "
                    "sub_status=excluded.sub_status, provider=excluded.provider, "
                    "score=excluded.score, confidence=excluded.confidence, notes=excluded.notes, "
                    "updated_at=excluded.updated_at",
                    (
                        key, candidate.email, candidate.source, candidate.source_url,
                        candidate.pattern, candidate.context[:300], int(candidate.is_role),
                        int(candidate.is_personal_domain), int(candidate.is_low_value),
                        int(candidate.on_business_domain), candidate.status,
                        verification.sub_status if verification else "",
                        verification.provider if verification else "",
                        verification.score if verification else None,
                        candidate.confidence, json.dumps(candidate.notes), time.time(),
                    ),
                )
            self.conn.commit()

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
                "UPDATE runs SET finished_at = ?, stats = ? WHERE run_id = ?",
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
