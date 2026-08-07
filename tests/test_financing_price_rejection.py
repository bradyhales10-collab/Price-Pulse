"""A monthly financing payment must not be read as the item's price.

Partzilla's product page shows the real price beside an instalment offer:

    $300.43
    Starting at $28.70/mo or as low as 0% APR

Both are real dollar amounts, so $28.70 was accepted as a second selling
price. With two conflicting selling prices and nothing marking which was the
discount, the parser deliberately recorded no price at all rather than guess,
which is why 34 parts came back blank while showing a clear price on the site.

REJECT_CONTEXT_MARKERS already contained "financing", but that word never
appears on the page, so it never matched.
"""

from __future__ import annotations

from app.price_forensics import REJECT_CONTEXT_MARKERS


def _is_rejected(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in REJECT_CONTEXT_MARKERS)


def test_the_exact_financing_text_from_the_page_is_rejected() -> None:
    assert _is_rejected("Starting at $28.70/mo or as low as 0% APR. Learn more")


def test_other_common_instalment_offers_are_rejected() -> None:
    for text in (
        "Pay in 4 interest-free payments of $75.11",
        "or 4 payments of $75.11 with Klarna",
        "As low as $28/month with Affirm",
        "Pay over time with PayPal Credit",
        "4 interest-free installments",
        "$28.70 per month",
    ):
        assert _is_rejected(text), text


def test_real_prices_are_still_accepted() -> None:
    """The rejection must be narrow. Blocking a real price would turn a
    missing-price problem into a wrong-price problem, which is worse."""
    for text in (
        "$300.43",
        "Your Price: $300.43",
        "Add to Cart $300.43",
        "MSRP $355.99",
        "Sale $289.99",
        "In Stock",
    ):
        assert not _is_rejected(text), text


def test_ordinary_words_containing_the_markers_do_not_trip_it() -> None:
    """"/mo" and similar are matched as substrings, so anything containing them
    incidentally must not be rejected."""
    for text in (
        "Motor Oil, 1 qt $12.99",
        "More options from $45.00",
        "Mounting Bracket $19.99",
        "Motorcycle Cover $89.99",
    ):
        assert not _is_rejected(text), text
