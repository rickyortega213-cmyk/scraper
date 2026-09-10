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
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import httpx

from ..models import BusinessResult
from .sinks import STATUS_QUEUED, email_records, lead_records

log = logging.getLogger(__name__)

FLUSH_INTERVAL = 2.0     # seconds between writes while a stage is running
BATCH_SIZE = 100         # rows per PostgREST call
QUEUE_TIMEOUT = 0.25


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SinkStats:
    leads_written: int = 0
    emails_written: int = 0
    run_rows_written: int = 0
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
    access_token: str = ""      # Management API token (sbp_...) - lets us create tables

    def __post_init__(self) -> None:
        # People paste REST endpoints and table links; only the project origin matters.
        from ..keys import clean_value

        self.url = clean_value("SUPABASE_URL", self.url)
        self.key = (self.key or "").strip()
        self.access_token = (self.access_token or "").strip()

    @property
    def rest_url(self) -> str:
        return self.url.rstrip("/") + "/rest/v1"

    @property
    def project_ref(self) -> str:
        """'https://abcdefgh.supabase.co' -> 'abcdefgh'"""
        host = self.url.replace("https://", "").replace("http://", "").split("/")[0]
        return host.split(".")[0] if host.endswith(".supabase.co") else ""

    @property
    def dashboard_url(self) -> str:
        return f"https://supabase.com/dashboard/project/{self.project_ref}" if self.project_ref else ""

    @property
    def table_editor_url(self) -> str:
        return f"{self.dashboard_url}/editor" if self.dashboard_url else ""

    @property
    def sql_editor_url(self) -> str:
        return f"{self.dashboard_url}/sql/new" if self.dashboard_url else ""

    def table(self, name: str) -> str:
        return f"{self.prefix}{name}"

    def problems(self) -> list[str]:
        """Why this config cannot work, in plain words (empty = looks fine)."""
        out: list[str] = []
        for label, value, env in (("Supabase URL", self.url, "SUPABASE_URL"),
                                  ("Supabase key", self.key, "SUPABASE_KEY"),
                                  ("Supabase access token", self.access_token, "SUPABASE_ACCESS_TOKEN")):
            bad = sum(1 for ch in value if ord(ch) > 126 or ord(ch) < 32)
            if bad:
                out.append(f"the {label} on file contains {bad} character(s) that cannot be in a "
                           f"key (a copy/paste went wrong) - copy it again from the Supabase "
                           f"dashboard and run: scraper keys set {env}=<paste>")
        if not self.url:
            out.append("no Supabase URL on file")
        if not self.key and not self.access_token:
            out.append("no Supabase key on file")
        return out

    def headers(self) -> dict[str, str]:
        problems = self.problems()
        if problems:
            raise SupabaseError(problems[0])
        headers = {
            "apikey": self.key,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        # Legacy service_role keys are JWTs and also go in Authorization; the
        # newer sb_secret_... keys are accepted through apikey alone.
        if self.key.count(".") == 2:
            headers["Authorization"] = f"Bearer {self.key}"
        if self.schema and self.schema != "public":
            headers["Content-Profile"] = self.schema
            headers["Accept-Profile"] = self.schema
        return headers


class SupabaseSink:
    """Best-effort live mirror of the run into Supabase."""

    name = "supabase"

    def __init__(self, config: SupabaseConfig, *, client: Optional[httpx.Client] = None,
                 run_table: str = "") -> None:
        self.config = config
        self.run_table = run_table          # per-run table (created by the caller), or ""
        self.stats = SinkStats()
        self._client = client or httpx.Client(timeout=config.timeout)
        self._owns_client = client is None
        # Rows waiting to be written, latest version per id. A business goes
        # through five statuses; keeping one row per business instead of one
        # per status bounds memory by what is in flight, not by how fast the
        # run goes or how slow Supabase answers.
        self._pending: dict[str, dict[str, dict[str, Any]]] = {}
        self._cv = threading.Condition()
        self._rush = False
        self._stopping = False
        self._writing = False
        self._run_id = ""
        self._thread = threading.Thread(
            target=self._writer_loop, name="supabase-sink", daemon=True
        )
        self._thread.start()

    # --- public API --------------------------------------------------------
    def start_run(self, run_id: str, meta: dict[str, Any]) -> None:
        self._run_id = run_id
        self._run_label = run_label(meta.get("queries", []))
        self._enqueue("runs", [{
            "run_id": run_id,
            "run_label": self._run_label,
            "started_at": _now(),
            "queries": meta.get("queries", []),
            "maps_provider": meta.get("maps_provider", ""),
            "verify_provider": meta.get("verify_provider", ""),
            "stats": {},
        }])

    def upsert(self, results: Sequence[BusinessResult], status: str) -> None:
        if not results:
            return
        leads = [
            {**rec, "run_label": getattr(self, "_run_label", "")}
            for r in results for rec in lead_records(r, self._run_id, status)
        ]
        self._enqueue("leads", leads, urgent=(status == STATUS_QUEUED))
        if self.run_table:
            self._enqueue(self.run_table, [
                {**_run_row(rec), "status": status} for rec in leads
            ], raw_table=True)
        # Only publish addresses once they exist; `queued` rows have none yet.
        emails = [rec for r in results for rec in email_records(r, self._run_id)]
        if emails:
            self._enqueue("emails", emails)

    FLUSH_TIMEOUT = 180.0

    def pending_rows(self) -> int:
        with self._cv:
            return sum(len(rows) for rows in self._pending.values())

    def flush(self) -> None:
        """Block until everything buffered so far has been written - but never
        forever: a wedged network must not wedge the scrape."""
        deadline = time.monotonic() + self.FLUSH_TIMEOUT
        with self._cv:
            self._rush = True
            self._cv.notify_all()
            while (self._pending or self._writing) and time.monotonic() < deadline:
                if not self._thread.is_alive():
                    self._record_failure("writer thread stopped", self.pending_rows_locked())
                    return
                self._cv.wait(timeout=0.1)
            if self._pending or self._writing:
                self._record_failure("flush timed out", self.pending_rows_locked())

    def pending_rows_locked(self) -> int:
        return sum(len(rows) for rows in self._pending.values())

    def finish_run(self, run_id: str, stats: dict[str, Any]) -> None:
        self._enqueue("runs", [{
            "run_id": run_id,
            "finished_at": _now(),
            "stats": stats,
        }])
        self.flush()

    def close(self) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        self._thread.join(timeout=30.0)
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "SupabaseSink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- writer thread -----------------------------------------------------
    def _enqueue(self, table: str, rows: list[dict[str, Any]], raw_table: bool = False,
                 urgent: bool = False) -> None:
        name = ("=" + table) if raw_table else table
        key = "run_id" if table == "runs" else "id"
        now = _now()
        with self._cv:
            bucket = self._pending.setdefault(name, {})
            for row in rows:
                ident = str(row.get(key) or "")
                previous = bucket.get(ident)
                merged = {**previous, **row} if previous else dict(row)
                merged["updated_at"] = now
                bucket[ident] = merged
            self._rush = self._rush or urgent      # freshly queued businesses: show them now
            self._cv.notify_all()

    def _writer_loop(self) -> None:
        """Write the buffered rows: at once when asked (urgent / flush / a full
        batch), otherwise every FLUSH_INTERVAL seconds while anything waits."""
        while True:
            with self._cv:
                waited = 0.0
                while not (self._stopping or self._rush or self._batch_full_locked()):
                    if self._pending and waited >= FLUSH_INTERVAL:
                        break
                    self._cv.wait(timeout=QUEUE_TIMEOUT)
                    waited += QUEUE_TIMEOUT
                batch, self._pending = self._pending, {}
                self._rush = False
                stopping = self._stopping
                self._writing = bool(batch)
            for table, rows in batch.items():
                try:
                    self._write(table, list(rows.values()))
                except Exception as exc:  # noqa: BLE001 - the thread must outlive any bug
                    self._record_failure(f"{type(exc).__name__}: {exc}", len(rows))
            with self._cv:
                self._writing = False
                self._cv.notify_all()
            if stopping:
                return

    def _batch_full_locked(self) -> bool:
        return any(len(rows) >= BATCH_SIZE for rows in self._pending.values())

    def _write(self, table: str, rows: list[dict[str, Any]]) -> None:
        rows = _dedupe_by_id(rows)
        # "=name" means an exact table name (the per-run table); otherwise prefixed.
        full = table[1:] if table.startswith("=") else self.config.table(table)
        url = f"{self.config.rest_url}/{full}"
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
            elif table.startswith("="):
                self.stats.run_rows_written += len(chunk)

    def _record_failure(self, detail: str, rows: int) -> None:
        self.stats.failures += 1
        self.stats.last_error = detail
        # Warn once per distinct problem; a dead database must not spam the log.
        log.warning("supabase write failed (%d rows): %s", rows, detail)


def run_label(queries: list[str]) -> str:
    """'dentist in austin tx · 2026-09-10' (+ N more) - how a run shows in the table."""
    from datetime import date

    queries = [q for q in queries if q and q != "*"]
    head = queries[0] if queries else "enrich"
    extra = f" +{len(queries) - 1} more" if len(queries) > 1 else ""
    return f"{head}{extra} · {date.today().isoformat()}"


MANAGEMENT_API = "https://api.supabase.com/v1/projects/{ref}/database/query"
MANAGEMENT_PROJECTS = "https://api.supabase.com/v1/projects"
MANAGEMENT_KEYS = "https://api.supabase.com/v1/projects/{ref}/api-keys"


def _management_get(token: str, url: str, params: Optional[dict[str, Any]] = None) -> Any:
    with httpx.Client(timeout=30.0) as client:
        response = client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
    if response.status_code in (401, 403):
        raise SupabaseError(
            "Supabase rejected the access token. Make one at "
            "https://supabase.com/dashboard/account/tokens (it starts with sbp_)."
        )
    if response.status_code >= 400:
        raise SupabaseError(f"{url} -> HTTP {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except ValueError as exc:
        raise SupabaseError(f"{url} returned non-JSON") from exc


def list_projects(token: str) -> list[dict[str, str]]:
    """[{ref, name, region}] for the account behind the token."""
    payload = _management_get(token, MANAGEMENT_PROJECTS)
    projects = payload if isinstance(payload, list) else payload.get("projects") or payload.get("data") or []
    out: list[dict[str, str]] = []
    for item in projects:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("id") or item.get("ref") or "")
        if ref:
            out.append({"ref": ref, "name": str(item.get("name") or ref),
                        "region": str(item.get("region") or "")})
    return out


def service_role_key(token: str, ref: str) -> str:
    payload = _management_get(token, MANAGEMENT_KEYS.format(ref=ref), params={"reveal": "true"})
    keys = payload if isinstance(payload, list) else payload.get("keys") or payload.get("data") or []
    for item in keys:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("id") or "").lower()
        value = str(item.get("api_key") or item.get("apiKey") or item.get("key") or "")
        if value and ("service_role" in name or name == "service"):
            return value
    raise SupabaseError(
        f"no service_role key found for project {ref}; the token may lack permission"
    )


