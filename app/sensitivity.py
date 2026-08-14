"""Score how price-sensitive a part is.

Category alone does not decide this, which is why it is scored separately. A
brake pad and a bracket are both parts, but customers comparison-shop one and
not the other; equally, a normally-ignored fastener sold in huge volume does
warrant attention. The score combines how much it sells, what kind of part it
is, and what it costs.

The result drives how aggressively a price may move:

    HIGH   Price Image        stay visibly competitive
    MEDIUM Balanced           modest premium acceptable
    LOW    Margin Opportunity take margin where the customer is not shopping

Every score reports the factors that produced it, so a recommendation can
explain itself rather than asserting a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.categorization import (
    CATEGORY_BRAKES,
    CATEGORY_DRIVETRAIN,
    CATEGORY_ELECTRICAL,
    CATEGORY_FLUIDS,
    CATEGORY_HARDWARE,
    CATEGORY_MAINTENANCE,
    CATEGORY_SEALS,
)

SENSITIVITY_HIGH = "HIGH"
SENSITIVITY_MEDIUM = "MEDIUM"
SENSITIVITY_LOW = "LOW"

HIGH_THRESHOLD = 50
MEDIUM_THRESHOLD = 25

# Categories customers routinely compare on price before buying.
PRICE_IMAGE_CATEGORIES = frozenset(
    {CATEGORY_MAINTENANCE, CATEGORY_FLUIDS, CATEGORY_BRAKES, CATEGORY_DRIVETRAIN, CATEGORY_ELECTRICAL}
)
# Categories rarely shopped on price, usually bought because something broke.
LOW_SENSITIVITY_CATEGORIES = frozenset({CATEGORY_HARDWARE, CATEGORY_SEALS})

# Above this volume a part is shopped regardless of what kind of part it is,
# so the low-sensitivity reduction no longer applies.
HIGH_VOLUME_OVERRIDE_QTY = 250
# Below this price, a part is cheap enough that the category reduction is fair.
LOW_TICKET_CEILING = Decimal("25")


@dataclass(frozen=True)
class SensitivityResult:
    score: int
    sensitivity: str
    factors: list[str] = field(default_factory=list)
    confidence: str = "HIGH"

    @property
    def is_price_image(self) -> bool:
        return self.sensitivity == SENSITIVITY_HIGH

    @property
    def summary(self) -> str:
        return f"Sensitivity {self.score} ({self.sensitivity}): " + "; ".join(self.factors)


def _quantity_points(qty: int | None) -> tuple[int, str | None]:
    if qty is None:
        return 0, None
    for threshold, points in ((1000, 30), (500, 25), (250, 20), (100, 15), (50, 10)):
        if qty >= threshold:
            return points, f"+{points} sold {qty:,} in 12 months"
    return 5, f"+5 sold {qty:,} in 12 months"


def _sales_points(sales: Decimal | None) -> tuple[int, str | None]:
    if sales is None:
        return 0, None
    for threshold, points in ((100000, 25), (50000, 20), (20000, 15), (7500, 10)):
        if sales >= Decimal(threshold):
            return points, f"+{points} annual sales ${sales:,.0f}"
    return 5, f"+5 annual sales ${sales:,.0f}"


def _high_ticket_points(price: Decimal | None) -> tuple[int, str | None]:
    """A costly part gets compared even when few are sold."""
    if price is None:
        return 0, None
    if price >= Decimal("500"):
        return 15, f"+15 high ticket at ${price:,.2f}"
    if price >= Decimal("200"):
        return 10, f"+10 high ticket at ${price:,.2f}"
    return 0, None


def score_sensitivity(
    *,
    category: str,
    qty_sold_12m: int | None = None,
    annual_sales: Decimal | None = None,
    current_price: Decimal | None = None,
    category_is_confident: bool = True,
    high_threshold: int = HIGH_THRESHOLD,
    medium_threshold: int = MEDIUM_THRESHOLD,
) -> SensitivityResult:
    """Score a part and classify it.

    annual_sales may be derived from quantity and price when not supplied
    directly; the caller does that so this stays a pure calculation.
    """
    factors: list[str] = []
    score = 0

    for points, reason in (
        _quantity_points(qty_sold_12m),
        _sales_points(annual_sales),
        _high_ticket_points(current_price),
    ):
        score += points
        if reason:
            factors.append(reason)

    if category in PRICE_IMAGE_CATEGORIES:
        score += 20
        factors.append(f"+20 {category} is routinely price shopped")
    elif category in LOW_SENSITIVITY_CATEGORIES:
        # Volume overrides the category: a fastener selling in the thousands is
        # shopped whatever it is. Price matters too, since an expensive seal is
        # not the throwaway item this reduction assumes.
        high_volume = qty_sold_12m is not None and qty_sold_12m >= HIGH_VOLUME_OVERRIDE_QTY
        low_ticket = current_price is None or current_price < LOW_TICKET_CEILING
        if high_volume:
            factors.append(f"no reduction for {category}: volume of {qty_sold_12m:,} means it is shopped anyway")
        elif not low_ticket:
            factors.append(f"no reduction for {category}: at ${current_price:,.2f} it is not a throwaway item")
        else:
            score -= 15
            factors.append(f"-15 {category} is rarely price shopped")

    confidence = "HIGH"
    if not category_is_confident:
        # An unreliable category must not produce an aggressive recommendation,
        # so anything it would have pushed to an extreme is pulled back toward
        # Balanced and the reduced confidence is recorded.
        confidence = "LOW"
        factors.append("category was not confident, so sensitivity is held toward Balanced")
        score = max(medium_threshold, min(score, high_threshold - 1))

    if score >= high_threshold:
        sensitivity = SENSITIVITY_HIGH
    elif score >= medium_threshold:
        sensitivity = SENSITIVITY_MEDIUM
    else:
        sensitivity = SENSITIVITY_LOW

    if not factors:
        factors.append("no sales history or price available, defaulted to Balanced")
        sensitivity = SENSITIVITY_MEDIUM
        confidence = "LOW"

    return SensitivityResult(score=score, sensitivity=sensitivity, factors=factors, confidence=confidence)


def derive_annual_sales(
    annual_sales: Decimal | None, qty_sold_12m: int | None, current_price: Decimal | None
) -> Decimal | None:
    """Fall back to quantity times price when sales are not supplied."""
    if annual_sales is not None:
        return annual_sales
    if qty_sold_12m is None or current_price is None:
        return None
    return Decimal(qty_sold_12m) * current_price
