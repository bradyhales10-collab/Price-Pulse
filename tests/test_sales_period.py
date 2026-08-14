"""Sensitivity compares demand against fixed thresholds, so the period behind a
quantity changes the pricing treatment. The import field is named
units_sold_12m and matches a column called simply "Qty Sold", with nothing
checking it really covers twelve months.
"""

from __future__ import annotations

from decimal import Decimal

from app.sales_period import (
    DEFAULT_SALES_PERIOD,
    annualize_quantity,
    annualize_sales,
    detect_period_from_header,
)


def test_the_same_number_means_different_demand_on_different_periods() -> None:
    """400 units over three months is four times the demand of 400 over a year,
    and must not be scored as though they were the same."""
    annual = annualize_quantity(400, "12_months")
    half = annualize_quantity(400, "6_months")
    quarter = annualize_quantity(400, "3_months")

    assert annual.annualized_qty == 400
    assert half.annualized_qty == 800
    assert quarter.annualized_qty == 1600


def test_a_twelve_month_figure_is_left_exactly_as_supplied() -> None:
    result = annualize_quantity(8073, "12_months")

    assert result.annualized_qty == 8073
    assert result.was_scaled is False


def test_a_longer_period_is_scaled_down() -> None:
    result = annualize_quantity(2400, "24_months")

    assert result.annualized_qty == 1200
    assert result.was_scaled is True


def test_scaling_is_recorded_because_it_is_an_estimate() -> None:
    """Projecting a year from three months is an estimate, not a measurement,
    and a recommendation built on it should be readable as such."""
    result = annualize_quantity(400, "3_months")

    assert result.was_scaled is True
    assert "scaled up" in result.note
    assert "3 months" in result.note


def test_a_header_that_states_its_period_is_detected() -> None:
    assert detect_period_from_header("Units Sold 12M") == "12_months"
    assert detect_period_from_header("6 Month Sales") == "6_months"
    assert detect_period_from_header("Qty Sold (3 months)") == "3_months"
    assert detect_period_from_header("Annual Units") == "12_months"
    assert detect_period_from_header("Monthly Qty") == "1_month"


def test_twelve_month_beats_one_month_in_a_heading() -> None:
    """"12 month" contains "1 month" as a substring, so order matters."""
    assert detect_period_from_header("12 Month Qty") == "12_months"


def test_an_ambiguous_heading_is_not_guessed() -> None:
    """"Qty Sold" is exactly what the real Polaris upload uses, and it says
    nothing about the period. YTD depends on when the file was produced. A
    wrong guess is worse than asking, so these return nothing."""
    assert detect_period_from_header("Qty Sold") is None
    assert detect_period_from_header("YTD Qty") is None
    assert detect_period_from_header("") is None
    assert detect_period_from_header(None) is None


def test_a_missing_quantity_does_not_fail() -> None:
    result = annualize_quantity(None, "6_months")

    assert result.annualized_qty is None
    assert result.was_scaled is False


def test_sales_amounts_scale_the_same_way() -> None:
    assert annualize_sales(Decimal("10000"), "6_months") == Decimal("20000.00")
    assert annualize_sales(Decimal("10000"), "12_months") == Decimal("10000")
    assert annualize_sales(None, "6_months") is None


def test_the_default_is_annual_which_matches_the_field_name() -> None:
    assert DEFAULT_SALES_PERIOD == "12_months"
    assert annualize_quantity(500).annualized_qty == 500
