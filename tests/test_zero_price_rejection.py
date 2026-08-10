"""A competitor price of zero must never be recorded.

Two real parts recorded Chaparral at $0.00, and because it was the lowest of
the four competitor prices it became the Lowest_Competitor_Price in the export.
That feeds the gap calculation and the suggested price, so a zero would drive a
pricing decision to an absurd conclusion. No price is correct here; a wrong
price is not.

Rejected at two layers on purpose. Parsing catches it at the source with a
warning naming why. money_to_cents catches it again because that is the single
point every stored price passes through, including imported results and any
future source that does not go through the parser.
"""

from __future__ import annotations

from decimal import Decimal

from app.database import money_to_cents
from app.parsers.money_parser import parse_money


def test_a_zero_price_is_not_parsed_as_a_price() -> None:
    result = parse_money("$0.00")

    assert result.value is None
    assert "non_positive_price_rejected" in result.warnings


def test_zero_in_other_forms_is_also_rejected() -> None:
    for text in ("$0", "$0.00", "$ 0.00", "$0.0"):
        result = parse_money(text)
        assert result.value is None, text


def test_a_negative_price_is_rejected_rather_than_read_as_positive() -> None:
    """A leading minus sits outside the matched amount, so "-$5.00" would
    otherwise have been read as positive 5."""
    result = parse_money("-$5.00")

    assert result.value is None
    assert "non_positive_price_rejected" in result.warnings


def test_real_prices_are_unaffected() -> None:
    assert parse_money("$177.66").value == Decimal("177.66")
    assert parse_money("$1,298.99").value == Decimal("1298.99")
    assert parse_money("$0.01").value == Decimal("0.01")


def test_the_storage_layer_also_refuses_a_non_positive_price() -> None:
    assert money_to_cents("0.00") is None
    assert money_to_cents("0") is None
    assert money_to_cents("-5.00") is None
    assert money_to_cents(Decimal("0")) is None
    assert money_to_cents(Decimal("-1.50")) is None


def test_the_storage_layer_still_stores_real_prices() -> None:
    assert money_to_cents("177.66") == 17766
    assert money_to_cents(Decimal("1298.99")) == 129899
    assert money_to_cents("0.01") == 1
    assert money_to_cents(None) is None
    assert money_to_cents("") is None


def test_a_cleared_zero_cannot_become_the_lowest_competitor_price() -> None:
    """The specific harm: with Partzilla at 177.66 and MotoSport at 190.35, a
    Chaparral zero was reported as the lowest competitor price."""
    prices = {"partzilla": money_to_cents("177.66"), "motosport": money_to_cents("190.35"),
              "chaparral": money_to_cents("0.00")}
    real = {key: value for key, value in prices.items() if value is not None}

    assert "chaparral" not in real
    assert min(real.values()) == 17766
