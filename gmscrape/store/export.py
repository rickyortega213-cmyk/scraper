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

from ..format import clean_state, format_phone, smart_title, split_name
from ..models import BusinessResult, V_VALID

# The table people actually read. One row per contact; title-cased; phone
# as (956)-324-6856. `verified_email` is filled only when a verifier said the
# mailbox exists; `email` always shows the best candidate.
CLEAN_COLUMNS = (
    "company_name", "city", "state", "address", "phone_number", "verified_email",
    "contact_first_name", "contact_last_name", "contact_title", "business_type",
    "contact_type", "email", "email_status", "email_confidence", "website",
    "google_maps_link", "rating", "reviews", "is_chain", "search_query", "run_date",
)

# Everything, one row per contact (general inbox and/or owner). Business
# columns repeat on each of a business's rows; only the contact columns differ.
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


def clean_rows(result: BusinessResult, run_date: str = "") -> list[dict[str, Any]]:
    """The client-facing rows for one business."""
    place = result.place
    address = smart_title(place.address or ", ".join(
        p for p in (place.street, place.city, place.state, place.postal_code) if p
    ))
    city = smart_title(place.city) or _city_from(address)
    base = {
        "company_name": smart_title(place.name),
        "city": city,
        "state": clean_state(place.state) or _state_from(address),
        "address": address,
        "phone_number": format_phone(place.phone),
        "business_type": smart_title(place.category) or smart_title(_query_type(place.query)),
        "website": place.website,
        "google_maps_link": place.google_url,
        "rating": place.rating if place.rating is not None else "",
        "reviews": place.reviews if place.reviews is not None else "",
        "is_chain": "Yes" if result.is_chain else "No",
        "search_query": place.query,
        "run_date": run_date,
    }
    contacts = result.lead_contacts()
    if not contacts:
        return [{**base, "verified_email": "", "contact_first_name": "", "contact_last_name": "",
                 "contact_title": "", "contact_type": "", "email": "", "email_status": "",
                 "email_confidence": ""}]
    rows: list[dict[str, Any]] = []
    for candidate in contacts:
        first, last = split_name(candidate.contact_name)
        rows.append({
            **base,
            "verified_email": candidate.email if candidate.status == V_VALID else "",
            "contact_first_name": first,
            "contact_last_name": last,
            "contact_title": smart_title(candidate.contact_title),
            "contact_type": smart_title(candidate.contact_type),
            "email": candidate.email,
            "email_status": candidate.status,
            "email_confidence": candidate.confidence,
        })
    return rows


def _query_type(query: str) -> str:
    from ..query import parse_query

    try:
        return parse_query(query).business_type if query else ""
    except ValueError:
        return ""


def _city_from(address: str) -> str:
    from ..util import city_from_address

    return smart_title(city_from_address(address))


def _state_from(address: str) -> str:
    import re

    match = re.search(r",\s*([A-Za-z]{2})(?:\s+\d{5}(?:-\d{4})?)?(?:,\s*(?:USA|US|United States))?\s*$",
                      address or "")
    return match.group(1).upper() if match else ""


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
    run_date: str = "",
) -> list[Path]:
    """Write every requested format; returns the paths written.

    `<basename>.csv` is the clean table (title case, formatted phone, first /
    last name). `<basename>_detailed.csv` has every diagnostic column and
    `<basename>_emails.csv` every address considered.
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    formats = [f.lower() for f in formats]
    if "all" in formats:
        formats = ["csv", "json", "jsonl", "xlsx"]

    clean = [row for r in results for row in clean_rows(r, run_date)]
    leads = [row for r in results for row in lead_rows(r)]
    emails = [row for r in results for row in email_rows(r)]

    if "csv" in formats:
        path = directory / f"{basename}.csv"
        _write_csv(path, CLEAN_COLUMNS, clean)
        written.append(path)
        detailed_path = directory / f"{basename}_detailed.csv"
        _write_csv(detailed_path, LEAD_COLUMNS, leads)
        written.append(detailed_path)
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
        path = _write_xlsx(directory / f"{basename}.xlsx", clean, leads, emails)
        if path is not None:
            written.append(path)

    return written


def _write_xlsx(
    path: Path, clean: list[dict[str, Any]], leads: list[dict[str, Any]],
    emails: list[dict[str, Any]],
) -> Path | None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font
        from openpyxl.utils import get_column_letter
    except ImportError:  # pragma: no cover - optional dependency
        return None

    workbook = Workbook()
    for index, (title, columns, rows) in enumerate(
        (("Leads", CLEAN_COLUMNS, clean), ("Detailed", LEAD_COLUMNS, leads),
         ("Emails", EMAIL_COLUMNS, emails))
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


class CsvAppender:
    """Append-only CSV writers for large runs: headers once, then rows per batch,
    so the file is always complete up to the last checkpoint and never rewritten."""

    def __init__(self, out_dir: str | Path, basename: str = "leads", run_date: str = "") -> None:
        self.directory = Path(out_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.basename = basename
        self.run_date = run_date
        self.paths = {
            "clean": self.directory / f"{basename}.csv",
            "detailed": self.directory / f"{basename}_detailed.csv",
            "emails": self.directory / f"{basename}_emails.csv",
        }

    def start(self, fresh: bool = True) -> None:
        if not fresh and all(p.exists() for p in self.paths.values()):
            return
        for kind, columns in (("clean", CLEAN_COLUMNS), ("detailed", LEAD_COLUMNS), ("emails", EMAIL_COLUMNS)):
            with self.paths[kind].open("w", newline="", encoding="utf-8-sig") as handle:
                csv.DictWriter(handle, fieldnames=list(columns)).writeheader()

    def append(self, results: Sequence[BusinessResult]) -> None:
        if not results:
            return
        batches = (
            ("clean", CLEAN_COLUMNS, [row for r in results for row in clean_rows(r, self.run_date)]),
            ("detailed", LEAD_COLUMNS, [row for r in results for row in lead_rows(r)]),
            ("emails", EMAIL_COLUMNS, [row for r in results for row in email_rows(r)]),
        )
        for kind, columns, rows in batches:
            with self.paths[kind].open("a", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
                writer.writerows(rows)

    def written(self) -> list[Path]:
        return list(self.paths.values())
