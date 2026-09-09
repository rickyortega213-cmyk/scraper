"""Stream leads into Supabase while the run is still going.

Writes go through PostgREST (`/rest/v1/<table>` with `Prefer: resolution=merge-duplicates`),
so there is no extra dependency beyond httpx. Rows are keyed by the business's
dedupe key, which means a re-run updates the same row instead of duplicating it,
and the `status` column advances as the pipeline works:

    queued -> crawled -> guessed -> verified -> done

A background writer thread owns the HTTP calls, so the async crawler is never
blocked by a database round trip, and Supabase being slow or down can never
fail a scrape - errors are counted and reported, not raised.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import httpx

from ..models import BusinessResult
from .sinks import email_records, lead_record

log = logging.getLogger(__name__)

FLUSH_INTERVAL = 2.0     # seconds between writes while a stage is running
BATCH_SIZE = 100         # rows per PostgREST call
QUEUE_TIMEOUT = 0.25
SHUTDOWN = object()      # sentinel enqueued by close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SinkStats:
    leads_written: int = 0
    emails_written: int = 0
    requests: int = 0
    failures: int = 0
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "leads_written": self.leads_written,
            "emails_written": self.emails_written,
            "requests": self.requests,
            "failures": self.failures,
            "last_error": self.last_error,
        }


class SupabaseError(RuntimeError):
    """Raised only by the explicit connectivity check, never during a run."""


@dataclass
class SupabaseConfig:
    url: str
    key: str
    schema: str = "public"
    prefix: str = "gmscrape_"
    timeout: float = 20.0

    @property
    def rest_url(self) -> str:
        return self.url.rstrip("/") + "/rest/v1"

    def table(self, name: str) -> str:
        return f"{self.prefix}{name}"

    def headers(self) -> dict[str, str]:
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        if self.schema and self.schema != "public":
            headers["Content-Profile"] = self.schema
            headers["Accept-Profile"] = self.schema
        return headers


class SupabaseSink:
    """Best-effort live mirror of the run into Supabase."""

    name = "supabase"

    def __init__(self, config: SupabaseConfig, *, client: Optional[httpx.Client] = None) -> None:
        self.config = config
        self.stats = SinkStats()
        self._client = client or httpx.Client(timeout=config.timeout)
        self._owns_client = client is None
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._run_id = ""
        self._thread = threading.Thread(
            target=self._writer_loop, name="supabase-sink", daemon=True
        )
        self._thread.start()

    # --- public API --------------------------------------------------------
    def start_run(self, run_id: str, meta: dict[str, Any]) -> None:
        self._run_id = run_id
        self._enqueue("runs", [{
            "run_id": run_id,
            "started_at": _now(),
            "queries": meta.get("queries", []),
            "maps_provider": meta.get("maps_provider", ""),
            "verify_provider": meta.get("verify_provider", ""),
            "stats": {},
        }])

    def upsert(self, results: Sequence[BusinessResult], status: str) -> None:
        if not results:
            return
        leads = [lead_record(r, self._run_id, status) for r in results]
        self._enqueue("leads", leads)
        # Only publish addresses once they exist; `queued` rows have none yet.
        emails = [rec for r in results for rec in email_records(r, self._run_id)]
        if emails:
            self._enqueue("emails", emails)

    def flush(self) -> None:
        """Block until everything queued so far has been written."""
        self._queue.join()

    def finish_run(self, run_id: str, stats: dict[str, Any]) -> None:
        self._enqueue("runs", [{
            "run_id": run_id,
            "finished_at": _now(),
            "stats": stats,
        }])
        self.flush()

    def close(self) -> None:
        self._queue.put(SHUTDOWN)
        self._thread.join(timeout=30.0)
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "SupabaseSink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- writer thread -----------------------------------------------------
    def _enqueue(self, table: str, rows: list[dict[str, Any]]) -> None:
        stamped = [{**row, "updated_at": _now()} for row in rows]
        self._queue.put((table, stamped))

    def _writer_loop(self) -> None:
        """Drain the queue, coalescing rows per table before each write.

        Queue items are acknowledged only once their rows have actually been
        written, so `flush()` (a queue join) genuinely means "persisted".
        """
        pending: dict[str, list[dict[str, Any]]] = {}
        unacked = 0
        last_flush = time.monotonic()
        stopping = False

        while True:
            try:
                item = self._queue.get(timeout=QUEUE_TIMEOUT)
            except queue.Empty:
                item = None
            else:
                unacked += 1
                if item is SHUTDOWN:
                    stopping = True
                else:
                    table, rows = item
                    pending.setdefault(table, []).extend(rows)

            batch_full = any(len(rows) >= BATCH_SIZE for rows in pending.values())
            idle = item is None
            aged = time.monotonic() - last_flush >= FLUSH_INTERVAL
            due = stopping or batch_full or (bool(pending) and (idle or aged))

            if due:
                for table, rows in list(pending.items()):
                    self._write(table, rows)
                pending.clear()
                last_flush = time.monotonic()
                for _ in range(unacked):
                    self._queue.task_done()
                unacked = 0
            if stopping:
                return

    def _write(self, table: str, rows: list[dict[str, Any]]) -> None:
        rows = _dedupe_by_id(rows)
        url = f"{self.config.rest_url}/{self.config.table(table)}"
        for start in range(0, len(rows), BATCH_SIZE):
            chunk = rows[start: start + BATCH_SIZE]
            conflict = "run_id" if table == "runs" else "id"
            try:
                response = self._client.post(
                    url,
                    params={"on_conflict": conflict},
                    json=chunk,
                    headers=self.config.headers(),
                )
                self.stats.requests += 1
            except Exception as exc:
                self._record_failure(f"{type(exc).__name__}: {exc}", len(chunk))
                continue
            if response.status_code >= 400:
                self._record_failure(
                    f"HTTP {response.status_code}: {response.text[:200]}", len(chunk)
                )
                continue
            if table == "leads":
                self.stats.leads_written += len(chunk)
            elif table == "emails":
                self.stats.emails_written += len(chunk)

    def _record_failure(self, detail: str, rows: int) -> None:
        self.stats.failures += 1
        self.stats.last_error = detail
        # Warn once per distinct problem; a dead database must not spam the log.
        log.warning("supabase write failed (%d rows): %s", rows, detail)


def _dedupe_by_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PostgREST rejects a batch containing the same key twice - keep the last."""
    seen: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.get("id") or row.get("run_id") or id(row))
        if key in seen:
            seen[key].update(row)
        else:
            seen[key] = dict(row)
            ordered.append(seen[key])
    return ordered


