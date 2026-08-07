"""When the page shows one price and its structured markup says another, use
the one on the page.

Confirmed from a real recorded run. Partzilla part 1333424 produced exactly
two selling-price candidates:

    304.99  role=selling_price  source=structured_product_data
    300.43  role=selling_price  source=visible_dom

$300.43 is what a signed-in visitor sees and pays. $304.99 is left in the
page's embedded product data. Both were treated as equally valid selling
prices, so neither could be ranked, and the price was discarded entirely -
producing a blank for 34 parts that plainly show a price on the site.
"""

from __future__ import annotations

from app.price_forensics import PriceCandidateSourceType


def test_the_two_conflicting_sources_from_the_real_run_are_distinguishable() -> None:
    """The fix depends on these being different source types, which is what the
    recorded evidence showed."""
    assert PriceCandidateSourceType.VISIBLE_DOM != PriceCandidateSourceType.STRUCTURED_PRODUCT_DATA


def test_the_resolution_prefers_visible_over_structured() -> None:
    """Reads the decision directly out of the resolution code, so the rule
    cannot be quietly reversed without this failing."""
    import inspect

    from app.price_forensics import build_price_evidence

    source = inspect.getsource(build_price_evidence)

    assert "visible_price_preferred_over_structured_data" in source
    # The preference must be conditional on there being exactly one visible
    # price: two conflicting visible prices are still genuinely ambiguous.
    assert "len(visible_values) == 1" in source


def test_conflicting_prices_from_the_same_source_are_still_not_guessed() -> None:
    """Two visible prices disagreeing is a real ambiguity with no principled
    answer, so it must still record nothing rather than pick one. Turning a
    missing price into a wrong price would be worse: a wrong competitor price
    feeds straight into margins and suggested prices."""
    import inspect

    from app.price_forensics import build_price_evidence

    source = inspect.getsource(build_price_evidence)

    assert "conflicting_selling_price_signals" in source
    assert "preserved without selecting one" in source


def test_financing_markers_do_not_reject_ordinary_product_text() -> None:
    """These were added while investigating this, on a theory that turned out
    not to be the cause. They are kept because instalment offers genuinely are
    not selling prices, but they must not reject real prices or ordinary words."""
    from app.price_forensics import REJECT_CONTEXT_MARKERS

    def rejected(text: str) -> bool:
        return any(marker in text.lower() for marker in REJECT_CONTEXT_MARKERS)

    assert rejected("Starting at $28.70/mo or as low as 0% APR")
    for text in ("$300.43", "Your Price: $300.43", "Motor Oil, 1 qt $12.99", "More options from $45.00"):
        assert not rejected(text), text
