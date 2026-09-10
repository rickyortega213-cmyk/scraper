"""Export results to CSV / JSON / JSONL / XLSX.

Two shapes:
  * business rows  - one row per business, best email plus all emails joined
  * email rows     - one row per email (for import into a sending tool)
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..models import BusinessResult

# One row per contact (general inbox and/or owner). Everything about the
# business repeats on each of its rows; only the contact columns differ.
LEAD_COLUMNS = (
    "name", "query", "category", "contact_type", "contact_name", "contact_title",
    "email", "email_source", "email_status", "email_confidence",
    "phone", "website", "website_source", "domain", "address", "city", "state",
    "postal_code", "rating", "reviews", "is_chain", "chain_reasons",
    "owner_name", "owner_title", "owner_source", "owner_confidence",
    "emails_found", "emails_guessed", "all_emails", "website_status",
    "domain_has_mx", "permutations_skipped_reason", "pages_crawled",
    "place_id", "latitude", "longitude", "google_url", "notes",
)

# Business-level summary (used by the JSON export and the Supabase sink).
BUSINESS_COLUMNS = (
    "name", "query", "category", "best_email", "best_email_source",
    "best_email_status", "best_email_confidence", "emails_found", "emails_guessed",
    "all_emails", "phone", "website", "website_source", "domain", "address", "city",
    "state", "postal_code", "rating", "reviews", "is_chain", "chain_reasons",
    "owner_name", "owner_title", "owner_source", "owner_confidence",
    "website_status", "domain_has_mx", "permutations_skipped_reason",
    "pages_crawled", "place_id", "latitude", "longitude", "google_url", "notes",
)

EMAIL_COLUMNS = (
    "email", "business_name", "contact_type", "contact_name", "lead_eligible",
    "confidence", "status", "sub_status", "provider", "source", "source_url",
    "pattern", "is_role", "is_personal_domain", "on_business_domain", "domain",
    "phone", "website", "city", "state", "is_chain", "query", "context", "notes",
)


def business_row(result: BusinessResult) -> dict[str, Any]:
    place = result.place
    best = result.best_email
    return {
        "name": place.name,
        "query": place.query,
        "category": place.category,
        "best_email": best.email if best else "",
        "best_email_source": best.source if best else "",
        "best_email_status": best.status if best else "",
        "best_email_confidence": best.confidence if best else "",
        "emails_found": len(result.found_emails),
        "emails_guessed": len(result.guessed_emails),
        "all_emails": "; ".join(c.email for c in result.emails),
        "phone": place.phone,
        "website": place.website,
        "website_source": result.website_source,
        "domain": place.domain,
        "address": place.address,
        "city": place.city,
        "state": place.state,
        "postal_code": place.postal_code,
        "rating": place.rating if place.rating is not None else "",
        "reviews": place.reviews if place.reviews is not None else "",
        "is_chain": "yes" if result.is_chain else "no",
        "chain_reasons": "; ".join(result.chain_reasons),
        "owner_name": result.owner.name if result.owner else "",
        "owner_title": result.owner.title if result.owner else "",
        "owner_source": result.owner.source if result.owner else "",
        "owner_confidence": result.owner.confidence if result.owner else "",
        "website_status": result.website_status,
        "domain_has_mx": "" if result.domain_has_mx is None else ("yes" if result.domain_has_mx else "no"),
        "permutations_skipped_reason": result.permutations_skipped_reason,
        "pages_crawled": len(result.pages_crawled),
        "place_id": place.place_id,
        "latitude": place.latitude if place.latitude is not None else "",
        "longitude": place.longitude if place.longitude is not None else "",
        "google_url": place.google_url,
        "notes": "; ".join(result.notes),
    }


def lead_rows(result: BusinessResult) -> list[dict[str, Any]]:
    """One row per selected contact; a business with no email still gets a row."""
    base = business_row(result)
    for key in ("best_email", "best_email_source", "best_email_status", "best_email_confidence"):
        base.pop(key, None)
    contacts = result.lead_contacts()
    if not contacts:
        return [{**base, "contact_type": "", "contact_name": "", "contact_title": "",
                 "email": "", "email_source": "", "email_status": "", "email_confidence": ""}]
    rows: list[dict[str, Any]] = []
    for candidate in contacts:
        rows.append({
            **base,
            "contact_type": candidate.contact_type,
            "contact_name": candidate.contact_name,
            "contact_title": candidate.contact_title,
            "email": candidate.email,
            "email_source": candidate.source,
            "email_status": candidate.status,
            "email_confidence": candidate.confidence,
        })
    return rows


def email_rows(result: BusinessResult) -> list[dict[str, Any]]:
    place = result.place
    rows: list[dict[str, Any]] = []
    for candidate in result.emails:
        verification = candidate.verification
        rows.append({
            "email": candidate.email,
            "business_name": place.name,
            "contact_type": candidate.contact_type,
            "contact_name": candidate.contact_name,
            "lead_eligible": "yes" if candidate.lead_eligible else "no",
            "confidence": candidate.confidence,
            "status": candidate.status,
            "sub_status": verification.sub_status if verification else "",
            "provider": verification.provider if verification else "",
            "source": candidate.source,
            "source_url": candidate.source_url,
            "pattern": candidate.pattern,
            "is_role": "yes" if candidate.is_role else "no",
            "is_personal_domain": "yes" if candidate.is_personal_domain else "no",
            "on_business_domain": "yes" if candidate.on_business_domain else "no",
            "domain": candidate.domain,
            "phone": place.phone,
            "website": place.website,
            "city": place.city,
            "state": place.state,
            "is_chain": "yes" if result.is_chain else "no",
            "query": place.query,
            "context": candidate.context[:200],
            "notes": "; ".join(candidate.notes),
        })
    return rows


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def _result_to_dict(result: BusinessResult) -> dict[str, Any]:
    payload = {
        "place": asdict(result.place),
        "is_chain": result.is_chain,
        "chain_score": result.chain_score,
        "chain_reasons": result.chain_reasons,
        "website_status": result.website_status,
        "pages_crawled": result.pages_crawled,
        "domain_has_mx": result.domain_has_mx,
        "domain_is_catch_all": result.domain_is_catch_all,
        "permutations_skipped_reason": result.permutations_skipped_reason,
        "notes": result.notes,
        "website_source": result.website_source,
        "owner": (
            {k: v for k, v in asdict(result.owner).items()} if result.owner else None
        ),
        "best_email": result.best_email.email if result.best_email else None,
        "contacts": [
            {"contact_type": c.contact_type, "contact_name": c.contact_name,
             "contact_title": c.contact_title, "email": c.email,
             "source": c.source, "status": c.status, "confidence": c.confidence}
            for c in result.lead_contacts()
        ],
        "emails": [
            {
                **{k: v for k, v in asdict(candidate).items() if k != "verification"},
                "status": candidate.status,
                "verification": (
                    {k: v for k, v in asdict(candidate.verification).items() if k != "raw"}
                    if candidate.verification else None
                ),
            }
            for candidate in result.emails
        ],
    }
    payload["place"].pop("raw", None)
    return payload


def export_results(
    results: Sequence[BusinessResult],
    out_dir: str | Path,
    *,
    basename: str = "leads",
    formats: Sequence[str] = ("csv", "json"),
) -> list[Path]:
    """Write every requested format; returns the paths written."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    formats = [f.lower() for f in formats]
    if "all" in formats:
        formats = ["csv", "json", "jsonl", "xlsx"]

    leads = [row for r in results for row in lead_rows(r)]
    emails = [row for r in results for row in email_rows(r)]

    if "csv" in formats:
        path = directory / f"{basename}.csv"
        _write_csv(path, LEAD_COLUMNS, leads)
        written.append(path)
        email_path = directory / f"{basename}_emails.csv"
        _write_csv(email_path, EMAIL_COLUMNS, emails)
        written.append(email_path)

    if "json" in formats:
        path = directory / f"{basename}.json"
        path.write_text(
            json.dumps([_result_to_dict(r) for r in results], indent=2, default=str),
            encoding="utf-8",
        )
        written.append(path)

    if "jsonl" in formats:
        path = directory / f"{basename}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for result in results:
                handle.write(json.dumps(_result_to_dict(result), default=str) + "\n")
        written.append(path)

    if "xlsx" in formats:
        path = _write_xlsx(directory / f"{basename}.xlsx", leads, emails)
        if path is not None:
            written.append(path)

    return written


def _write_xlsx(
    path: Path, leads: list[dict[str, Any]], emails: list[dict[str, Any]]
) -> Path | None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font
        from openpyxl.utils import get_column_letter
    except ImportError:  # pragma: no cover - optional dependency
        return None

    workbook = Workbook()
    for index, (title, columns, rows) in enumerate(
        (("Leads", LEAD_COLUMNS, leads), ("Emails", EMAIL_COLUMNS, emails))
    ):
        sheet = workbook.active if index == 0 else workbook.create_sheet()
        sheet.title = title
        sheet.append(list(columns))
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(vertical="center")
        for row in rows:
            sheet.append([row.get(column, "") for column in columns])
        sheet.freeze_panes = "A2"
        for position, column in enumerate(columns, start=1):
            width = max(len(column) + 2, 12)
            sheet.column_dimensions[get_column_letter(position)].width = min(width, 42)
        if rows:
            sheet.auto_filter.ref = (
                f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
            )
    workbook.save(path)
    return path
