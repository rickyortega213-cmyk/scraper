from gmscrape.filters.chains import ChainVerdict, classify, should_keep
from gmscrape.models import Place


def test_flags_big_box_and_franchise_brands():
    assert classify(Place(name="Walmart Supercenter #1234", website="https://walmart.com")).is_chain
    assert classify(Place(name="McDonald's")).is_chain
    assert classify(Place(name="Great Clips", website="https://greatclips.com")).is_chain
    assert classify(Place(name="Starbucks Reserve Roastery")).is_chain


def test_keeps_genuine_local_businesses():
    assert not classify(Place(name="Joe's Plumbing & Heating", reviews=87)).is_chain
    assert not classify(Place(name="Austin Family Dental", reviews=412)).is_chain
    # A busy local restaurant is not a chain just because it is popular.
    assert not classify(Place(name="Riverside Taqueria", reviews=2900)).is_chain


def test_corporate_domain_alone_is_enough():
    verdict = classify(Place(name="The Coffee Place", website="https://starbucks.com/store/9"))
    assert verdict.is_chain
    assert any("corporate_domain" in reason for reason in verdict.reasons)


def test_chain_mode_filtering():
    chain = ChainVerdict(is_chain=True, score=100, reasons=[])
    local = ChainVerdict(is_chain=False, score=0, reasons=[])
    assert should_keep(chain, "flag") and should_keep(local, "flag")
    assert not should_keep(chain, "skip") and should_keep(local, "skip")
    assert should_keep(chain, "only") and not should_keep(local, "only")
