"""Extraction false positives that were seen in the wild."""

from gmscrape.web.extract import extract_emails


def _emails(html: str, domain: str = "") -> list[str]:
    return sorted(f.email for f in extract_emails(html, business_domain=domain))


def test_prose_at_is_not_an_address():
    html = "<p>Join us at meetup.com and find us at facebook.com. Visit us at yelp.com!</p>"
    assert _emails(html) == []
    assert _emails(html, "joes.com") == []


def test_word_at_own_domain_is_an_address_only_for_mailbox_words():
    assert _emails("<p>Reach dispatch at joes.com</p>", "joes.com") == ["dispatch@joes.com"]
    assert _emails("<p>Reach dispatch at joes.com</p>") == []        # domain unknown
    assert _emails("<p>See us at joes.com</p>", "joes.com") == []    # "us" is prose


def test_fully_obfuscated_forms_always_count():
    assert _emails("<p>info [at] joes [dot] com</p>") == ["info@joes.com"]
    assert _emails("<p>info (at) joes.com</p>") == ["info@joes.com"]
