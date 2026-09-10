"""Client-facing formatting: title case, phone numbers, names, the clean table."""

import csv

import pytest

from gmscrape.format import clean_state, format_phone, smart_title, split_name
from gmscrape.models import (
    CONTACT_OWNER, BusinessResult, EmailCandidate, Place, SOURCE_MAILTO, SOURCE_PERMUTATION,
    VerificationResult, V_RISKY, V_VALID,
)
from gmscrape.store.export import CLEAN_COLUMNS, clean_rows, export_results


@pytest.mark.parametrize("raw, expected", [
    ("JOE'S PLUMBING & HEATING LLC", "Joe's Plumbing & Heating LLC"),
    ("mcdonald's of round rock", "McDonald's of Round Rock"),
    ("h-e-b plus", "H-E-B Plus"),
    ("o'brien roofing co", "O'Brien Roofing Co"),
    ("the ups store", "The UPS Store"),
    ("220 oak ave, austin, tx 78702", "220 Oak Ave, Austin, TX 78702"),
    ("1st choice hvac", "1st Choice HVAC"),
    ("iPhone repair", "iPhone Repair"),
    ("Bright Now! Dental", "Bright Now! Dental"),
    ("", ""),
])
def test_smart_title(raw, expected):
    assert smart_title(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("(956) 324-6856", "(956)-324-6856"),
    ("956.324.6856", "(956)-324-6856"),
    ("+1 956-324-6856", "(956)-324-6856"),
    ("9563246856", "(956)-324-6856"),
    ("1-956-324-6856 ext 12", "(956)-324-6856 ext. 12"),
    ("+44 20 7946 0958", "+44 20 7946 0958"),        # not North American: left alone
    ("", ""),
])
def test_format_phone(raw, expected):
    assert format_phone(raw) == expected


def test_split_name_and_state():
    assert split_name("Dr. Priya K. Patel") == ("Priya", "Patel")
    assert split_name("john kowalski") == ("John", "Kowalski")
    assert split_name("Cher") == ("Cher", "")
    assert split_name("") == ("", "")
    assert clean_state("tx") == "TX" and clean_state("texas") == "Texas"


def _result() -> BusinessResult:
    return BusinessResult(
        place=Place(name="JOE'S PLUMBING & HEATING", query="plumber in austin tx",
                    category="plumber", address="100 main st, austin, tx 78701",
                    city="austin", state="tx", phone="512 555 0100",
                    website="https://joesplumbing.com", google_url="https://maps.google.com/?cid=1",
                    rating=4.8, reviews=87),
        emails=[
            EmailCandidate(email="office@joesplumbing.com", source=SOURCE_MAILTO, confidence=90,
                           verification=VerificationResult(status=V_VALID, provider="t")),
            EmailCandidate(email="john.kowalski@joesplumbing.com", source=SOURCE_PERMUTATION,
                           confidence=62, contact_type=CONTACT_OWNER, contact_name="John Kowalski",
                           contact_title="owner",
                           verification=VerificationResult(status=V_RISKY, provider="t")),
        ],
    )


def test_clean_rows_shape_and_formatting():
    rows = clean_rows(_result(), run_date="2026-09-10")
    assert [r["contact_type"] for r in rows] == ["Owner", "General"]
    owner, general = rows
    assert owner["company_name"] == "Joe's Plumbing & Heating"
    assert owner["city"] == "Austin" and owner["state"] == "TX"
    assert owner["address"] == "100 Main St, Austin, TX 78701"
    assert owner["phone_number"] == "(512)-555-0100"
    assert owner["business_type"] == "Plumber"
    assert (owner["contact_first_name"], owner["contact_last_name"]) == ("John", "Kowalski")
    assert owner["contact_title"] == "Owner"
    # Only a `valid` address is a verified email; the candidate still shows in `email`.
    assert owner["verified_email"] == "" and owner["email"] == "john.kowalski@joesplumbing.com"
    assert general["verified_email"] == "office@joesplumbing.com"
    assert general["contact_first_name"] == "" and general["run_date"] == "2026-09-10"
    assert list(rows[0]) and set(CLEAN_COLUMNS) == set(rows[0])


def test_city_and_state_fall_back_to_the_address():
    result = _result()
    result.place.city = ""
    result.place.state = ""
    row = clean_rows(result)[0]
    assert row["city"] == "Austin" and row["state"] == "TX"


def test_business_type_falls_back_to_the_query():
    result = _result()
    result.place.category = ""
    assert clean_rows(result)[0]["business_type"] == "Plumber"


def test_csv_is_the_clean_table(tmp_path):
    paths = export_results([_result()], tmp_path, formats=["csv"], run_date="2026-09-10")
    names = sorted(p.name for p in paths)
    assert names == ["leads.csv", "leads_detailed.csv", "leads_emails.csv"]
    rows = list(csv.DictReader((tmp_path / "leads.csv").open(encoding="utf-8-sig")))
    assert list(rows[0]) == list(CLEAN_COLUMNS)
    assert rows[0]["phone_number"] == "(512)-555-0100"