def check_connection(config: SupabaseConfig) -> dict[str, Any]:
    """Verify credentials and that the tables exist. Raises SupabaseError."""
    results: dict[str, Any] = {}
    with httpx.Client(timeout=config.timeout) as client:
        for table in ("runs", "leads", "emails"):
            url = f"{config.rest_url}/{config.table(table)}"
            try:
                response = client.get(
                    url, params={"select": "*", "limit": 1},
                    headers={**config.headers(), "Prefer": "count=exact"},
                )
            except Exception as exc:
                raise SupabaseError(f"could not reach {url}: {exc}") from exc
            if response.status_code == 404 or (
                response.status_code == 400 and "does not exist" in response.text
            ):
                raise SupabaseError(
                    f"table {config.table(table)!r} does not exist - run "
                    "`gmscrape supabase-init --write schema.sql` and apply it "
                    "in the Supabase SQL editor"
                )
            if response.status_code in (401, 403):
                raise SupabaseError(
                    f"Supabase rejected the key for {config.table(table)!r} "
                    f"(HTTP {response.status_code}). Use the service_role key for "
                    "server-side writes, or add an RLS policy for the anon key."
                )
            if response.status_code >= 400:
                raise SupabaseError(
                    f"{url} -> HTTP {response.status_code}: {response.text[:200]}"
                )
            results[config.table(table)] = "ok"
    return results


