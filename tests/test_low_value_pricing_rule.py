from __future__ import annotations

from app.pricing_rules import PricingRule, suggest_price


def _rules(threshold: float | int = 5) -> list[PricingRule]:
    return [
        PricingRule(
            "use_lowest_competitor",
            "Use Lowest Competitor",
            "anchor",
            True,
            10,
            {"adjustment_cents": 0},
            "",
        ),
        PricingRule(
            "keep_price_on_low_value_items",
            "Keep Price On Low-Value Items",
            "low_price_floor",
            True,
            40,
            {"minimum_price": threshold},
            "",
        ),
    ]


def _suggest(our: str | None, lowest: str | None, threshold: float | int = 5) -> dict:
    return suggest_price(
        {"our_current_price": our, "lowest_competitor_price": lowest, "current_cost": None},
        _rules(threshold),
    )


def test_cheap_part_is_not_lowered_to_match_a_cheaper_competitor() -> None:
    assert _suggest("3.99", "1.99")["suggested_price"] == "3.99"


def test_price_exactly_at_the_threshold_is_still_protected() -> None:
    assert _suggest("5.00", "1.99")["suggested_price"] == "5.00"


def test_price_just_above_the_threshold_is_lowered_normally() -> None:
    assert _suggest("5.01", "1.99")["suggested_price"] == "1.99"


def test_increases_are_still_allowed_on_cheap_parts() -> None:
    """The rule only blocks decreases. A competitor priced higher should still
    pull our suggestion up."""
    assert _suggest("3.99", "9.99")["suggested_price"] == "9.99"


def test_expensive_parts_are_unaffected() -> None:
    assert _suggest("49.99", "39.99")["suggested_price"] == "39.99"


def test_threshold_is_configurable() -> None:
    assert _suggest("14.99", "9.99", threshold=20)["suggested_price"] == "14.99"
    assert _suggest("14.99", "9.99", threshold=10)["suggested_price"] == "9.99"


def test_missing_our_price_does_not_crash_or_block() -> None:
    result = _suggest(None, "1.99")

    assert result["suggested_price"] == "1.99"


def test_rule_explains_itself_to_the_reviewer() -> None:
    result = _suggest("3.99", "1.99")
    effects = {item["rule_code"]: item["effect"] for item in result["applied_rules"]}

    message = effects["keep_price_on_low_value_items"]
    assert "3.99" in message
    assert "5.00" in message
    assert "do not lower" in message.lower()
