"""The new pricing engine, checked against the worked examples in the pricing
specification and against real parts from the Polaris upload.

This engine runs alongside the existing rules engine and changes no stored
prices, so these tests are about whether its judgement is defensible rather
than whether output changed.
"""

from __future__ import annotations

from decimal import Decimal

from app.pricing_engine import (
    ACTION_DECREASE,
    ACTION_DECREASE_REVIEW,
    ACTION_HOLD,
    ACTION_INCREASE,
    ACTION_MAP_EXCLUDED,
    ACTION_NEEDS_RESEARCH,
    CompetitorQuote,
    assess_market,
    dollar_tolerance,
    recommend,
)
from app.sensitivity import SENSITIVITY_HIGH, SENSITIVITY_LOW, SENSITIVITY_MEDIUM


def test_a_large_percentage_on_a_cheap_part_is_held() -> None:
    """From the specification: $4.49 against $3.99 is 12.5%, which sounds
    urgent, but the customer saves fifty cents. Cutting price there gives away
    margin for a difference nobody notices."""
    result = recommend(
        current_price=Decimal("4.49"),
        cost=Decimal("2.00"),
        sensitivity=SENSITIVITY_LOW,
        quotes=[CompetitorQuote("Partzilla", Decimal("3.99")), CompetitorQuote("Chaparral", Decimal("4.10"))],
    )

    assert result.action == ACTION_HOLD
    assert result.recommended_price == Decimal("4.49")


def test_a_trivial_difference_on_an_expensive_part_is_held() -> None:
    """From the specification: $85.02 against $84.76. Do not cut price merely
    to technically match."""
    result = recommend(
        current_price=Decimal("85.02"),
        cost=Decimal("50.00"),
        sensitivity=SENSITIVITY_MEDIUM,
        quotes=[CompetitorQuote("Partzilla", Decimal("84.76")), CompetitorQuote("MotoSport", Decimal("88.00"))],
    )

    assert result.action == ACTION_HOLD


def test_a_genuinely_overpriced_price_image_part_is_reduced() -> None:
    """From the specification: $74.39 against a market near $55 on a
    price-image part is a real comparison-shopping risk."""
    result = recommend(
        current_price=Decimal("74.39"),
        cost=Decimal("30.00"),
        sensitivity=SENSITIVITY_HIGH,
        quotes=[
            CompetitorQuote("Partzilla", Decimal("55.00")),
            CompetitorQuote("MotoSport", Decimal("56.20")),
            CompetitorQuote("Chaparral", Decimal("54.99")),
        ],
    )

    assert result.action == ACTION_DECREASE
    assert result.recommended_price < Decimal("74.39")
    # Stays just above the lowest rather than undercutting it.
    assert result.recommended_price > Decimal("54.99")


def test_being_well_under_market_raises_but_stays_below_the_lowest() -> None:
    """Real drive belt figures. The point is to capture margin without
    becoming the most expensive option."""
    result = recommend(
        current_price=Decimal("163.99"),
        cost=Decimal("107.19"),
        sensitivity=SENSITIVITY_HIGH,
        quotes=[
            CompetitorQuote("Partzilla", Decimal("189.99")),
            CompetitorQuote("MotoSport", Decimal("192.50")),
            CompetitorQuote("Chaparral", Decimal("188.00")),
        ],
    )

    assert result.action == ACTION_INCREASE
    assert Decimal("163.99") < result.recommended_price < Decimal("188.00")


def test_being_slightly_under_market_is_left_alone() -> None:
    """Not far enough below to be worth the risk of moving."""
    result = recommend(
        current_price=Decimal("98.00"),
        cost=Decimal("50.00"),
        sensitivity=SENSITIVITY_HIGH,
        quotes=[CompetitorQuote("Partzilla", Decimal("100.00")), CompetitorQuote("MotoSport", Decimal("101.00"))],
    )

    assert result.action == ACTION_HOLD


def test_the_margin_floor_stops_an_automatic_cut() -> None:
    """Matching the market would earn too little, so this becomes a decision
    rather than an automatic reduction. With a cost of 60 the target leaves
    around 25%, inside the review band rather than below the 18% point where
    nothing automatic happens at all."""
    result = recommend(
        current_price=Decimal("100.00"),
        cost=Decimal("60.00"),
        sensitivity=SENSITIVITY_HIGH,
        minimum_margin_pct=Decimal("30"),
        quotes=[
            CompetitorQuote("Partzilla", Decimal("78.00")),
            CompetitorQuote("MotoSport", Decimal("79.00")),
            CompetitorQuote("Chaparral", Decimal("77.50")),
        ],
    )

    assert result.action == ACTION_DECREASE_REVIEW
    assert result.projected_margin_pct >= Decimal("30")