def schema_sql(prefix: str = "gmscrape_") -> str:
    """DDL for the tables plus a Clay-style ordered view."""
    p = prefix
    return f"""-- gmscrape Supabase schema
-- Apply in the Supabase SQL editor (or `psql`), then:
--   SUPABASE_URL=https://<project>.supabase.co
--   SUPABASE_KEY=<service_role key>   # server-side only, bypasses RLS
--   gmscrape run "dentist in austin tx" --supabase

create table if not exists {p}runs (
    run_id          text primary key,
    started_at      timestamptz,
    finished_at     timestamptz,
    queries         jsonb,
    maps_provider   text,
    verify_provider text,
    stats           jsonb,
    updated_at      timestamptz default now()
);

create table if not exists {p}leads (
    id                          text primary key,
    run_id                      text,
    status                      text,
    name                        text not null,
    query                       text,
    category                    text,
    best_email                  text,
    best_email_source           text,
    best_email_status           text,
    best_email_confidence       int,
    emails_found                int default 0,
    emails_guessed              int default 0,
    all_emails                  text,
    phone                       text,
    website                     text,
    domain                      text,
    address                     text,
    city                        text,
    state                       text,
    postal_code                 text,
    rating                      numeric,
    reviews                     int,
    is_chain                    boolean default false,
    chain_reasons               text,
    website_status              text,
    domain_has_mx               boolean,
    domain_is_catch_all         boolean,
    permutations_skipped_reason text,
    pages_crawled               int default 0,
    place_id                    text,
    latitude                    numeric,
    longitude                   numeric,
    google_url                  text,
    notes                       text,
    updated_at                  timestamptz default now()
);

create table if not exists {p}emails (
    id                 text primary key,
    lead_id            text references {p}leads(id) on delete cascade,
    run_id             text,
    email              text not null,
    business_name      text,
    confidence         int,
    status             text,
    sub_status         text,
    provider           text,
    source             text,
    source_url         text,
    pattern            text,
    is_role            boolean,
    is_personal_domain boolean,
    on_business_domain boolean,
    domain             text,
    context            text,
    notes              text,
    updated_at         timestamptz default now()
);

create index if not exists {p}leads_run_idx      on {p}leads(run_id);
create index if not exists {p}leads_status_idx   on {p}leads(status);
create index if not exists {p}leads_domain_idx   on {p}leads(domain);
create index if not exists {p}leads_best_idx     on {p}leads(best_email_status, best_email_confidence desc);
create index if not exists {p}emails_lead_idx    on {p}emails(lead_id);
create index if not exists {p}emails_status_idx  on {p}emails(status);

-- The lead view, ordered the way you actually read it.
create or replace view {p}table as
select
    status,
    name              as business,
    best_email        as email,
    best_email_status as email_status,
    best_email_confidence as confidence,
    best_email_source as found_via,
    phone, website, city, state, category,
    reviews, rating,
    is_chain,
    emails_found, emails_guessed,
    website_status,
    permutations_skipped_reason as no_guess_reason,
    query, run_id, domain, all_emails, updated_at
from {p}leads
order by best_email_confidence desc nulls last, updated_at desc;

-- Live progress: how far the current run has got.
create or replace view {p}progress as
select run_id, status, count(*) as leads,
       count(best_email) as with_email,
       count(*) filter (where best_email_status = 'valid') as verified_valid
from {p}leads
group by run_id, status
order by run_id, status;

-- Writes use the service_role key, which bypasses RLS. Keep RLS on so the
-- anon key cannot read your leads; add your own policies if you want to
-- expose them to a front end.
alter table {p}runs   enable row level security;
alter table {p}leads  enable row level security;
alter table {p}emails enable row level security;
"""