def resolve_from_token(token: str, preferred_ref: str = "") -> SupabaseConfig:
    """Everything the sink needs, from the access token alone.

    Returns a config whose `url` and `key` were looked up; the caller may
    save them so later runs skip these two calls.
    """
    projects = list_projects(token)
    if not projects:
        raise SupabaseError("the token's account has no Supabase projects - create one first")
    chosen = next((p for p in projects if p["ref"] == preferred_ref), None) if preferred_ref else None
    if chosen is None:
        if len(projects) > 1 and not preferred_ref:
            names = ", ".join(f"{p['name']} ({p['ref']})" for p in projects)
            raise SupabaseError(
                f"the token can see {len(projects)} projects: {names}. "
                "Set SUPABASE_PROJECT_REF to the one to use."
            )
        chosen = projects[0]
    key = service_role_key(token, chosen["ref"])
    return SupabaseConfig(
        url=f"https://{chosen['ref']}.supabase.co", key=key, access_token=token,
    )


def run_table_name(label: str, prefix: str = "") -> str:
    """'dentist in austin tx · 2026-09-10' -> 'run_2026_09_10_dentist_in_austin_tx'."""
    import re

    if " · " in label:
        text, date_part = label.rsplit(" · ", 1)
    else:
        text, date_part = label, ""
    text = re.sub(r"\s\+\d+\s+more$", "", text)          # "dentist ... +2 more"
    date_slug = date_part.replace("-", "_") if date_part else ""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if date_slug:
        name = f"run_{date_slug}_{slug}"
    elif slug.startswith("run_") or not slug:
        name = slug or "run"
    else:
        name = slug                                      # a name the user typed
    if name[0].isdigit():
        name = "run_" + name
    name = re.sub(r"_+", "_", name).strip("_")[:60]
    return f"{prefix}{name}"


