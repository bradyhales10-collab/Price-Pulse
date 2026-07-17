from __future__ import annotations

from decimal import Decimal

from app.parsers.money_parser import parse_money


def test_parses_standard_money() -> None:
    result = parse_money("$282.32")

    assert result.value == Decimal("282.32")
    assert result.warnings == []


def test_parses_money_with_comma() -> None:
    result = parse_money("$1,248.11")

    assert result.value == Decimal("1248.11")
    assert result.warnings == []


def test_malformed_money_returns_warning() -> None:
    result = parse_money("not money", warning_code="msrp_parse_failed")

    assert result.value is None
    assert result.warnings == ["msrp_parse_failed"]
