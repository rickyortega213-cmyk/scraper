from gmscrape.web.extract import extract_emails, find_internal_links, score_link


def _emails(html: str) -> set[str]:
    return {f.email for f in extract_emails(html)}


def test_finds_mailto_text_and_obfuscated():
    html = """
      <a href="mailto:Office@Acme.com?subject=hi">mail</a>
      <p>sales@acme.com and info [at] acme (dot) com</p>
    """
    assert _emails(html) == {"office@acme.com", "sales@acme.com", "info@acme.com"}


def test_decodes_cloudflare_protection():
    email = "billing@acme.com"
    key = 0x2f
    encoded = format(key, "02x") + "".join(format(ord(c) ^ key, "02x") for c in email)
    html = f'<a data-cfemail="{encoded}">[email protected]</a>'
    found = extract_emails(html)
    assert [f.email for f in found] == [email]
    assert found[0].source == "cloudflare_decoded"


def test_reads_jsonld_email():
    html = """<script type="application/ld+json">
      {"@type":"Dentist","email":"mailto:hello@acme.com"}</script>"""
    assert _emails(html) == {"hello@acme.com"}


def test_rejects_assets_analytics_and_placeholders():
    html = """
      <img src="logo@2x.png"><img src="hero@3x.jpg">
      <script>Sentry.init({dsn:"https://key@sentry.io/1"});</script>
      <p>you@example.com, name@yourdomain.com, a@b.notarealtld</p>
      <link href="fonts@1x.css">
    """
    assert _emails(html) == set()


def test_entity_encoded_at_sign():
    assert _emails("<p>info&#64;acme.com</p>") == {"info@acme.com"}


def test_prefers_strongest_source_per_address():
    html = """<p>info@acme.com</p><a href="mailto:info@acme.com">x</a>"""
    found = extract_emails(html)
    assert len(found) == 1 and found[0].source == "mailto"


def test_ranks_contact_links_first():
    html = """
      <a href="/blog/x">Blog</a><a href="/contact-us">Contact Us</a>
      <a href="/about">About</a><a href="https://other.com/contact">Other</a>
    """
    links = find_internal_links(html, "https://acme.com/")
    assert links[0].endswith("/contact-us")
    assert not any("other.com" in link for link in links)
    assert score_link("/contact", "Contact") > score_link("/blog/post", "Post")
