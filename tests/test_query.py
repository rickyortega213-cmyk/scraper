from gmscrape.query import parse_queries, parse_query


def test_splits_on_in():
    spec = parse_query("dentist in austin tx")
    assert spec.business_type == "dentist"
    assert spec.location == "austin tx"


def test_handles_separators_and_commas():
    assert parse_query("plumbers in Miami, FL").location == "Miami, FL"
    assert parse_query("coffee shop - Boulder CO").business_type == "coffee shop"
    assert parse_query("med spa near San Diego").location == "San Diego"
    spec = parse_query("Roofing Companies, Dallas, TX")
    assert spec.business_type == "Roofing Companies"
    assert spec.location == "Dallas, TX"


def test_trailing_zip_is_a_location():
    spec = parse_query("barber shop 90210")
    assert spec.business_type == "barber shop"
    assert spec.location == "90210"


def test_no_location_passes_through():
    spec = parse_query("hvac contractor")
    assert spec.location == ""
    assert spec.search_string == "hvac contractor"


def test_dedupes_and_skips_comments():
    specs = parse_queries([
        "# a comment", "", "dentist in austin tx", "Dentist in Austin TX",
    ])
    assert len(specs) == 1
