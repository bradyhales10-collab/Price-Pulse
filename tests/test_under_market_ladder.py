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
    RULE_LOW_MEANINGFUL_995,
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
    assert result.rule_applied == RULE_LOW_MEANINGFUL_995


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

    assert result.rule_applied == RULE_LOW_MEANINGFUL_995


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


def test_a_high_ticket_part_keeps_a_small_advantage_despite_low_volume() -> None:
    """The change this specification adds. One unit a year is minimal volume,
    but customers comparison shop a $1,200 purchase, and half a percent of
    $1,500 is $7.50 to give up for a visible position below the market."""
    result = recommend(
        current_price=Decimal("1200"), cost=Decimal("600"), sensitivity=SENSITIVITY_LOW,
        quotes=[
            CompetitorQuote("Partzilla", Decimal("1500")),
            CompetitorQuote("MotoSport", Decimal("1550")),
            CompetitorQuote("Chaparral", Decimal("1520")),
        ],
        qty_sold_12m=1, annual_sales=Decimal("1200"),
    )

    assert result.rule_applied == RULE_LOW_MEANINGFUL_995
    assert result.recommended_price == Decimal("1492.50")
    assert "High Ticket" in result.target_tier_qualification


def test_qualifying_by_annual_sales_alone() -> None:
    result = recommend(
        current_price=Decimal("400"), cost=Decimal("200"), sensitivity=SENSITIVITY_LOW,
        quotes=[
            CompetitorQuote("Partzilla", Decimal("500")),
            CompetitorQuote("MotoSport", Decimal("520")),
            CompetitorQuote("Chaparral", Decimal("510")),
        ],
        qty_sold_12m=20, annual_sales=Decimal("8000"),
    )

    assert result.rule_applied == RULE_LOW_MEANINGFUL_995
    assert result.recommended_price == Decimal("497.50")


def test_the_qualification_names_why_the_tier_was_reached() -> None:
    """A 99.5% recommendation has to be auditable without working back through
    the thresholds by hand."""
    result = recommend(
        current_price=Decimal("80"), cost=Decimal("40"), sensitivity=SENSITIVITY_LOW,
        quotes=MARKET, qty_sold_12m=75,
    )

    assert "Qty >= 50" in result.target_tier_qualification


def test_every_outcome_names_the_rule_that_produced_it() -> None:
    """A blank rule column makes a recommendation impossible to audit, so no
    path may leave it empty."""
    cases = [
        dict(current_price=Decimal("80"), cost=Decimal("40"), sensitivity=SENSITIVITY_HIGH,
             quotes=MARKET, qty_sold_12m=500),
        dict(current_price=Decimal("96"), cost=Decimal("40"), sensitivity=SENSITIVITY_HIGH,
             quotes=MARKET, qty_sold_12m=500),
        dict(current_price=Decimal("130"), cost=Decimal("50"), sensitivity=SENSITIVITY_MEDIUM,
             quotes=MARKET, qty_sold_12m=100),
        dict(current_price=Decimal("8.35"), cost=Decimal("4"), sensitivity=SENSITIVITY_MEDIUM,
             quotes=[CompetitorQuote("A", Decimal("7.17")), CompetitorQuote("B", Decimal("7.60"))],
             qty_sold_12m=7000),
        dict(current_price=Decimal("50"), cost=Decimal("30"), sensitivity=SENSITIVITY_HIGH,
             manufacturer="KTM", quotes=MARKET),
        dict(current_price=Decimal("100"), cost=Decimal("40"), sensitivity=SENSITIVITY_MEDIUM,
             quotes=[CompetitorQuote("A", Decimal("95")), CompetitorQuote("B", Decimal("98")),
                     CompetitorQuote("C", Decimal("52"))]),
        dict(current_price=Decimal("50"), cost=Decimal("20"), sensitivity=SENSITIVITY_MEDIUM,
             quotes=[CompetitorQuote("A", None)]),
    ]
    for case in cases:
        result = recommend(**case)
        assert result.rule_applied, f"no rule for action {result.action}"


def test_the_competitive_target_is_reported_separately_from_the_final_price() -> None:
    """So management can see how much the margin floor moved the competitive
    recommendation, rather than only the number it landed on."""
    result = recommend(
        current_price=Decimal("80"), cost=Decimal("40"), sensitivity=SENSITIVITY_HIGH,
        quotes=MARKET, qty_sold_12m=500,
    )

    assert result.competitive_target_price == Decimal("98.00")