def test_a_price_leaving_too_little_margin_is_not_reduced_automatically() -> None:
    """Three bands, not one floor. Matching the market here would leave 12%,
    below the 18% point where a reduction stops being worth making, so nothing
    automatic happens and a person decides whether the sale is worth it."""
    result = recommend(
        current_price=Decimal("100.00"), cost=Decimal("70.00"), sensitivity=SENSITIVITY_HIGH,
        quotes=[CompetitorQuote("A", Decimal("78.00")), CompetitorQuote("B", Decimal("79.00")),
                CompetitorQuote("C", Decimal("77.50"))],
    )

    assert result.action == ACTION_HOLD
    assert "needs approval" in result.reason


def test_a_price_in_the_review_band_asks_for_a_decision() -> None:
    """Between the floor and the review point, a reduction is possible but
    should not happen on its own."""
    result = recommend(
        current_price=Decimal("100.00"), cost=Decimal("60.00"), sensitivity=SENSITIVITY_HIGH,
        minimum_margin_pct=Decimal("30"),
        quotes=[CompetitorQuote("A", Decimal("78.00")), CompetitorQuote("B", Decimal("79.00")),
                CompetitorQuote("C", Decimal("77.50"))],
    )

    assert result.action == ACTION_DECREASE_REVIEW
    assert result.projected_margin_pct >= Decimal("30")


def test_a_zero_competitor_price_is_never_treated_as_the_market() -> None:
    """The real case that produced a $0.00 lowest competitor in an export."""
    result = recommend(
        current_price=Decimal("177.66"),
        cost=Decimal("100.00"),
        sensitivity=SENSITIVITY_MEDIUM,
        quotes=[
            CompetitorQuote("Partzilla", Decimal("177.66")),
            CompetitorQuote("MotoSport", Decimal("190.35")),
            CompetitorQuote("Chaparral", Decimal("0.00")),
        ],
    )

    assert result.market.lowest == Decimal("177.66")
    assert any("zero" in reason for _, reason in result.market.rejected)


def test_a_competitor_price_below_our_cost_is_not_trusted() -> None:
    """Repricing against this would be a real loss, and it usually means a
    different item rather than a real price."""
    market = assess_market(
        [CompetitorQuote("Partzilla", Decimal("30.00")), CompetitorQuote("MotoSport", Decimal("95.00"))],
        our_cost=Decimal("50.00"),
    )

    assert market.lowest == Decimal("95.00")
    assert any("below our cost" in reason for _, reason in market.rejected)


def test_a_lone_low_outlier_asks_for_a_look_rather_than_a_price_cut() -> None:
    result = recommend(
        current_price=Decimal("100.00"),
        cost=Decimal("40.00"),
        sensitivity=SENSITIVITY_MEDIUM,
        quotes=[
            CompetitorQuote("Partzilla", Decimal("95.00")),
            CompetitorQuote("MotoSport", Decimal("98.00")),
            CompetitorQuote("Chaparral", Decimal("52.00")),
        ],
    )

    assert result.action == ACTION_NEEDS_RESEARCH
    assert result.recommended_price == Decimal("100.00")


def test_no_trustworthy_competitor_means_no_recommendation() -> None:
    result = recommend(
        current_price=Decimal("50.00"),
        cost=Decimal("20.00"),
        sensitivity=SENSITIVITY_MEDIUM,
        quotes=[CompetitorQuote("Partzilla", None), CompetitorQuote("MotoSport", Decimal("10.00"), in_stock=False)],
    )

    assert result.action == ACTION_NEEDS_RESEARCH


def test_ktm_is_excluded_from_automatic_pricing() -> None:
    result = recommend(
        current_price=Decimal("50.00"),
        cost=Decimal("30.00"),
        sensitivity=SENSITIVITY_HIGH,
        manufacturer="KTM",
        quotes=[CompetitorQuote("Partzilla", Decimal("40.00")), CompetitorQuote("MotoSport", Decimal("41.00"))],
    )

    assert result.action == ACTION_MAP_EXCLUDED
    assert result.recommended_price == Decimal("50.00")
    # Competitor prices are still gathered for reference.
    assert result.market.lowest == Decimal("40.00")


