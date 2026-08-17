"""Recommend a price from sensitivity, market position and margin.

This runs alongside the existing rules engine rather than replacing it, so
recommendations from both can be compared on real parts before anything is
switched over. Nothing here writes to the database or changes the current
suggestion; call recommend() and read the result.

The question it answers is not "what is the lowest competitor price" but
"given how shopped this part is, how much the difference is actually worth,
and what margin it leaves, what price is most defensible". Two things follow
from that:

Absolute dollars matter as much as percentage. A part at $4.49 against a
competitor at $3.99 is 12.5% high, which sounds urgent, but the customer saves
fifty cents. Cutting price there gives away margin for a difference nobody
notices.

A price is only as trustworthy as the observation behind it. A single
competitor, a suspiciously low outlier, or a price below our own cost all mean
the honest answer is to look rather than to act.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.sensitivity import SENSITIVITY_HIGH, SENSITIVITY_LOW, SENSITIVITY_MEDIUM

ACTION_INCREASE = "INCREASE"
ACTION_DECREASE = "DECREASE"
ACTION_HOLD = "HOLD"
ACTION_DECREASE_REVIEW = "DECREASE_REVIEW"
ACTION_NEEDS_RESEARCH = "NEEDS_RESEARCH"
ACTION_MAP_EXCLUDED = "MAP_EXCLUDED"
ACTION_HIGH_SALES_REVIEW = "HIGH_SALES_PRICE_REVIEW"

PRICING_RULE_VERSION = "OEM-HYBRID-1.1"

# Margin bands. Above the floor a reduction can be recommended automatically;
# in the band below it a person decides; below that nothing automatic happens
# at all, because the price would not be worth having.
MARGIN_REVIEW_FLOOR = Decimal("18")

# A gap inside the dollar tolerance is normally not worth acting on, but the
# same gap on a part selling in volume adds up across every unit. Exposure is
# what makes a penny matter, so a high-volume part gets looked at rather than
# silently held.
HIGH_VOLUME_REVIEW_QTY = 2000
HIGH_VOLUME_EXPOSURE = Decimal("5000")

# Manufacturers whose pricing is managed under MAP or full retail and must not
# be repriced automatically. Competitor prices are still collected for
# reference. Configurable later; kept here so the exclusion is explicit.
DEFAULT_MANUFACTURER_MODES: dict[str, str] = {"KTM": "MAP_EXCLUDED"}

# Below these differences the customer saving is too small to be worth acting
# on, whatever the percentage says.
DOLLAR_TOLERANCE_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("10"), Decimal("1.50")),
    (Decimal("25"), Decimal("2.50")),
    (Decimal("50"), Decimal("4.00")),
    (Decimal("75"), Decimal("6.00")),
)
HIGH_PRICE_TOLERANCE_FLOOR = Decimal("5")
HIGH_PRICE_TOLERANCE_PCT = Decimal("0.03")

# How far above the lowest competitor a part may sit before it is worth acting,
# by sensitivity. A price-image part stays close to the market; a part nobody
# compares can carry more.
ABOVE_MARKET_TARGET_MULTIPLIER = {
    SENSITIVITY_HIGH: Decimal("1.02"),
    SENSITIVITY_MEDIUM: Decimal("1.03"),
    SENSITIVITY_LOW: Decimal("1.05"),
}
# How far below the market a part must be before raising is worth the risk.
UNDER_MARKET_TRIGGER = {
    SENSITIVITY_HIGH: Decimal("0.05"),
    SENSITIVITY_MEDIUM: Decimal("0.06"),
    SENSITIVITY_LOW: Decimal("0.08"),
}
# How close to the lowest competitor to price when we are currently under it.
# The target rises as economic importance falls, because low sensitivity does
# not mean surrendering every competitive advantage: a part that still sells in
# reasonable numbers is worth keeping visibly cheaper, even if it is not a
# price-image item. Only a part with genuinely minimal exposure goes all the way
# to matching the market.
UNDER_MARKET_TARGET = {
    SENSITIVITY_HIGH: Decimal("0.98"),
    SENSITIVITY_MEDIUM: Decimal("0.99"),
    SENSITIVITY_LOW: Decimal("0.995"),
}
# A low-sensitivity part below these has little enough at stake that matching
# the lowest competitor exactly is the sensible starting point.
LOW_995_QTY = 50
LOW_995_ANNUAL_SALES = Decimal("5000")
LOW_995_EXPOSURE = Decimal("500")
LOW_MINIMAL_TARGET = Decimal("1.00")

# A high-ticket part is comparison shopped whatever its volume, and half a
# percent of a large price is real money to a customer while costing us little:
# on a $1,500 item, 99.5% gives up $7.50 to stay visibly below the market.
LOW_995_HIGH_TICKET = Decimal("500")

# Every recommendation names the rule that produced it, so a price can be
# audited without re-deriving it.
RULE_HIGH_98 = "HIGH_98"
RULE_MEDIUM_99 = "MEDIUM_99"
RULE_LOW_MEANINGFUL_995 = "LOW_MEANINGFUL_995"
RULE_LOW_MINIMAL_100 = "LOW_MINIMAL_100"
RULE_ABOVE_MARKET_HIGH = "ABOVE_MARKET_HIGH_102"
RULE_ABOVE_MARKET_MEDIUM = "ABOVE_MARKET_MEDIUM_103"
RULE_ABOVE_MARKET_LOW = "ABOVE_MARKET_LOW_105"
RULE_LOW_DOLLAR_TOLERANCE_HOLD = "LOW_DOLLAR_TOLERANCE_HOLD"
RULE_HIGH_SALES_REVIEW = "HIGH_SALES_PRICE_REVIEW"
RULE_MARGIN_FLOOR_REVIEW = "MARGIN_FLOOR_REVIEW"
RULE_MARGIN_HARD_FLOOR = "MARGIN_HARD_FLOOR_HOLD"
RULE_COMPETITOR_DATA_REVIEW = "COMPETITOR_DATA_REVIEW"
RULE_HOLD_WITHIN_BAND = "HOLD_WITHIN_BAND"
RULE_MAP_EXCLUDED = "MAP_EXCLUDED"

ABOVE_MARKET_RULES = {
    SENSITIVITY_HIGH: RULE_ABOVE_MARKET_HIGH,
    SENSITIVITY_MEDIUM: RULE_ABOVE_MARKET_MEDIUM,
    SENSITIVITY_LOW: RULE_ABOVE_MARKET_LOW,
}
UNDER_MARKET_RULES = {SENSITIVITY_HIGH: RULE_HIGH_98, SENSITIVITY_MEDIUM: RULE_MEDIUM_99}

# A lowest quote this far under the rest of the market is more likely to be a
# different item, a multipack, or a bad reading than a real price.
OUTLIER_MEDIAN_RATIO = Decimal("0.72")
OUTLIER_SPREAD_RATIO = Decimal("1.5")


@dataclass(frozen=True)
class CompetitorQuote:
    name: str
    price: Decimal | None
    in_stock: bool = True
    stale: bool = False


@dataclass(frozen=True)
class MarketView:
    valid: list[CompetitorQuote] = field(default_factory=list)
    rejected: list[tuple[CompetitorQuote, str]] = field(default_factory=list)
    lowest: Decimal | None = None
    median: Decimal | None = None
    highest: Decimal | None = None
    confidence: str = "LOW"
    outlier_suspected: bool = False

    @property
    def valid_count(self) -> int:
        return len(self.valid)


@dataclass(frozen=True)
class Recommendation:
    action: str
    recommended_price: Decimal | None
    reason: str
    market: MarketView
    projected_margin_pct: Decimal | None = None
    rule_version: str = PRICING_RULE_VERSION
    factors: list[str] = field(default_factory=list)
    # Which rung of the under-market ladder produced this, so a price can be
    # audited without re-deriving it. Blank when a different rule applied.
    target_percent_of_lowest: Decimal | None = None
    rule_applied: str = ""
    # Why this tier was reached, so a 99.5% recommendation can be explained
    # without working back through the thresholds.
    target_tier_qualification: str = ""
    # What the competitive logic wanted before the margin floor moved it, so the
    # effect of the floor is visible rather than hidden in the final number.
    competitive_target_price: Decimal | None = None


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def dollar_tolerance(current_price: Decimal) -> Decimal:
    """The smallest difference worth acting on at this price level."""
    for ceiling, tolerance in DOLLAR_TOLERANCE_BANDS:
        if current_price < ceiling:
            return tolerance
    return max(HIGH_PRICE_TOLERANCE_FLOOR, _money(current_price * HIGH_PRICE_TOLERANCE_PCT))


def assess_market(quotes: list[CompetitorQuote], *, our_cost: Decimal | None = None) -> MarketView:
    """Decide which competitor prices can be trusted, and describe the market.

    Rejecting a quote is not the same as it being absent: a price below our own
    cost usually means a different item or a bad reading, and repricing against
    it would be a real loss.
    """
    valid: list[CompetitorQuote] = []
    rejected: list[tuple[CompetitorQuote, str]] = []

    for quote in quotes:
        if quote.price is None:
            rejected.append((quote, "no price found"))
        elif quote.price <= 0:
            rejected.append((quote, "price of zero or less is not a real price"))
        elif not quote.in_stock:
            rejected.append((quote, "out of stock, so not a price we compete with"))
        elif quote.stale:
            rejected.append((quote, "observation is stale"))
        elif our_cost is not None and quote.price <= our_cost:
            rejected.append((quote, f"price ${quote.price} is at or below our cost ${our_cost}"))
        else:
            valid.append(quote)

    if not valid:
        return MarketView(valid=[], rejected=rejected, confidence="NONE")

    prices = sorted(quote.price for quote in valid)  # type: ignore[misc]
    lowest, highest = prices[0], prices[-1]
    median = _money(Decimal(statistics.median(prices)))

    outlier = False
    if len(prices) >= 3 and median > 0 and lowest > 0:
        if lowest < median * OUTLIER_MEDIAN_RATIO and (highest / lowest) >= OUTLIER_SPREAD_RATIO:
            outlier = True

    if len(valid) >= 3 and not outlier:
        confidence = "HIGH"
    elif len(valid) == 2 or (len(valid) >= 3 and outlier):
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return MarketView(
        valid=valid,
        rejected=rejected,
        lowest=lowest,
        median=median,
        highest=highest,
        confidence=confidence,
        outlier_suspected=outlier,
    )


def _margin_pct(price: Decimal, cost: Decimal | None) -> Decimal | None:
    if cost is None or price <= 0:
        return None
    return ((price - cost) / price * Decimal("100")).quantize(Decimal("0.01"))


def _price_for_margin(cost: Decimal, minimum_margin_pct: Decimal) -> Decimal:
    """Cheapest price that still clears the margin floor."""
    divisor = Decimal("1") - (minimum_margin_pct / Decimal("100"))
    if divisor <= 0:
        return cost
    return _money(cost / divisor)


def _under_market_rationale(sensitivity: str, rule_applied: str) -> str:
    """Why this particular target was chosen, in plain terms."""
    if sensitivity == SENSITIVITY_HIGH:
        return (
            "This part is heavily shopped, so the price recovers margin while staying visibly "
            "below the market."
        )
    if sensitivity == SENSITIVITY_MEDIUM:
        return "This part is moderately shopped, so the price stays just under the market."
    if rule_applied == RULE_LOW_MEANINGFUL_995:
        return (
            "Customers rarely compare this part, but it sells enough that a small advantage is "
            "worth keeping rather than matching the market exactly."
        )
    return (
        "Customers rarely compare this part and it sells little, so there is nothing to gain from "
        "pricing under the market."
    )


def recommend(
    *,
    current_price: Decimal,
    cost: Decimal | None,
    sensitivity: str,
    quotes: list[CompetitorQuote],
    manufacturer: str = "",
    minimum_margin_pct: Decimal = Decimal("20"),
    manufacturer_modes: dict[str, str] | None = None,
    qty_sold_12m: int | None = None,
    annual_sales: Decimal | None = None,
) -> Recommendation:
    modes = DEFAULT_MANUFACTURER_MODES if manufacturer_modes is None else manufacturer_modes
    if modes.get(manufacturer.strip().upper()) == "MAP_EXCLUDED":
        return Recommendation(
            action=ACTION_MAP_EXCLUDED,
            recommended_price=current_price,
            reason=f"{manufacturer} pricing is managed under MAP or full retail, so no change is recommended.",
            market=assess_market(quotes, our_cost=cost),
            projected_margin_pct=_margin_pct(current_price, cost),
            rule_applied=RULE_MAP_EXCLUDED,
            target_tier_qualification="Excluded from automatic pricing",
        )

    market = assess_market(quotes, our_cost=cost)
    factors: list[str] = []

    if market.lowest is None:
        return Recommendation(
            action=ACTION_NEEDS_RESEARCH,
            recommended_price=current_price,
            reason="No competitor price could be trusted for this part, so there is nothing to price against.",
            market=market,
            projected_margin_pct=_margin_pct(current_price, cost),
            factors=[reason for _, reason in market.rejected],
            rule_applied=RULE_COMPETITOR_DATA_REVIEW,
            target_tier_qualification="No validated competitor",
        )

    if market.outlier_suspected:
        return Recommendation(
            action=ACTION_NEEDS_RESEARCH,
            recommended_price=current_price,
            reason=(
                f"The lowest quote of ${market.lowest} sits well under the rest of the market "
                f"(median ${market.median}), which usually means a different item or a bad reading "
                f"rather than a real price. Worth checking before repricing."
            ),
            market=market,
            projected_margin_pct=_margin_pct(current_price, cost),
            rule_applied=RULE_COMPETITOR_DATA_REVIEW,
            target_tier_qualification="Lowest quote not corroborated",
        )

    lowest = market.lowest
    tolerance = dollar_tolerance(current_price)
    gap = current_price - lowest
    factors.append(f"lowest trusted competitor ${lowest} from {market.valid_count} source(s)")

    if current_price < lowest:
        # Priced under the market. Worth raising only if the gap is wide enough
        # to be worth the risk of moving.
        shortfall = (lowest - current_price) / lowest
        trigger = UNDER_MARKET_TRIGGER[sensitivity]
        if shortfall < trigger:
            return Recommendation(
                action=ACTION_HOLD,
                recommended_price=current_price,
                reason=(
                    f"Hold at ${current_price}. We are {shortfall * 100:.1f}% under the lowest competitor, "
                    f"which is not far enough below market to be worth moving."
                ),
                market=market,
                projected_margin_pct=_margin_pct(current_price, cost),
                factors=factors,
            rule_applied=RULE_HOLD_WITHIN_BAND,
            target_tier_qualification="under-market trigger not met",
        )
        # Split low sensitivity by what is actually at stake. A part still
        # selling in reasonable numbers is worth keeping visibly cheaper; only
        # one with minimal exposure should match the market exactly.
        target_percent = UNDER_MARKET_TARGET[sensitivity]
        rule_applied = UNDER_MARKET_RULES.get(sensitivity, "")
        qualification = f"{sensitivity} Sensitivity"

        if sensitivity == SENSITIVITY_LOW:
            exposure = abs(current_price - lowest) * Decimal(qty_sold_12m or 0)
            # Any one of these is enough. Volume, revenue and exposure each say
            # something is at stake; price says the customer will shop it
            # regardless of how rarely it sells.
            reasons: list[str] = []
            if qty_sold_12m is not None and qty_sold_12m >= LOW_995_QTY:
                reasons.append(f"Qty >= {LOW_995_QTY}")
            if annual_sales is not None and annual_sales >= LOW_995_ANNUAL_SALES:
                reasons.append(f"Annual Sales >= ${LOW_995_ANNUAL_SALES:,.0f}")
            if exposure >= LOW_995_EXPOSURE:
                reasons.append(f"Competitive Exposure >= ${LOW_995_EXPOSURE:,.0f}")
            if current_price >= LOW_995_HIGH_TICKET:
                reasons.append(f"High Ticket >= ${LOW_995_HIGH_TICKET:,.0f}")

            if reasons:
                rule_applied = RULE_LOW_MEANINGFUL_995
                qualification = "LOW - " + ("Multiple Qualifiers: " if len(reasons) > 1 else "") + "; ".join(reasons)
                factors.append("low sensitivity, but there is enough at stake to keep a small advantage")
            elif market.confidence == "LOW":
                # Matching a figure that has no corroboration is too far to go.
                rule_applied = RULE_LOW_MEANINGFUL_995
                qualification = "LOW - Minimal Exposure, held under market on weak competitor data"
                factors.append("competitor data is weak, so holding just under the market rather than matching it")
            else:
                target_percent = LOW_MINIMAL_TARGET
                rule_applied = RULE_LOW_MINIMAL_100
                qualification = "LOW - Minimal Exposure"
                factors.append("low sensitivity with minimal sales exposure and not high ticket")

        # The percentage is applied to the price and only then rounded, so the
        # result is not distorted by rounding the multiplier first.
        target = _money(lowest * target_percent)
        if rule_applied == RULE_LOW_MINIMAL_100 and market.median is not None:
            # Moving toward the median needs data worth trusting.
            if market.confidence in {"MEDIUM", "HIGH"}:
                target = min(max(target, lowest), market.median)

        percent_label = f"{target_percent * 100:.1f}".rstrip("0").rstrip(".")
        return Recommendation(
            action=ACTION_INCREASE,
            recommended_price=target,
            reason=(
                f"Increase from ${current_price} to ${target}. We are {shortfall * 100:.1f}% below the "
                f"lowest validated competitor at ${lowest}. "
                + _under_market_rationale(sensitivity, rule_applied)
                + f" The target is about {percent_label}% of the lowest competitor."
            ),
            market=market,
            projected_margin_pct=_margin_pct(target, cost),
            factors=factors,
            target_percent_of_lowest=target_percent * Decimal("100"),
            rule_applied=rule_applied,
            target_tier_qualification=qualification,
            competitive_target_price=target,
        )

    # Priced at or above the market.
    if gap <= tolerance:
        exposure = gap * Decimal(qty_sold_12m) if qty_sold_12m else Decimal("0")
        if qty_sold_12m and (qty_sold_12m >= HIGH_VOLUME_REVIEW_QTY or exposure >= HIGH_VOLUME_EXPOSURE):
            # $1.18 on one part is nothing; on 7,000 parts it is real money and
            # every one of those customers saw the difference.
            return Recommendation(
                action=ACTION_HIGH_SALES_REVIEW,
                recommended_price=current_price,
                reason=(
                    f"Worth reviewing. We are ${gap} above the lowest competitor, which is normally "
                    f"too small to act on, but this sold {qty_sold_12m:,} units, so that gap is about "
                    f"${exposure:,.0f} a year and every one of those customers saw the difference."
                ),
                market=market,
                projected_margin_pct=_margin_pct(current_price, cost),
                factors=factors + [f"annual exposure ${exposure:,.0f}"],
            rule_applied=RULE_HIGH_SALES_REVIEW,
            target_tier_qualification="small gap but high annual exposure",
        )
        return Recommendation(
            action=ACTION_HOLD,
            recommended_price=current_price,
            reason=(
                f"Hold at ${current_price}. We are ${gap} above the lowest competitor, which is within "
                f"the ${tolerance} that matters at this price. The percentage looks larger than the "
                f"difference a customer would actually notice."
            ),
            market=market,
            projected_margin_pct=_margin_pct(current_price, cost),
            factors=factors,
            rule_applied=RULE_LOW_DOLLAR_TOLERANCE_HOLD,
            target_tier_qualification="gap within dollar tolerance",
        )

    gap_pct = (gap / lowest * Decimal("100")) if lowest > 0 else Decimal("0")
    if sensitivity == SENSITIVITY_HIGH and gap_pct <= Decimal("2"):
        # Tiny percentage differences are not worth chasing even on a
        # price-image part: the customer does not notice and the margin does.
        return Recommendation(
            action=ACTION_HOLD,
            recommended_price=current_price,
            reason=(
                f"Hold at ${current_price}. We are only {gap_pct:.1f}% above the lowest competitor, "
                f"which is too small a difference to be worth changing."
            ),
            market=market,
            projected_margin_pct=_margin_pct(current_price, cost),
            factors=factors,
            rule_applied=RULE_HOLD_WITHIN_BAND,
            target_tier_qualification="percentage gap under 2%",
        )

    target = _money(lowest * ABOVE_MARKET_TARGET_MULTIPLIER[sensitivity])
    if sensitivity != SENSITIVITY_HIGH and market.median is not None and market.median > target:
        # The median only lifts the target, never lowers it. Capping at the
        # median instead pulled every sensitivity down to the same number
        # whenever the market was tightly grouped, which defeats the point of
        # scoring sensitivity at all: a part nobody compares should be allowed
        # to hold more margin than a price-image one.
        target = market.median
        factors.append(f"the wider market at ${market.median} supports more than the lowest quote alone")
    if target >= current_price:
        return Recommendation(
            action=ACTION_HOLD,
            recommended_price=current_price,
            reason=f"Hold at ${current_price}. The market does not support a lower price for this part.",
            market=market,
            projected_margin_pct=_margin_pct(current_price, cost),
            factors=factors,
            rule_applied=RULE_HOLD_WITHIN_BAND,
            target_tier_qualification="market above current price",
        )

    if cost is not None:
        target_margin = _margin_pct(target, cost)
        floor_price = _price_for_margin(cost, minimum_margin_pct)
        if target_margin is not None and target_margin < MARGIN_REVIEW_FLOOR:
            # Below this the price is not worth having, so nothing automatic
            # happens and someone decides whether the sale is worth the margin.
            return Recommendation(
                action=ACTION_HOLD,
                recommended_price=current_price,
                reason=(
                    f"Hold at ${current_price}. Matching the market would need ${target}, leaving only "
                    f"{target_margin}% margin, below the {MARGIN_REVIEW_FLOOR}% point where an automatic "
                    f"reduction is worth making. This needs approval rather than a price change."
                ),
                market=market,
                projected_margin_pct=_margin_pct(current_price, cost),
                factors=factors,
            rule_applied=RULE_MARGIN_HARD_FLOOR,
            target_tier_qualification="below the hard margin floor",
        )
        if target < floor_price:
            return Recommendation(
                action=ACTION_DECREASE_REVIEW,
                recommended_price=floor_price,
                reason=(
                    f"Matching the market would need ${target}, which falls below the {minimum_margin_pct}% "
                    f"margin floor. ${floor_price} is the lowest price that still clears it, so this needs "
                    f"a decision rather than an automatic cut."
                ),
                market=market,
                projected_margin_pct=_margin_pct(floor_price, cost),
                factors=factors,
            rule_applied=RULE_MARGIN_FLOOR_REVIEW,
            target_tier_qualification="competitive target below margin floor",
        )

    return Recommendation(
        action=ACTION_DECREASE,
        recommended_price=target,
        reason=(
            f"Decrease from ${current_price} to ${target}. We are ${gap} above the lowest competitor "
            f"at ${lowest}, which is more than the ${tolerance} that matters at this price."
        ),
        market=market,
        projected_margin_pct=_margin_pct(target, cost),
        factors=factors,
            rule_applied=ABOVE_MARKET_RULES[sensitivity],
            target_tier_qualification="above market beyond tolerance",
        )
