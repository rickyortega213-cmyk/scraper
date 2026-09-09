"""Live result sinks.

A sink receives businesses as the pipeline works on them, so an external table
fills in while the run is still going instead of at the end. The pipeline calls
`upsert` at each stage boundary with a status, and a sink is free to batch.

A sink must never break a run: implementations swallow their own transport
errors and count them, and the pipeline treats a sink as best-effort.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from ..models import BusinessResult
from .export import business_row, email_rows

# Status values, in the order a lead moves through them.
STATUS_QUEUED = "queued"
STATUS_CRAWLED = "crawled"
STATUS_GUESSED = "guessed"
STATUS_VERIFIED = "verified"
STATUS_DONE = "done"

STATUS_ORDER = (
    STATUS_QUEUED, STATUS_CRAWLED, STATUS_GUESSED, STATUS_VERIFIED, STATUS_DONE,
)


@runtime_checkable
class LeadSink(Protocol):
    """Somewhere results are published to while a run is in flight."""

    def start_run(self, run_id: str, meta: dict[str, Any]) -> None: ...

    def upsert(self, results: Sequence[BusinessResult], status: str) -> None: ...

    def flush(self) -> None: ...

    def finish_run(self, run_id: str, stats: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


def lead_id(result: BusinessResult) -> str:
    """Stable primary key for a business across runs and overlapping queries."""
    return result.place.dedupe_key()


def lead_record(result: BusinessResult, run_id: str, status: str) -> dict[str, Any]:
    """One row for the leads table, reusing the CSV column shapes."""
    row = business_row(result)
    best = result.best_email

    def blank_to_none(value: Any) -> Any:
        return None if value == "" else value

    return {
        "id": lead_id(result),
        "run_id": run_id,
        "status": status,
        "name": row["name"],
        "query": row["query"],
        "category": blank_to_none(row["category"]),
        "best_email": blank_to_none(row["best_email"]),
        "best_email_source": blank_to_none(row["best_email_source"]),
        "best_email_status": blank_to_none(row["best_email_status"]),
        "best_email_confidence": best.confidence if best else None,
        "emails_found": row["emails_found"],
        "emails_guessed": row["emails_guessed"],
        "all_emails": blank_to_none(row["all_emails"]),
        "phone": blank_to_none(row["phone"]),
        "website": blank_to_none(row["website"]),
        "domain": blank_to_none(row["domain"]),
        "address": blank_to_none(row["address"]),
        "city": blank_to_none(row["city"]),
        "state": blank_to_none(row["state"]),
        "postal_code": blank_to_none(row["postal_code"]),
        "rating": blank_to_none(row["rating"]),
        "reviews": blank_to_none(row["reviews"]),
        "is_chain": result.is_chain,
        "chain_reasons": blank_to_none(row["chain_reasons"]),
        "website_status": blank_to_none(row["website_status"]),
        "domain_has_mx": result.domain_has_mx,
        "domain_is_catch_all": result.domain_is_catch_all,
        "permutations_skipped_reason": blank_to_none(row["permutations_skipped_reason"]),
        "pages_crawled": row["pages_crawled"],
        "place_id": blank_to_none(row["place_id"]),
        "latitude": blank_to_none(row["latitude"]),
        "longitude": blank_to_none(row["longitude"]),
        "google_url": blank_to_none(row["google_url"]),
        "notes": blank_to_none(row["notes"]),
    }


def email_records(result: BusinessResult, run_id: str) -> list[dict[str, Any]]:
    """One row per candidate address, keyed so re-runs update rather than duplicate."""
    parent = lead_id(result)
    records: list[dict[str, Any]] = []
    for row in email_rows(result):
        records.append({
            "id": f"{parent}|{row['email']}",
            "lead_id": parent,
            "run_id": run_id,
            "email": row["email"],
            "business_name": row["business_name"],
            "confidence": row["confidence"],
            "status": row["status"],
            "sub_status": row["sub_status"] or None,
            "provider": row["provider"] or None,
            "source": row["source"],
            "source_url": row["source_url"] or None,
            "pattern": row["pattern"] or None,
            "is_role": row["is_role"] == "yes",
            "is_personal_domain": row["is_personal_domain"] == "yes",
            "on_business_domain": row["on_business_domain"] == "yes",
            "domain": row["domain"],
            "context": row["context"] or None,
            "notes": row["notes"] or None,
        })
    return records


class NullSink:
    """Does nothing - the default, so the pipeline needs no special-casing."""

    name = "null"

    def start_run(self, run_id: str, meta: dict[str, Any]) -> None:
        return

    def upsert(self, results: Sequence[BusinessResult], status: str) -> None:
        return

    def flush(self) -> None:
        return

    def finish_run(self, run_id: str, stats: dict[str, Any]) -> None:
        return

    def close(self) -> None:
        return