def test_sensitivity_changes_how_close_to_market_a_part_sits() -> None:
    """The whole purpose of scoring sensitivity: a shopped part stays near the
    market, one nobody compares can hold more margin."""
    quotes = [
        CompetitorQuote("Partzilla", Decimal("100.00")),
        CompetitorQuote("MotoSport", Decimal("104.00")),
        CompetitorQuote("Chaparral", Decimal("102.00")),
    ]
    high = recommend(current_price=Decimal("130.00"), cost=Decimal("50.00"), sensitivity=SENSITIVITY_HIGH, quotes=quotes)
    low = recommend(current_price=Decimal("130.00"), cost=Decimal("50.00"), sensitivity=SENSITIVITY_LOW, quotes=quotes)

    assert high.recommended_price < low.recommended_price


def test_competitor_confidence_reflects_how_much_agreement_there_is() -> None:
    three = assess_market([CompetitorQuote("A", Decimal("100")), CompetitorQuote("B", Decimal("101")),
                           CompetitorQuote("C", Decimal("102"))])
    two = assess_market([CompetitorQuote("A", Decimal("100")), CompetitorQuote("B", Decimal("101"))])
    one = assess_market([CompetitorQuote("A", Decimal("100"))])

    assert three.confidence == "HIGH"
    assert two.confidence == "MEDIUM"
    assert one.confidence == "LOW"


def test_dollar_tolerance_scales_with_price() -> None:
    assert dollar_tolerance(Decimal("8.35")) == Decimal("1.50")
    assert dollar_tolerance(Decimal("20.00")) == Decimal("2.50")
    assert dollar_tolerance(Decimal("40.00")) == Decimal("4.00")
    assert dollar_tolerance(Decimal("60.00")) == Decimal("6.00")
    # Above the bands it becomes proportional, with a floor.
    assert dollar_tolerance(Decimal("500.00")) == Decimal("15.00")
    assert dollar_tolerance(Decimal("80.00")) == Decimal("5")


def test_every_recommendation_explains_itself() -> None:
    """A recommendation nobody can follow will not be trusted or used."""
    result = recommend(
        current_price=Decimal("74.39"), cost=Decimal("30.00"), sensitivity=SENSITIVITY_HIGH,
        quotes=[CompetitorQuote("Partzilla", Decimal("55.00")), CompetitorQuote("MotoSport", Decimal("56.20")),
                CompetitorQuote("Chaparral", Decimal("54.99"))],
    )

    assert len(result.reason) > 40
    assert "$" in result.reason
    assert result.rule_version.startswith("OEM-HYBRID-")


def test_the_same_small_gap_is_reviewed_when_volume_makes_it_add_up() -> None:
    """From the revised specification: $8.35 against $7.17 is $1.18, inside the
    tolerance for a cheap part. On 15 units that is nothing. On 7,000 it is
    thousands of dollars and every one of those customers saw the difference."""
    quotes = [CompetitorQuote("Partzilla", Decimal("7.17")), CompetitorQuote("MotoSport", Decimal("7.60"))]

    quiet = recommend(
        current_price=Decimal("8.35"), cost=Decimal("4.00"), sensitivity=SENSITIVITY_MEDIUM,
        quotes=quotes, qty_sold_12m=15,
    )
    busy = recommend(
        current_price=Decimal("8.35"), cost=Decimal("4.00"), sensitivity=SENSITIVITY_MEDIUM,
        quotes=quotes, qty_sold_12m=7000,
    )

    assert quiet.action == ACTION_HOLD
    assert busy.action == "HIGH_SALES_PRICE_REVIEW"
    assert "7,000 units" in busy.reason


def test_a_tiny_percentage_gap_is_not_chased_even_on_a_price_image_part() -> None:
    """The revised specification requires a percentage gap above 2% as well as
    a meaningful dollar gap before reducing a high-sensitivity part."""
    result = recommend(
        current_price=Decimal("101.50"), cost=Decimal("40.00"), sensitivity=SENSITIVITY_HIGH,
        quotes=[
            CompetitorQuote("Partzilla", Decimal("100.00")),
            CompetitorQuote("MotoSport", Decimal("103.00")),
            CompetitorQuote("Chaparral", Decimal("102.00")),
        ],
    )

    assert result.action == ACTION_HOLD