def run_table_sql(table: str) -> str:
    """A per-run table with the clean columns - what a person reads."""
    return f"""
create table if not exists {table} (
    id                  text primary key,
    status              text,
    company_name        text,
    city                text,
    state               text,
    address             text,
    phone_number        text,
    verified_email      text,
    contact_first_name  text,
    contact_last_name   text,
    contact_title       text,
    business_type       text,
    contact_type        text,
    email               text,
    email_status        text,
    email_confidence    int,
    website             text,
    google_maps_link    text,
    rating              numeric,
    reviews             int,
    is_chain            text,
    search_query        text,
    updated_at          timestamptz default now()
);
alter table {table} enable row level security;
"""


def missing_tables(config: SupabaseConfig) -> list[str]:
    """Which of our tables do not exist yet (empty list = ready)."""
    missing: list[str] = []
    with httpx.Client(timeout=config.timeout) as client:
        for table in ("runs", "leads", "emails"):
            url = f"{config.rest_url}/{config.table(table)}"
            try:
                response = client.get(url, params={"select": "*", "limit": 1},
                                      headers=config.headers())
            except Exception as exc:
                raise SupabaseError(f"could not reach {url}: {exc}") from exc
            if response.status_code in (401, 403):
                raise SupabaseError(
                    f"Supabase rejected the key (HTTP {response.status_code}). Use the "
                    "service_role key from Project Settings → API."
                )
            if response.status_code == 404 or (
                response.status_code == 400 and "does not exist" in response.text
            ):
                missing.append(config.table(table))
    return missing


