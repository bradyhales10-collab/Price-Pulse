"""Run both pricing engines over the same parts and compare them.

The new engine writes nothing, so the only way to judge it is to see what it
would have done next to what the current one does, on real parts. This does
that and summarises where they disagree.

Disagreement is not itself a fault. The engines are built on different
principles: the current one anchors to the lowest competitor and applies
guardrails, while the new one weighs how shopped a part is and whether the
difference is worth acting on. The point is to see whether the differences are
defensible, and to catch cases where the new engine is plainly wrong before it
is trusted with anything.

    .venv\\Scripts\\python.exe compare_pricing_engines.py
    .venv\\Scripts\\python.exe compare_pricing_engines.py --limit 50 --show disagreements
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.categorization import categorize_product
from app.comparison import ComparisonFilters, comparison_rows
from app.competitors.registry import list_competitors, short_display_name
from app.config import DEFAULT_DATABASE_PATH
from app.pricing_engine import CompetitorQuote, recommend
from app.pricing_rules import list_pricing_rules, suggest_price
from app.sensitivity import derive_annual_sales, score_sensitivity

ROOT = Path(__file__).resolve().parent


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


def _quotes_from_row(row: dict[str, Any]) -> list[CompetitorQuote]:
    quotes: list[CompetitorQuote] = []
    for adapter in list_competitors():
        key = adapter.competitor_key
        price = _decimal(row.get(f"{key}_selling_price"))
        availability = str(row.get(f"{key}_availability_status") or "").lower()
        quotes.append(
            CompetitorQuote(
                name=short_display_name(adapter),
                price=price,
                in_stock=availability not in {"out_of_stock", "discontinued"},
            )
        )
    return quotes


def evaluate_row(row: dict[str, Any], rules: list[Any], minimum_margin_pct: Decimal) -> dict[str, Any] | None:
    current_price = _decimal(row.get("our_current_price"))
    if current_price is None or current_price <= 0:
        return None

    cost = _decimal(row.get("current_cost"))
    qty = _int(row.get("units_sold_12m"))
    category = categorize_product(row.get("product_name"))
    sales = derive_annual_sales(None, qty, current_price)
    sensitivity = score_sensitivity(
        category=category.category,
        qty_sold_12m=qty,
        annual_sales=sales,
        current_price=current_price,
        category_is_confident=category.is_confident,
    )

    old = suggest_price(row, rules)
    old_price = _decimal(old.get("suggested_price"))

    new = recommend(
        current_price=current_price,
        cost=cost,
        sensitivity=sensitivity.sensitivity,
        quotes=_quotes_from_row(row),
        manufacturer=str(row.get("manufacturer") or ""),
        minimum_margin_pct=minimum_margin_pct,
    )

    difference = None
    if old_price is not None and new.recommended_price is not None:
        difference = new.recommended_price - old_price

    return {
        "manufacturer": row.get("manufacturer", ""),
        "oem_part_number": row.get("oem_part_number", ""),
        "product_name": row.get("product_name", ""),
        "category": category.category,
        "category_confidence": category.confidence_class,
        "qty_sold_12m": qty,
        "sensitivity": sensitivity.sensitivity,
        "sensitivity_score": sensitivity.score,
        "current_price": current_price,
        "old_price": old_price,
        "new_price": new.recommended_price,
        "new_action": new.action,
        "difference": difference,
        "competitor_confidence": new.market.confidence,
        "reason": new.reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare the current and new pricing engines.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--limit", type=int, default=0, help="Only look at this many parts.")
    parser.add_argument(
        "--show",
        choices=["summary", "disagreements", "all"],
        default="summary",
        help="summary counts only; disagreements lists parts where the engines differ.",
    )
    args = parser.parse_args()

    if not args.database.exists():
        print(f"No database at {args.database}. Upload a parts file first.")
        return 1

    rows = comparison_rows(args.database, ComparisonFilters())
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("No parts found to compare.")
        return 1

    rules = list_pricing_rules(args.database)
    margin_rule = next((rule for rule in rules if rule.rule_type == "margin_floor"), None)
    minimum_margin_pct = Decimal(str((margin_rule.settings if margin_rule else {}).get("minimum_margin_pct", 20)))

    results = [result for row in rows if (result := evaluate_row(row, rules, minimum_margin_pct))]
    if not results:
        print("No parts had a usable current price.")
        return 1

    print("=" * 78)
    print(f"  Comparing both pricing engines across {len(results)} parts")
    print(f"  Margin floor in use: {minimum_margin_pct}%")
    print("=" * 78)

    actions: dict[str, int] = {}
    sensitivities: dict[str, int] = {}
    categories: dict[str, int] = {}
    both_priced = [r for r in results if r["old_price"] is not None and r["new_price"] is not None]
    disagreements = [r for r in both_priced if r["difference"] != 0]
    new_only = [r for r in results if r["old_price"] is None and r["new_price"] is not None]
    old_only = [r for r in results if r["old_price"] is not None and r["new_price"] is None]

    for result in results:
        actions[result["new_action"]] = actions.get(result["new_action"], 0) + 1
        sensitivities[result["sensitivity"]] = sensitivities.get(result["sensitivity"], 0) + 1
        categories[result["category"]] = categories.get(result["category"], 0) + 1

    print("\nWhat the new engine recommends:")
    for action, count in sorted(actions.items(), key=lambda item: -item[1]):
        print(f"  {count:5d}  {action}")

    print("\nPrice sensitivity:")
    for name in ("HIGH", "MEDIUM", "LOW"):
        if name in sensitivities:
            print(f"  {sensitivities[name]:5d}  {name}")

    print("\nCategories found (top 8):")
    for name, count in sorted(categories.items(), key=lambda item: -item[1])[:8]:
        print(f"  {count:5d}  {name}")

    print("\nHow the two engines compare:")
    print(f"  {len(both_priced):5d}  both produced a price")
    print(f"  {len(disagreements):5d}  they disagree on the price")
    print(f"  {len(new_only):5d}  only the new engine produced a price")
    print(f"  {len(old_only):5d}  only the current engine produced a price")

    if disagreements:
        higher = [r for r in disagreements if r["difference"] > 0]
        lower = [r for r in disagreements if r["difference"] < 0]
        print(f"\n  Of the disagreements, the new engine is higher on {len(higher)} and lower on {len(lower)}.")
        print("  Higher usually means it is holding margin where the old engine matched the lowest price.")

    if args.show in {"disagreements", "all"}:
        listing = disagreements if args.show == "disagreements" else results
        print("\n" + "=" * 78)
        print(f"  {'Detail' if args.show == 'all' else 'Where they disagree'} ({len(listing)} parts)")
        print("=" * 78)
        for result in listing[:200]:
            old = f"${result['old_price']}" if result["old_price"] is not None else "-"
            new = f"${result['new_price']}" if result["new_price"] is not None else "-"
            print(
                f"\n  {result['manufacturer']} {result['oem_part_number']}  {str(result['product_name'])[:44]}"
            )
            print(
                f"    category {result['category']} ({result['category_confidence']}), "
                f"sensitivity {result['sensitivity']} ({result['sensitivity_score']}), "
                f"competitor confidence {result['competitor_confidence']}"
            )
            print(f"    now ${result['current_price']}   current engine {old}   new engine {new}   [{result['new_action']}]")
            print(f"    {result['reason']}")
        if len(listing) > 200:
            print(f"\n  ...and {len(listing) - 200} more.")

    print("")
    print("Nothing was changed. The new engine writes no prices.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
