"""The under-market target ladder, from the pricing specification.

When we are priced below the lowest validated competitor, how close to that
competitor we move depends on how much is economically at stake. Low
sensitivity does not mean surrendering every competitive advantage: a part that
still sells in reasonable numbers is worth keeping visibly cheaper, and only one
with genuinely minimal exposure should match the market exactly.

    HIGH                              98.0%
    MEDIUM                            99.0%
    LOW with meaningful exposure      99.5%
    LOW with minimal exposure        100.0%

The tests below are the ones the specification asks for by name, and they run
against the same recommend() used in production.
"""

from __future__ import annotations

from decimal import Decimal

from app.pricing_engine import (
    ACTION_HOLD,
    ACTION_INCREASE,
    ACTION_NEEDS_RESEARCH,
    RULE_LOW_EXPOSURE_995,
    RULE_LOW_MINIMAL_100,
    CompetitorQuote,
    recommend,
)
from app.sensitivity import SENSITIVITY_HIGH, SENSITIVITY_LOW, SENSITIVITY_MEDIUM

MARKET = [
    CompetitorQuote("Partzilla", Decimal("100")),
    CompetitorQuote("MotoSport", Decimal("104")),
    CompetitorQuote("Chaparral", Decimal("102")),
]


def test_high_sensitivity_targets_98_percent_of_lowest() -> None:
    result = recommend(
        current_price=Decimal("80"), cost=Decimal("40"), sensitivity=SENSITIVITY_HIGH,
        quotes=MARKET, qty_sold_12m=500,
    )

    assert result.action == ACTION_INCREASE
    assert result.recommended_price == Decimal("98.00")
    assert result.target_percent_of_lowest == Decimal("98.000")


def test_medium_sensitivity_targets_99_percent_of_lowest() -> None:
    result = recommend(
        current_price=Decimal("80"), cost=Decimal("40"), sensitivity=SENSITIVITY_MEDIUM,
        quotes=MARKET, qty_sold_12m=200,
    )

    assert result.action == ACTION_INCREASE
    assert result.recommended_price == Decimal("99.00")


def test_low_sensitivity_with_meaningful_sales_targets_995_percent() -> None:
    """The change this specification is really about: 100 units a year is
    enough that a small advantage is worth keeping."""
    result = recommend(
        current_price=Decimal("80"), cost=Decimal("40"), sensitivity=SENSITIVITY_LOW,
        quotes=MARKET, qty_sold_12m=100,
    )

    assert result.action == ACTION_INCREASE
    assert result.recommended_price == Decimal("99.50")
    assert result.rule_applied == RULE_LOW_EXPOSURE_995


def test_low_sensitivity_with_minimal_exposure_matches_the_market() -> None:
    """Five units a year and $400 of sales is little enough that there is
    nothing to protect by staying cheaper."""
    result = recommend(
        current_price=Decimal("80"), cost=Decimal("40"), sensitivity=SENSITIVITY_LOW,
        quotes=MARKET, qty_sold_12m=5, annual_sales=Decimal("400"),
    )

    assert result.action == ACTION_INCREASE
    assert result.recommended_price == Decimal("100.00")
    assert result.rule_applied == RULE_LOW_MINIMAL_100


def test_a_gap_below_the_trigger_is_held() -> None:
    """4% below market is not far enough to be worth a price change on a
    high-sensitivity part, which requires 5%."""
    result = recommend(
        current_price=Decimal("96"), cost=Decimal("40"), sensitivity=SENSITIVITY_HIGH,
        quotes=MARKET, qty_sold_12m=500,
    )

    assert result.action == ACTION_HOLD
    assert result.recommended_price == Decimal("96")


def test_an_unsupported_outlier_produces_no_price_change() -> None:
    result = recommend(
        current_price=Decimal("100"), cost=Decimal("40"), sensitivity=SENSITIVITY_MEDIUM,
        quotes=[
            CompetitorQuote("Partzilla", Decimal("95")),
            CompetitorQuote("MotoSport", Decimal("98")),
            CompetitorQuote("Chaparral", Decimal("52")),
        ],
        qty_sold_12m=100,
    )

    assert result.action == ACTION_NEEDS_RESEARCH
    assert result.recommended_price == Decimal("100")


def test_the_ladder_rises_as_economic_importance_falls() -> None:
    """The point of the ladder, stated as one comparison."""
    prices = {}
    for sensitivity, qty in ((SENSITIVITY_HIGH, 500), (SENSITIVITY_MEDIUM, 200), (SENSITIVITY_LOW, 100)):
        prices[sensitivity] = recommend(
            current_price=Decimal("80"), cost=Decimal("40"), sensitivity=sensitivity,
            quotes=MARKET, qty_sold_12m=qty,
        ).recommended_price
    minimal = recommend(
        current_price=Decimal("80"), cost=Decimal("40"), sensitivity=SENSITIVITY_LOW,
        quotes=MARKET, qty_sold_12m=5, annual_sales=Decimal("400"),
    ).recommended_price

    assert prices[SENSITIVITY_HIGH] < prices[SENSITIVITY_MEDIUM] < prices[SENSITIVITY_LOW] < minimal


def test_exposure_alone_can_qualify_a_slow_moving_part() -> None:
    """A part selling 20 units with a $30 gap has $600 at stake, which is worth
    protecting even though the unit count is low."""
    result = recommend(
        current_price=Decimal("70"), cost=Decimal("30"), sensitivity=SENSITIVITY_LOW,
        quotes=MARKET, qty_sold_12m=20, annual_sales=Decimal("1400"),
    )

    assert result.rule_applied == RULE_LOW_EXPOSURE_995


def test_weak_competitor_data_stops_a_minimal_part_matching_the_market() -> None:
    """With one source there is no corroboration, so matching a figure that may
    be wrong is too far to go."""
    result = recommend(
        current_price=Decimal("80"), cost=Decimal("40"), sensitivity=SENSITIVITY_LOW,
        quotes=[CompetitorQuote("Partzilla", Decimal("100"))],
        qty_sold_12m=5, annual_sales=Decimal("400"),
    )

    assert result.market.confidence == "LOW"
    assert result.recommended_price < Decimal("100.00")


def test_the_percentage_is_applied_before_rounding() -> None:
    """From the specification: 189.99 * 0.98 is 186.1902, so the price is
    $186.19. Rounding the multiplier first would give a different answer."""
    result = recommend(
        current_price=Decimal("150.00"), cost=Decimal("70.00"), sensitivity=SENSITIVITY_HIGH,
        quotes=[
            CompetitorQuote("Partzilla", Decimal("189.99")),
            CompetitorQuote("MotoSport", Decimal("192.00")),
            CompetitorQuote("Chaparral", Decimal("191.00")),
        ],
        qty_sold_12m=500,
    )

    assert result.recommended_price == Decimal("186.19")


def test_the_explanation_names_the_target_used() -> None:
    """A price landing at 99.5% rather than 100% has to be auditable from the
    explanation alone."""
    result = recommend(
        current_price=Decimal("80"), cost=Decimal("40"), sensitivity=SENSITIVITY_LOW,
        quotes=MARKET, qty_sold_12m=100,
    )

    assert "99.5%" in result.reason
    assert "sells enough" in result.reason
