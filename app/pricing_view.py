"""Produce the new engine's recommendation for a row the interface already has.

The pricing engine, categorisation and sensitivity scoring are deliberately
independent of the database. This is the one place that joins them to a
comparison row, so every screen shows the same recommendation rather than each
assembling its own and quietly diverging.

Nothing here writes. The existing suggestion is untouched and both are shown
side by side, because the point is to judge the new engine on real parts before
trusting it with anything.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.categorization import categorize_product
from app.competitors.registry import list_competitors, short_display_name
from app.pricing_engine import CompetitorQuote, recommend
from app.pricing_rules import list_pricing_rules
from app.sales_period import DEFAULT_SALES_PERIOD, annualize_quantity
from app.sensitivity import derive_annual_sales, score_sensitivity

# How an action should read to someone scanning a list, and how it should look.
ACTION_LABELS: dict[str, str] = {
    "INCREASE": "Raise price",
    "DECREASE": "Lower price",
    "HOLD": "Leave as is",
    "DECREASE_REVIEW": "Lower, needs a decision",
    "NEEDS_RESEARCH": "Check this one",
    "MAP_EXCLUDED": "Excluded (MAP)",
}
ACTION_TONE: dict[str, str] = {
    "INCREASE": "success",
    "DECREASE": "warning",
    "HOLD": "neutral",
    "DECREASE_REVIEW": "warning",
    "NEEDS_RESEARCH": "warning",
    "MAP_EXCLUDED": "neutral",
}


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def minimum_margin_pct(database: Path) -> Decimal:
    """Read the margin floor from the existing rule rather than duplicating it.

    Keeping one setting means changing it in the rules screen changes both
    engines, instead of the two drifting apart.
    """
    for rule in list_pricing_rules(database):
        if rule.rule_type == "margin_floor":
            return Decimal(str(rule.settings.get("minimum_margin_pct", 20)))
    return Decimal("20")


def recommendation_for_row(row: dict[str, Any], *, minimum_margin: Decimal = Decimal("20")) -> dict[str, Any] | None:
    """The new engine's view of one comparison row, ready for display."""
    current_price = _decimal(row.get("our_current_price"))
    if current_price is None or current_price <= 0:
        return None

    category = categorize_product(row.get("product_name"))

    # A quantity means nothing without the period it covers, so scale it to a
    # year before scoring against annual thresholds.
    period = str(row.get("sales_period") or DEFAULT_SALES_PERIOD)
    scaled = annualize_quantity(_int(row.get("units_sold_12m")), period)
    qty = scaled.annualized_qty

    sensitivity = score_sensitivity(
        category=category.category,
        qty_sold_12m=qty,
        annual_sales=derive_annual_sales(None, qty, current_price),
        current_price=current_price,
        category_is_confident=category.is_confident,
    )

    quotes: list[CompetitorQuote] = []
    for adapter in list_competitors():
        key = adapter.competitor_key
        availability = str(row.get(f"{key}_availability_status") or "").lower()
        quotes.append(
            CompetitorQuote(
                name=short_display_name(adapter),
                price=_decimal(row.get(f"{key}_selling_price")),
                in_stock=availability not in {"out_of_stock", "discontinued"},
            )
        )

    priced = [quote for quote in quotes if quote.price is not None]
    raw_lowest_quote = min(priced, key=lambda quote: quote.price) if priced else None
    raw_lowest = raw_lowest_quote.price if raw_lowest_quote else None
    raw_lowest_name = raw_lowest_quote.name if raw_lowest_quote else ""

    result = recommend(
        current_price=current_price,
        cost=_decimal(row.get("current_cost")),
        sensitivity=sensitivity.sensitivity,
        quotes=quotes,
        manufacturer=str(row.get("manufacturer") or ""),
        minimum_margin_pct=minimum_margin,
        qty_sold_12m=qty,
        annual_sales=derive_annual_sales(None, qty, current_price),
    )

    changes_price = result.recommended_price is not None and result.recommended_price != current_price

    return {
        "action": result.action,
        "action_label": ACTION_LABELS.get(result.action, result.action),
        "action_tone": ACTION_TONE.get(result.action, "neutral"),
        "recommended_price": f"{result.recommended_price:.2f}" if result.recommended_price is not None else "",
        "changes_price": changes_price,
        "reason": result.reason,
        "projected_margin_pct": (
            f"{result.projected_margin_pct:.2f}" if result.projected_margin_pct is not None else ""
        ),
        "category": category.category,
        "category_confidence": category.confidence_class,
        "category_reason": category.reason,
        "sensitivity": sensitivity.sensitivity,
        "sensitivity_score": sensitivity.score,
        "sensitivity_factors": sensitivity.factors,
        "sales_period": period,
        "sales_period_note": scaled.note if scaled.was_scaled else "",
        "annualized_qty": qty,
        "competitor_confidence": result.market.confidence,
        "valid_competitor_count": result.market.valid_count,
        "lowest_valid": f"{result.market.lowest:.2f}" if result.market.lowest is not None else "",
        "median_valid": f"{result.market.median:.2f}" if result.market.median is not None else "",
        "rejected_quotes": [f"{quote.name}: {reason}" for quote, reason in result.market.rejected],
        "rule_version": result.rule_version,
        # Named at length on purpose. "Annual Exposure" reads as lost revenue,
        # which it is not: it is what the current price difference amounts to
        # across a year at historical volume.
        "annual_competitive_price_exposure": _exposure(current_price, result.market.lowest, qty),
        "target_percent_of_lowest": (
            f"{result.target_percent_of_lowest:.1f}" if result.target_percent_of_lowest is not None else ""
        ),
        "rule_applied": result.rule_applied,
        "target_tier_qualification": result.target_tier_qualification,
        "competitive_target_price": (
            f"{result.competitive_target_price:.2f}" if result.competitive_target_price is not None else ""
        ),
        # The raw lowest is shown next to the validated one because a
        # recommendation can look wrong when the engine correctly rejected the
        # cheapest quote. Seeing both makes that visible rather than puzzling.
        "raw_lowest_name": raw_lowest_name,
        "raw_lowest_price": f"{raw_lowest:.2f}" if raw_lowest is not None else "",
        "excluded_competitor_count": len(result.market.rejected),
        "excluded_competitors": " | ".join(
            f"{quote.name} - "
            + (f"${quote.price:.2f}" if quote.price is not None else "no price")
            + f" - Excluded: {reason}"
            for quote, reason in result.market.rejected
        ),
    }


def _exposure(current_price: Decimal, lowest: Decimal | None, qty: int | None) -> str:
    """What the current gap costs across a year of sales.

    A penny per unit is nothing; a penny across 10,000 units is $100 and every
    one of those customers saw the difference.
    """
    if lowest is None or qty is None:
        return ""
    return f"{abs(current_price - lowest) * Decimal(qty):.2f}"
