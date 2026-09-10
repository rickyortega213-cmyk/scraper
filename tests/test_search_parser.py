"""OpenWeb Ninja response parsing across the envelope shapes it might use."""

from gmscrape.providers.search.openwebninja import parse_response


def test_flat_data_list_shape():
    payload = {"status": "OK", "data": [
        {"url": "https://joesplumbing.com/", "title": "Joe's Plumbing", "snippet": "Owner John Smith"},
        {"link": "https://yelp.com/biz/joes", "title": "Joe's - Yelp", "description": "reviews"},
    ]}
    response = parse_response("q", payload)
    assert [h.domain for h in response.hits] == ["joesplumbing.com", "yelp.com"]
    assert response.hits[1].snippet == "reviews"
    assert response.ok and not response.ai_overview


def test_nested_shape_with_ai_overview_knowledge_and_paa():
    payload = {"status": "OK", "has_ai_overviews": True, "data": {
        "organic_results": [{"url": "https://x.com/a", "title": "t", "snippet": "s", "position": 1}],
        "ai_overview": {"text_parts": [
            {"type": "paragraph", "text": "Joe's Plumbing is owned by John Smith."},
            {"type": "list", "text": "Founded 1998."},
        ], "references": [{"url": "https://x"}]},
        "knowledge_graph": {"title": "Joe's Plumbing",
                            "attributes": {"Founder": "John Smith", "Founded": "1998"}},
        "people_also_ask": [{"question": "Who owns Joe's?", "answer": {"snippet": "John Smith."}}],
    }}
    response = parse_response("q", payload)
    assert response.ai_overview == "Joe's Plumbing is owned by John Smith. Founded 1998."
    assert response.knowledge["attributes.founder"] == "John Smith"
    assert response.extra_text == ["Who owns Joe's? John Smith."]
    labels = [label for label, _ in response.all_text_blocks()]
    assert labels[0] == "search_ai_overview"       # most trusted first
    assert "search_knowledge" in labels and "search_snippet" in labels


def test_ai_overview_as_plain_string():
    response = parse_response("q", {"data": {"results": [], "ai_overview": "Owned by Ann Lee."}})
    assert response.ai_overview == "Owned by Ann Lee."


def test_error_status_is_surfaced_not_raised():
    response = parse_response("q", {"status": "ERROR", "message": "quota exceeded"})
    assert response.error == "quota exceeded" and not response.hits


def test_garbage_is_tolerated():
    assert parse_response("q", "not json").error
    assert parse_response("q", {"status": "OK", "data": {"weird": 1}}).hits == []
    assert parse_response("q", {"data": [{"url": "javascript:void(0)"}]}).hits == []
