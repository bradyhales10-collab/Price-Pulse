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

HIGH_THRESHOLD = 70
MEDIUM_THRESHOLD = 40

# Sales evidence carries 80 of the 90 points available, so what a part actually
# sells decides its treatment. Category can move it by at most 10, and only
# when the classification was confident. An auto-generated category being wrong
# should not be able to misprice a part that objectively sells.
QUANTITY_TIERS: tuple[tuple[int, int], ...] = (
    (2000, 40), (1000, 35), (500, 30), (250, 25), (100, 20), (50, 10),
)
SALES_TIERS: tuple[tuple[int, int], ...] = (
    (100000, 30), (50000, 25), (20000, 20), (10000, 15), (5000, 10),
)
HIGH_TICKET_TIERS: tuple[tuple[str, int], ...] = (("500", 10), ("200", 7), ("100", 4))

# Sales figures that settle the matter on their own, whatever the category says.
# A bolt selling 4,500 a year is an important part; an oil filter selling 8 is
# not, however confidently either was classified.
SALES_FLOOR_MEDIUM_QTY = 1000
SALES_FLOOR_MEDIUM_SALES = Decimal("50000")
SALES_FLOOR_HIGH_QTY = 2000
SALES_FLOOR_HIGH_SALES = Decimal("100000")

# Below this the classification is a guess, so it must not affect the score at
# all rather than affecting it a little.
CATEGORY_CONFIDENCE_REQUIRED = 0.80

# Category is inferred from a product name and often cannot be determined at
# all, so it adjusts the score rather than deciding it. At the original +20/-15
# it could move a part between HIGH and LOW on its own while sales were
# identical, which put more weight on a guess than on measured demand. Sales
# and volume are facts; the category is an inference, and the scoring now
# reflects that difference.
PRICE_IMAGE_BONUS = 10
LOW_SENSITIVITY_PENALTY = 10

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
    for threshold, points in QUANTITY_TIERS:
        if qty >= threshold:
            return points, f"+{points} sold {qty:,} in 12 months"
    return 5, f"+5 sold {qty:,} in 12 months"


def _sales_points(sales: Decimal | None) -> tuple[int, str | None]:
    if sales is None:
        return 0, None
    for threshold, points in SALES_TIERS:
        if sales >= Decimal(threshold):
            return points, f"+{points} annual sales ${sales:,.0f}"
    return 5, f"+5 annual sales ${sales:,.0f}"


def _high_ticket_points(price: Decimal | None) -> tuple[int, str | None]:
    """A costly part gets compared even when few are sold."""
    if price is None:
        return 0, None
    for threshold, points in HIGH_TICKET_TIERS:
        if price >= Decimal(threshold):
            return points, f"+{points} high ticket at ${price:,.2f}"
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

    # What the measured evidence alone says, with no category inference in it.
    sales_evidence_score = score

    # Category adjusts, never decides, and only when the classification was
    # confident. Below that it is a guess, and a guess must not move the score
    # at all rather than moving it a little. This is what stops a bolt selling
    # 4,500 a year being treated as unimportant because it is a bolt, and an
    # oil filter selling 8 being treated as critical because it is a filter.
    if not category_is_confident:
        factors.append("category not confident enough to use, so sales decide this")
    elif category in PRICE_IMAGE_CATEGORIES:
        score += PRICE_IMAGE_BONUS
        factors.append(f"+{PRICE_IMAGE_BONUS} {category} is routinely price shopped")
    elif category in LOW_SENSITIVITY_CATEGORIES:
        high_volume = qty_sold_12m is not None and qty_sold_12m >= HIGH_VOLUME_OVERRIDE_QTY
        low_ticket = current_price is None or current_price < LOW_TICKET_CEILING
        if high_volume:
            factors.append(f"no reduction for {category}: volume of {qty_sold_12m:,} means it is shopped anyway")
        elif not low_ticket:
            factors.append(f"no reduction for {category}: at ${current_price:,.2f} it is not a throwaway item")
        else:
            score -= LOW_SENSITIVITY_PENALTY
            factors.append(f"-{LOW_SENSITIVITY_PENALTY} {category} is rarely price shopped")

    confidence = "HIGH" if category_is_confident else "LOW"

    # Sales figures that settle the matter regardless of anything else. These
    # are floors, not adjustments: a part selling this much is economically
    # important whatever it is called, and no classification error should be
    # able to hide that.
    floor: str | None = None
    if (qty_sold_12m is not None and qty_sold_12m >= SALES_FLOOR_HIGH_QTY) or (
        annual_sales is not None and annual_sales >= SALES_FLOOR_HIGH_SALES
    ):
        floor = SENSITIVITY_HIGH
        if score < high_threshold:
            score = high_threshold
            factors.append("sales alone make this important enough to treat as Price Image")
    elif (qty_sold_12m is not None and qty_sold_12m >= SALES_FLOOR_MEDIUM_QTY) or (
        annual_sales is not None and annual_sales >= SALES_FLOOR_MEDIUM_SALES
    ):
        floor = SENSITIVITY_MEDIUM
        if score < medium_threshold:
            score = medium_threshold
            factors.append("sales alone make this at least Balanced")

    if score >= high_threshold:
        sensitivity = SENSITIVITY_HIGH
    elif score >= medium_threshold:
        sensitivity = SENSITIVITY_MEDIUM
    else:
        sensitivity = SENSITIVITY_LOW

    if floor == SENSITIVITY_HIGH:
        sensitivity = SENSITIVITY_HIGH
    elif floor == SENSITIVITY_MEDIUM and sensitivity == SENSITIVITY_LOW:
        sensitivity = SENSITIVITY_MEDIUM

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
