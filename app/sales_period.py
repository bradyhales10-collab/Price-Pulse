"""Put sales quantities on a common footing.

Sensitivity scoring compares a part's demand against fixed thresholds, so the
period behind the number matters. The import field is named units_sold_12m and
matches a column called simply "Qty Sold", with nothing checking that it really
covers twelve months. Six months of sales scored against annual thresholds
understates demand by half, and a part that should be treated as heavily
shopped gets priced as though nobody buys it.

So the period is stated rather than assumed, and quantities are scaled to a
year before scoring. The original figure is kept as uploaded; only the scored
value is adjusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Periods someone might reasonably have exported, with how many months each
# covers. The keys are what gets stored with an import.
SALES_PERIODS: dict[str, int] = {
    "1_month": 1,
    "2_months": 2,
    "3_months": 3,
    "4_months": 4,
    "6_months": 6,
    "9_months": 9,
    "12_months": 12,
    "18_months": 18,
    "24_months": 24,
    "36_months": 36,
}
DEFAULT_SALES_PERIOD = "12_months"

PERIOD_LABELS: dict[str, str] = {
    "1_month": "1 month",
    "2_months": "2 months",
    "3_months": "3 months (a quarter)",
    "4_months": "4 months",
    "6_months": "6 months (half a year)",
    "9_months": "9 months",
    "12_months": "12 months (a full year)",
    "18_months": "18 months",
    "24_months": "24 months (two years)",
    "36_months": "36 months (three years)",
}

# Column headings that state their own period, so an obvious case does not have
# to be set by hand. Checked longest-first so "12 month" beats "1 month".
# Longest and most specific first, so "12 month" is not read as "2 month".
HEADER_PERIOD_HINTS: tuple[tuple[str, str | None], ...] = (
    ("36 month", "36_months"),
    ("3 year", "36_months"),
    ("24 month", "24_months"),
    ("2 year", "24_months"),
    ("18 month", "18_months"),
    ("12 month", "12_months"),
    ("12m", "12_months"),
    ("1 year", "12_months"),
    ("annual", "12_months"),
    ("ytd", None),  # deliberately ambiguous: depends when the file was made
    ("9 month", "9_months"),
    ("6 month", "6_months"),
    ("6m", "6_months"),
    ("4 month", "4_months"),
    ("3 month", "3_months"),
    ("quarter", "3_months"),
    ("2 month", "2_months"),
    ("1 month", "1_month"),
    ("monthly", "1_month"),
)


@dataclass(frozen=True)
class SalesPeriodResult:
    period: str
    annualized_qty: int | None
    was_scaled: bool
    note: str

    @property
    def label(self) -> str:
        return PERIOD_LABELS.get(self.period, self.period)


def detect_period_from_header(header: str | None) -> str | None:
    """Guess the period from a column heading, when it says so plainly.

    Returns None when the heading gives no usable signal, including for
    genuinely ambiguous ones like "YTD" whose meaning depends on when the file
    was produced. A wrong guess is worse than asking.
    """
    if not header:
        return None
    lowered = str(header).strip().lower()
    for hint, period in HEADER_PERIOD_HINTS:
        if hint in lowered:
            return period
    return None


def annualize_quantity(qty: int | None, period: str = DEFAULT_SALES_PERIOD) -> SalesPeriodResult:
    """Scale a quantity to a twelve month equivalent.

    Scaling up from a short period is an estimate, not a measurement, which the
    note records so a recommendation can be read with that in mind. Anything
    longer than a year is scaled down for the same reason.
    """
    months = SALES_PERIODS.get(period, 12)
    if qty is None:
        return SalesPeriodResult(period=period, annualized_qty=None, was_scaled=False, note="no quantity supplied")
    if months == 12:
        return SalesPeriodResult(period=period, annualized_qty=qty, was_scaled=False, note="already a 12 month figure")

    factor = Decimal(12) / Decimal(months)
    scaled = int((Decimal(qty) * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    direction = "scaled up from" if months < 12 else "scaled down from"
    return SalesPeriodResult(
        period=period,
        annualized_qty=scaled,
        was_scaled=True,
        note=f"{direction} {qty:,} over {PERIOD_LABELS.get(period, period)} to {scaled:,} a year",
    )


def annualize_sales(amount: Decimal | None, period: str = DEFAULT_SALES_PERIOD) -> Decimal | None:
    if amount is None:
        return None
    months = SALES_PERIODS.get(period, 12)
    if months == 12:
        return amount
    return (amount * Decimal(12) / Decimal(months)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