def apply_schema(config: SupabaseConfig, sql: str) -> None:
    """Create the tables through the Management API (needs an access token)."""
    if not config.access_token:
        raise SupabaseError("no SUPABASE_ACCESS_TOKEN - cannot create tables automatically")
    if not config.project_ref:
        raise SupabaseError(f"cannot work out the project ref from {config.url}")
    url = MANAGEMENT_API.format(ref=config.project_ref)
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            url, json={"query": sql},
            headers={"Authorization": f"Bearer {config.access_token}",
                     "Content-Type": "application/json"},
        )
    if response.status_code >= 400:
        raise SupabaseError(
            f"Management API refused to run the schema (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )


EXEC_FUNCTION = "gmscrape_exec"


def exec_function_sql() -> str:
    """A tightly scoped SQL runner so the project API key can create run tables.

    It only accepts statements that create/alter gmscrape_* or run_* objects,
    and is callable only with the service/secret key (never anon)."""
    return f"""
create or replace function {EXEC_FUNCTION}(sql text) returns void
language plpgsql security definer set search_path = public as $$
begin
  if sql !~* '^\\s*(create table if not exists|alter table|create index if not exists|create or replace view|create view)\\s+(public\\.)?(gmscrape_|run_)' then
    raise exception '{EXEC_FUNCTION} only manages gmscrape_* and run_* objects';
  end if;
  execute sql;
end $$;
revoke all on function {EXEC_FUNCTION}(text) from public;
revoke all on function {EXEC_FUNCTION}(text) from anon;
revoke all on function {EXEC_FUNCTION}(text) from authenticated;
"""


def bootstrap_sql(prefix: str = "gmscrape_") -> str:
    """The one-time paste: shared tables + the runner that makes later runs automatic."""
    return schema_sql(prefix) + "\n-- Lets gmscrape create a table per run with just the project API key.\n" + exec_function_sql()


def _statements(sql: str) -> list[str]:
    """Split SQL on ';' outside of $$ bodies and comments."""
    out: list[str] = []
    buffer: list[str] = []
    in_dollar = False
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buffer.append(line)
        if "$$" in line:
            in_dollar = not in_dollar if line.count("$$") % 2 else in_dollar
        if not in_dollar and stripped.endswith(";"):
            out.append("\n".join(buffer).strip().rstrip(";"))
            buffer = []
    if buffer:
        out.append("\n".join(buffer).strip().rstrip(";"))
    return [st for st in out if st]


def exec_via_rpc(config: SupabaseConfig, sql: str) -> None:
    """Run DDL through the project's own API key using the installed runner."""
    url = f"{config.rest_url}/rpc/{EXEC_FUNCTION}"
    headers = {**config.headers(), "Prefer": "return=minimal"}
    with httpx.Client(timeout=60.0) as client:
        for statement in _statements(sql):
            if statement.lower().startswith(("revoke", "create or replace function")):
                continue          # only the bootstrap paste may touch the runner itself
            response = client.post(url, json={"sql": statement}, headers=headers)
            if response.status_code == 404 or "PGRST202" in response.text:
                raise SupabaseError("runner_missing")
            if response.status_code >= 400:
                raise SupabaseError(
                    f"{EXEC_FUNCTION} refused a statement (HTTP {response.status_code}): "
                    f"{response.text[:200]}"
                )


def runner_installed(config: SupabaseConfig) -> bool:
    """Whether the bootstrap paste has been applied."""
    try:
        exec_via_rpc(config, "create table if not exists gmscrape__probe (id int)")
    except SupabaseError as exc:
        return str(exc) != "runner_missing"
    return True


def run_ddl(config: SupabaseConfig, sql: str) -> str:
    """Create things by whatever route is available. Returns the route used."""
    if config.key:
        try:
            exec_via_rpc(config, sql)
            return "project key"
        except SupabaseError as exc:
            if str(exc) != "runner_missing":
                raise
    if config.access_token:
        apply_schema(config, sql)
        return "access token"
    raise SupabaseError("bootstrap_needed")


def ensure_schema(config: SupabaseConfig, run_table: str = "") -> tuple[bool, str]:
    """Make sure the tables exist. Returns (ready, what_happened).

    Creates them through the Management API when an access token is saved;
    otherwise explains exactly where to paste the SQL. With `run_table`, a
    fresh per-run table is created as well (token required).
    """
    missing = missing_tables(config)
    if not missing and not run_table:
        return True, "tables present"
    sql = (schema_sql(config.prefix) if missing else "") + (run_table_sql(run_table) if run_table else "")
    try:
        route = run_ddl(config, sql)
    except SupabaseError as exc:
        if str(exc) != "bootstrap_needed":
            raise
        if not missing:
            return True, "tables present (paste `gmscrape supabase-init` once to get a table per run)"
        return False, (
            f"tables missing: {', '.join(missing)}. One-time setup: run "
            "`gmscrape supabase-init` and paste the SQL into "
            f"{config.sql_editor_url or 'the Supabase SQL editor'}, then every run "
            "creates its own table automatically."
        )
    still = missing_tables(config)
    if still:
        raise SupabaseError(f"schema applied but tables still missing: {', '.join(still)}")
    done = []
    if missing:
        done.append(f"created {', '.join(missing)}")
    if run_table:
        done.append(f"created {run_table}")
    return True, ("; ".join(done) + f" (via {route})") if done else "tables present"
    return False, (
        f"tables missing: {', '.join(missing)}. Either save a Supabase access token "
        "(`gmscrape setup --only supabase`; make one at "
        "https://supabase.com/dashboard/account/tokens) so they can be created for "
        "you, or paste `gmscrape supabase-init` into "
        f"{config.sql_editor_url or 'the SQL editor'} and run it."
    )


_RUN_ROW_KEYS = (
    "company_name", "phone_number", "verified_email", "contact_first_name",
    "contact_last_name", "contact_title", "business_type", "contact_type", "email",
    "email_status", "email_confidence", "website", "rating", "reviews",
)


def _run_row(record: dict[str, Any]) -> dict[str, Any]:
    """Project a leads record onto the per-run table's clean columns."""
    row = {key: record.get(key) for key in _RUN_ROW_KEYS}
    from ..format import smart_title

    row.update({
        "id": record["id"],
        "contact_type": smart_title(str(record.get("contact_type") or "")),
        "city": record.get("city_clean"),
        "state": record.get("state_clean"),
        "address": record.get("address_clean"),
        "google_maps_link": record.get("google_url"),
        "is_chain": "Yes" if record.get("is_chain") else "No",
        "search_query": record.get("query"),
    })
    return row


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
            except SupabaseError:
                raise
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
    run_label       text,
    started_at      timestamptz,
    finished_at     timestamptz,
    queries         jsonb,
    maps_provider   text,
    verify_provider text,
    stats           jsonb,
    updated_at      timestamptz default now()
);

