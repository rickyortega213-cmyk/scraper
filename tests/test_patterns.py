from gmscrape.emails.patterns import (
    build_permutations,
    domain_is_guessable,
    owner_locals_from_person,
    personal_locals_from_name,
)


def test_refuses_free_mail_and_platform_domains():
    assert domain_is_guessable("gmail.com") == (False, "free_mail_domain")
    assert domain_is_guessable("shop.wixsite.com")[1] == "platform_or_social_domain"
    assert domain_is_guessable("joesplumbing.com") == (True, "")


def test_tier_one_is_the_safest_three():
    plan = build_permutations("acme.com", tier=1, require_mx=False)
    assert [c.email for c in plan.candidates] == [
        "info@acme.com", "contact@acme.com", "hello@acme.com",
    ]


def test_tier_two_adds_common_and_owner_and_industry():
    plan = build_permutations(
        "joesplumbing.com", business_name="Joe's Plumbing", category="Plumber",
        tier=2, max_candidates=30, require_mx=False,
    )
    emails = [c.email for c in plan.candidates]
    assert "info@joesplumbing.com" in emails          # tier 1
    assert "office@joesplumbing.com" in emails        # tier 2
    assert "joe@joesplumbing.com" in emails           # owner from name
    assert "dispatch@joesplumbing.com" in emails      # industry-specific
    assert all(c.from_permutation and c.is_role for c in plan.candidates)


def test_skips_chains_unless_allowed():
    assert build_permutations("mcdonalds.com", is_chain=True, require_mx=False).skipped_reason == (
        "national_chain"
    )
    allowed = build_permutations(
        "somechain.com", is_chain=True, allow_chains=True, require_mx=False
    )
    assert allowed.allowed


def test_excludes_already_known_addresses_and_respects_cap():
    plan = build_permutations(
        "acme.com", tier=2, require_mx=False, max_candidates=4,
        exclude=["info@acme.com"],
    )
    emails = [c.email for c in plan.candidates]
    assert "info@acme.com" not in emails
    assert len(emails) == 4


def test_owner_name_patterns():
    assert owner_locals_from_person("John Doe")[:3] == ["john", "john.doe", "jdoe"]
    assert personal_locals_from_name("Joe's Plumbing") == ["joe"]
    assert personal_locals_from_name("Kowalski Roofing") == ["kowalski"]
