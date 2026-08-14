"""Sensitivity decides how aggressively a price may move, so these cases check
the judgement calls rather than just the arithmetic. Figures are drawn from the
real Polaris upload.
"""

from __future__ import annotations

from decimal import Decimal

from app.categorization import (
    CATEGORY_DRIVETRAIN,
    CATEGORY_HARDWARE,
    CATEGORY_MAINTENANCE,
    CATEGORY_SEALS,
    CATEGORY_UNKNOWN,
)
from app.sensitivity import (
    SENSITIVITY_HIGH,
    SENSITIVITY_LOW,
    SENSITIVITY_MEDIUM,
    derive_annual_sales,
    score_sensitivity,
)


def test_a_high_volume_drive_belt_is_price_image() -> None:
    """8,073 sold at $189.99, the real top seller. Customers compare this."""
    result = score_sensitivity(
        category=CATEGORY_DRIVETRAIN,
        qty_sold_12m=8073,
        annual_sales=Decimal("1533789"),
        current_price=Decimal("189.99"),
    )

    assert result.sensitivity == SENSITIVITY_HIGH
    assert result.is_price_image is True


def test_the_same_fastener_scores_differently_on_volume_alone() -> None:
    """A dowel pin is normally ignored on price, but 8,000 of them a year is
    shopped whatever it is. Category alone must not decide this."""
    busy = score_sensitivity(
        category=CATEGORY_HARDWARE,
        qty_sold_12m=8000,
        annual_sales=Decimal("19840"),
        current_price=Decimal("2.48"),
    )
    quiet = score_sensitivity(
        category=CATEGORY_HARDWARE,
        qty_sold_12m=12,
        annual_sales=Decimal("30"),
        current_price=Decimal("2.48"),
    )

    assert busy.score > quiet.score
    assert quiet.sensitivity == SENSITIVITY_LOW
    assert any("no reduction" in factor for factor in busy.factors)
    assert any("rarely price shopped" in factor for factor in quiet.factors)


def test_an_expensive_seal_does_not_get_the_throwaway_reduction() -> None:
    """The reduction assumes a cheap part nobody examines. At $145 that
    assumption does not hold."""
    result = score_sensitivity(
        category=CATEGORY_SEALS,
        qty_sold_12m=30,
        annual_sales=Decimal("4350"),
        current_price=Decimal("145.00"),
    )

    assert not any("rarely price shopped" in factor for factor in result.factors)


def test_a_costly_part_is_shopped_even_when_few_are_sold() -> None:
    result = score_sensitivity(
        category=CATEGORY_UNKNOWN,
        qty_sold_12m=3,
        annual_sales=Decimal("3897"),
        current_price=Decimal("1298.99"),
    )

    assert any("high ticket" in factor for factor in result.factors)


def test_an_unconfident_category_does_not_push_a_modest_part_to_an_extreme() -> None:
    """A weak category must not drive an aggressive price change on a part
    whose sales do not justify it."""
    result = score_sensitivity(
        category=CATEGORY_UNKNOWN,
        qty_sold_12m=60,
        annual_sales=Decimal("3000"),
        current_price=Decimal("50"),
        category_is_confident=False,
    )

    assert result.sensitivity == SENSITIVITY_MEDIUM
    assert result.confidence == "LOW"
    assert any("held toward Balanced" in factor for factor in result.factors)


def test_measured_sales_still_count_when_the_category_is_unknown() -> None:
    """Categories are missing on plenty of real parts, and a failed guess must
    not drag down a part that objectively sells in volume. Sales are measured
    fact; the category is an inference, so the inference gives way."""
    result = score_sensitivity(
        category=CATEGORY_UNKNOWN,
        qty_sold_12m=8073,
        annual_sales=Decimal("1533789"),
        current_price=Decimal("189.99"),
        category_is_confident=False,
    )

    assert result.sensitivity == SENSITIVITY_HIGH
    assert any("sales alone justify" in factor for factor in result.factors)


def test_category_adjusts_the_score_rather_than_deciding_it() -> None:
    """With identical sales, the category must not be able to move a part
    between HIGH and LOW on its own. It was originally able to, which put more
    weight on a guess than on measured demand."""
    from app.categorization import CATEGORY_MAINTENANCE as PRICE_IMAGE

    shared = {
        "qty_sold_12m": 300,
        "annual_sales": Decimal("15000"),
        "current_price": Decimal("50"),
    }
    price_image = score_sensitivity(category=PRICE_IMAGE, **shared)
    hardware = score_sensitivity(category=CATEGORY_HARDWARE, **shared)

    assert price_image.sensitivity == hardware.sensitivity
    assert price_image.score > hardware.score


def test_a_part_with_no_history_defaults_to_balanced() -> None:
    """Missing sales data must not fail the run or imply low sensitivity."""
    result = score_sensitivity(category=CATEGORY_UNKNOWN)

    assert result.sensitivity == SENSITIVITY_MEDIUM
    assert result.confidence == "LOW"


def test_annual_sales_are_derived_when_not_supplied() -> None:
    assert derive_annual_sales(None, 100, Decimal("10.00")) == Decimal("1000.00")
    assert derive_annual_sales(Decimal("5"), 100, Decimal("10")) == Decimal("5")
    assert derive_annual_sales(None, None, Decimal("10")) is None
    assert derive_annual_sales(None, 100, None) is None


def test_the_factors_explain_the_score() -> None:
    """A recommendation has to justify itself, so the score must carry its
    reasoning rather than being an unexplained number."""
    result = score_sensitivity(
        category=CATEGORY_MAINTENANCE,
        qty_sold_12m=482,
        annual_sales=Decimal("29759"),
        current_price=Decimal("61.74"),
    )

    assert result.factors
    assert "Sensitivity" in result.summary
    assert any("routinely price shopped" in factor for factor in result.factors)
