"""Website discovery: pick the business's own site, refuse to guess otherwise."""

from gmscrape.models import Place
from gmscrape.providers.search.base import SearchHit
from gmscrape.web.discover import confirm_website, discovery_query, pick_website, score_hit

DENTAL = Place(name="Austin Family Dental", city="Austin", state="TX",
               phone="(512) 555-0142", address="220 Oak Ave, Austin, TX 78702")


def test_query_isolates_the_business():
    assert discovery_query(DENTAL) == '"Austin Family Dental" Austin TX'


def test_picks_the_matching_domain_over_directories():
    hits = [
        SearchHit("https://www.yelp.com/biz/austin-family-dental", "Austin Family Dental - Yelp", "", 1),
        SearchHit("https://www.austinfamilydental.com/", "Austin Family Dental | Dentist", "Call (512) 555-0142", 2),
        SearchHit("https://www.facebook.com/austinfamilydental", "Austin Family Dental", "", 3),
    ]
    guess = pick_website(DENTAL, hits)
    assert guess is not None
    assert guess.url == "https://www.austinfamilydental.com/"
    assert "domain_matches_name" in guess.reasons and "phone_in_snippet" in guess.reasons
    assert score_hit(hits[0], DENTAL) == (0, ["directory_or_platform"])


def test_refuses_when_two_sites_are_equally_plausible():
    place = Place(name="Smile Dental", city="Austin")
    hits = [
        SearchHit("https://smiledental.com/", "Smile Dental", "dentist", 1),
        SearchHit("https://smiledentalaustin.com/", "Smile Dental", "dentist", 2),
    ]
    assert pick_website(place, hits) is None


def test_refuses_weak_matches():
    hits = [SearchHit("https://austindentists.org/", "Dentists in Austin", "directory of dentists", 1)]
    assert pick_website(DENTAL, hits) is None


def test_confirmation_needs_a_business_marker_on_the_page():
    ok, reasons = confirm_website(DENTAL, "Austin Family Dental - call 512-555-0142 - 220 Oak Ave")
    assert ok and {"phone_on_page", "name_on_page", "street_on_page"} <= set(reasons)
    ok, reasons = confirm_website(DENTAL, "Bob's Roofing. Call 512-555-9999")
    assert not ok and reasons == ["no_business_markers_on_page"]


def test_generic_name_alone_is_not_confirmation():
    place = Place(name="Smile Dental", phone="(512) 555-0100")
    assert confirm_website(place, "Welcome to Smile Dental")[0] is False
    assert confirm_website(place, "Welcome to Smile Dental, call 512-555-0100")[0] is True
    # ...unless the name is distinctive enough on its own.
    long_name = Place(name="Pflugerville Bluebonnet Family Dentistry", phone="(512) 555-0100")
    assert confirm_website(long_name, "Pflugerville Bluebonnet Family Dentistry welcomes you")[0]
