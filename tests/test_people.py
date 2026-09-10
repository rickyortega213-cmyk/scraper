"""Owner extraction: strict enough that it cannot invent a person."""

from gmscrape.emails.people import (
    OwnerCandidate,
    choose_owner,
    clean_person_name,
    email_matches_person,
    owner_candidates_from_html,
    owner_candidates_from_search,
    owner_from_html,
    owner_local_parts,
)
from gmscrape.models import Person

ABOUT = """<html><body><nav>Home | About Us | Contact</nav>
<h1>About Joe's Plumbing</h1>
<p>Joe's Plumbing was founded in 1998 by John Kowalski, a licensed master plumber.</p>
<h3>Meet Our Team</h3><p>John Kowalski, Owner</p><p>Maria Lopez - Office Manager</p>
<p>Owner Response: thanks! Partner With Us. Director Of Operations. Meet Our Team.</p>
<script type="application/ld+json">
{"@type":"Plumber","name":"Joe's Plumbing","founder":{"@type":"Person","name":"John Kowalski"}}
</script></body></html>"""


def test_finds_the_owner_from_text_and_jsonld():
    person = owner_from_html(ABOUT, "https://joes.com/about", "Joe's Plumbing")
    assert person is not None
    assert person.name == "John Kowalski" and person.title == "owner"
    assert person.confidence >= 85          # three corroborating mentions


def test_office_manager_loses_to_owner_but_wins_alone():
    names = {c.name for c in owner_candidates_from_html(ABOUT, "u", "Joe's Plumbing")}
    assert "Maria Lopez" in names
    person = owner_from_html("<p>Maria Lopez - Office Manager</p>", "u", "Joe's Plumbing")
    assert person is not None and person.title == "office manager" and person.rank < 60


def test_page_furniture_never_becomes_a_person():
    junk = ("<p>Owner Operator | Our Team | Family Owned and Operated by Austin Family | "
            "Owner Response | Founder And CEO | President Of Sales | Meet The Doctor</p>")
    assert owner_from_html(junk, "u", "Austin Family Dental") is None
    assert clean_person_name("Our Team") == ""
    assert clean_person_name("Austin Family") == ""                    # "family" is furniture
    assert clean_person_name("Austin Ridge") == "Austin Ridge"         # shape ok...
    assert owner_from_html("<p>Owner: Austin Ridge</p>", "u", "Austin Ridge Dental") is None  # ...but it's the business


def test_practice_principal_on_a_medical_site():
    html = "<p>Welcome to Austin Family Dental. Dr. Priya Patel, DDS leads our team.</p>"
    person = owner_from_html(html, "u", "Austin Family Dental", medical=True)
    assert person is not None and person.name == "Priya Patel" and person.title == "dds"
    # Not a medical business: a stray "Dr." mention is not the owner.
    assert owner_from_html(html.replace(", DDS", ""), "u", "Austin Roofing", medical=False) is None


def test_first_name_only_needs_stronger_evidence():
    assert owner_from_html("<p>Owner: Joe</p>", "u", "Joe's Plumbing") is None
    person = choose_owner([OwnerCandidate("Joe", "owner", 100, "site_jsonld", weight=2)])
    assert person is not None and owner_local_parts(person) == ["joe"]


def test_two_owners_with_equal_support_is_ambiguous():
    cands = [OwnerCandidate("Ann Lee", "owner", 100, "site_text"),
             OwnerCandidate("Bob Ray", "owner", 100, "site_text")]
    assert choose_owner(cands) is None
    cands.append(OwnerCandidate("Ann Lee", "owner", 100, "site_jsonld"))
    assert choose_owner(cands).name == "Ann Lee"


def test_search_only_trusts_text_that_names_the_business():
    blocks = [
        ("search_ai_overview", "Joe's Plumbing in Austin is owned by John Kowalski, who founded it in 1998."),
        ("search_snippet", "Top plumbers in Austin - owner Mike Jones of Other Plumbing Co."),
        ("search_knowledge", "title: Other Plumbing; attributes.founder: Mike Jones"),
    ]
    cands = owner_candidates_from_search(blocks, "Joe's Plumbing", "Austin")
    assert {c.name for c in cands} == {"John Kowalski"}
    person = choose_owner(cands)
    assert person.source == "search_ai_overview" and person.confidence >= 70


def test_search_knowledge_panel_attribute():
    blocks = [("search_knowledge", "title: Joe's Plumbing; attributes.founder: Jane Doe")]
    person = choose_owner(owner_candidates_from_search(blocks, "Joe's Plumbing"))
    assert person is not None and person.name == "Jane Doe" and person.title == "founder"


def test_owner_mailbox_patterns_and_matching():
    person = Person("John Kowalski", "owner")
    assert owner_local_parts(person)[:4] == ["john", "john.kowalski", "jkowalski", "johnk"]
    assert email_matches_person("jkowalski", person)
    assert email_matches_person("John.Kowalski", person)
    assert not email_matches_person("info", person)
    assert not email_matches_person("jon", person)
