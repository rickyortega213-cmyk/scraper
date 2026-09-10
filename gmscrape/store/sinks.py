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
from .export import clean_rows, email_rows, lead_rows

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


def lead_records(result: BusinessResult, run_id: str, status: str) -> list[dict[str, Any]]:
    """Rows for the leads table: one per contact, keyed `<business>|<contact_type>`.

    Before any contact exists the business has a single `|general` row, which
    the general contact later takes over; the owner row appears when an owner
    address does. Re-runs update in place.
    """
    parent = lead_id(result)

    def blank_to_none(value: Any) -> Any:
        return None if value == "" else value

    records: list[dict[str, Any]] = []
    for row, clean in zip(lead_rows(result), clean_rows(result)):
        contact_type = row["contact_type"] or "general"
        records.append({
            "id": f"{parent}|{contact_type}",
            "business_id": parent,
            "run_id": run_id,
            "status": status,
            # --- the clean, human-facing columns (title case, formatted phone)
            "company_name": clean["company_name"],
            "city_clean": blank_to_none(clean["city"]),
            "state_clean": blank_to_none(clean["state"]),
            "address_clean": blank_to_none(clean["address"]),
            "phone_number": blank_to_none(clean["phone_number"]),
            "verified_email": blank_to_none(clean["verified_email"]),
            "contact_first_name": blank_to_none(clean["contact_first_name"]),
            "contact_last_name": blank_to_none(clean["contact_last_name"]),
            "business_type": blank_to_none(clean["business_type"]),
            "contact_type": contact_type,
            "contact_name": blank_to_none(row["contact_name"]),
            "contact_title": blank_to_none(row["contact_title"]),
            "email": blank_to_none(row["email"]),
            "email_source": blank_to_none(row["email_source"]),
            "email_status": blank_to_none(row["email_status"]),
            "email_confidence": blank_to_none(row["email_confidence"]),
            "name": row["name"],
            "query": row["query"],
            "category": blank_to_none(row["category"]),
            "phone": blank_to_none(row["phone"]),
            "website": blank_to_none(row["website"]),
            "website_source": blank_to_none(row["website_source"]),
            "domain": blank_to_none(row["domain"]),
            "address": blank_to_none(row["address"]),
            "city": blank_to_none(row["city"]),
            "state": blank_to_none(row["state"]),
            "postal_code": blank_to_none(row["postal_code"]),
            "rating": blank_to_none(row["rating"]),
            "reviews": blank_to_none(row["reviews"]),
            "is_chain": result.is_chain,
            "chain_reasons": blank_to_none(row["chain_reasons"]),
            "owner_name": blank_to_none(row["owner_name"]),
            "owner_title": blank_to_none(row["owner_title"]),
            "owner_source": blank_to_none(row["owner_source"]),
            "owner_confidence": blank_to_none(row["owner_confidence"]),
            "emails_found": row["emails_found"],
            "emails_guessed": row["emails_guessed"],
            "all_emails": blank_to_none(row["all_emails"]),
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
        })
    return records


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
            "contact_type": row["contact_type"],
            "contact_name": row["contact_name"] or None,
            "lead_eligible": row["lead_eligible"] == "yes",
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