-- One row per contact: `<business>|general` and, when found, `<business>|owner`.
create table if not exists {p}leads (
    id                          text primary key,
    business_id                 text,
    run_id                      text,
    run_label                   text,
    status                      text,
    -- clean, human-facing columns (title case, (956)-324-6856 phones)
    company_name                text,
    city_clean                  text,
    state_clean                 text,
    address_clean               text,
    phone_number                text,
    verified_email              text,
    contact_first_name          text,
    contact_last_name           text,
    business_type               text,
    contact_type                text,
    contact_name                text,
    contact_title               text,
    email                       text,
    email_source                text,
    email_status                text,
    email_confidence            int,
    name                        text not null,
    query                       text,
    category                    text,
    owner_name                  text,
    owner_title                 text,
    owner_source                text,
    owner_confidence            int,
    emails_found                int default 0,
    emails_guessed              int default 0,
    all_emails                  text,
    phone                       text,
    website                     text,
    website_source              text,
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
    contact_type       text,
    contact_name       text,
    lead_eligible      boolean default true,
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
create index if not exists {p}leads_business_idx on {p}leads(business_id);
create index if not exists {p}leads_email_idx    on {p}leads(email_status, email_confidence desc);
create index if not exists {p}leads_contact_idx  on {p}leads(contact_type);
create index if not exists {p}emails_lead_idx    on {p}emails(lead_id);
create index if not exists {p}emails_status_idx  on {p}emails(status);

-- The table you read: clean columns first, one row per contact.
create or replace view {p}table as
select
    company_name,
    city_clean          as city,
    state_clean         as state,
    address_clean       as address,
    phone_number,
    verified_email,
    contact_first_name,
    contact_last_name,
    contact_title,
    business_type,
    contact_type,
    email,
    email_status,
    email_confidence,
    website,
    google_url          as google_maps_link,
    rating, reviews,
    is_chain,
    status,
    run_label, run_id, query, updated_at
from {p}leads
order by run_label desc, company_name, contact_type desc;

-- Only the most recent run - the "current table".
create or replace view {p}latest as
select * from {p}table
where run_id = (select run_id from {p}runs order by started_at desc limit 1);

-- Live progress: how far the current run has got.
create or replace view {p}progress as
select run_id, status,
       count(distinct business_id) as businesses,
       count(email) as emails,
       count(*) filter (where email_status = 'valid') as verified_valid,
       count(*) filter (where contact_type in ('owner', 'manager') and email is not null) as person_emails
from {p}leads
group by run_id, status
order by run_id, status;

-- Upgrading from an earlier schema? These are safe to run on existing tables.
alter table {p}runs   add column if not exists run_label text;
alter table {p}leads  add column if not exists run_label text;
alter table {p}leads  add column if not exists company_name text;
alter table {p}leads  add column if not exists city_clean text;
alter table {p}leads  add column if not exists state_clean text;
alter table {p}leads  add column if not exists address_clean text;
alter table {p}leads  add column if not exists phone_number text;
alter table {p}leads  add column if not exists verified_email text;
alter table {p}leads  add column if not exists contact_first_name text;
alter table {p}leads  add column if not exists contact_last_name text;
alter table {p}leads  add column if not exists business_type text;
alter table {p}leads  add column if not exists business_id text;
alter table {p}leads  add column if not exists contact_type text;
alter table {p}leads  add column if not exists contact_name text;
alter table {p}leads  add column if not exists contact_title text;
alter table {p}leads  add column if not exists email text;
alter table {p}leads  add column if not exists email_source text;
alter table {p}leads  add column if not exists email_status text;
alter table {p}leads  add column if not exists email_confidence int;
alter table {p}leads  add column if not exists website_source text;
alter table {p}leads  add column if not exists owner_name text;
alter table {p}leads  add column if not exists owner_title text;
alter table {p}leads  add column if not exists owner_source text;
alter table {p}leads  add column if not exists owner_confidence int;
alter table {p}emails add column if not exists contact_type text;
alter table {p}emails add column if not exists contact_name text;
alter table {p}emails add column if not exists lead_eligible boolean default true;

-- Writes use the service_role key, which bypasses RLS. Keep RLS on so the
-- anon key cannot read your leads; add your own policies if you want to
-- expose them to a front end.
alter table {p}runs   enable row level security;
alter table {p}leads  enable row level security;
alter table {p}emails enable row level security;
"""
