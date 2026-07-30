from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.comparison import ComparisonFilters, comparison_rows
from app.database import connect_database, utc_now
from app.pricing_rules import (
    list_manufacturer_rule_overrides,
    list_pricing_rules,
    rules_for_manufacturer,
    suggest_price,
)

PENDING_REVIEW = "Pending Review"
ALL_STATUSES = "All"
ALL_BUCKETS = "All"
REVIEW_STATUSES = (
    PENDING_REVIEW,
    "Approved",
    "Needs Price Change",
    "Needs Investigation",
    "Hidden Price Review",
    "Ignored",
)
REVIEW_BUCKETS = (
    ALL_BUCKETS,
    "Our Price Higher",
    "Our Price Lower",
    "Hidden Price",
    "Missing Competitor Price",
    "Missing Cost",
    "Price Match",
    "Needs Data",
)


@dataclass(frozen=True)
class ReviewQueue:
    rows: list[dict[str, Any]]
    rule_columns: list[dict[str, str]]
    summary: dict[str, Any]
    counts: dict[str, int]
    bucket_counts: dict[str, int]
    status: str
    bucket: str
    page: int
    page_size: int
    total: int
    total_pages: int


def review_queue(
    database: Path,
    *,
    status: str = PENDING_REVIEW,
    bucket: str = ALL_BUCKETS,
    page: int = 1,
    page_size: int = 50,
) -> ReviewQueue:
    rows = review_rows(database, status=status, bucket=bucket)
    rule_columns = [{"rule_code": rule.rule_code, "rule_name": rule.rule_name} for rule in list_pricing_rules(database, enabled_only=True)]
    all_rows = _annotated_rows(database)
    selected_status = _selected_status(status)
    status_rows = _filter_status(all_rows, selected_status)
    counts = _status_counts(all_rows)
    bucket_counts = _bucket_counts(status_rows)
    selected_bucket = _selected_bucket(bucket)
    page_size = page_size if page_size in {25, 50, 100} else 50
    total = len(rows)
    total_pages = max(1, math.ceil(total / page_size))
    page = min(max(1, page), total_pages)
    offset = (page - 1) * page_size
    return ReviewQueue(
        rows=rows[offset : offset + page_size],
        rule_columns=rule_columns,
        summary=_review_summary(rows),
        counts=counts,
        bucket_counts=bucket_counts,
        status=selected_status,
        bucket=selected_bucket,
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
    )


def review_rows(database: Path, *, status: str = PENDING_REVIEW, bucket: str = ALL_BUCKETS) -> list[dict[str, Any]]:
    selected_status = _selected_status(status)
    selected_bucket = _selected_bucket(bucket)
    rows = _filter_status(_annotated_rows(database), selected_status)
    if selected_bucket != ALL_BUCKETS:
        rows = [row for row in rows if row["review_bucket"] == selected_bucket]
    return rows


def comparison_review_rows(database: Path, filters: ComparisonFilters = ComparisonFilters()) -> list[dict[str, Any]]:
    return _annotated_rows(database, filters)


def save_review_decision(
    database: Path,
    *,
    product_id: int,
    review_status: str,
    suggested_new_price: str = "",
    notes: str = "",
    reviewer: str = "",
    applied_rule_codes: list[str] | None = None,
) -> None:
    if review_status not in REVIEW_STATUSES:
        raise ValueError("Choose a valid review status.")
    suggested_new_price_cents = _money_to_cents(suggested_new_price)
    applied_rule_codes_json = json.dumps(applied_rule_codes or [])
    now = utc_now()
    reviewed_at = None if review_status == PENDING_REVIEW else now
    with connect_database(database) as conn:
        product = conn.execute("""
            SELECT p.product_id, ips.our_current_price_cents, prd.original_price_cents
            FROM products p
            LEFT JOIN internal_product_state ips ON ips.product_id=p.product_id
            LEFT JOIN pricing_review_decisions prd ON prd.product_id=p.product_id
            WHERE p.product_id=?
        """, (product_id,)).fetchone()
        if product is None:
            raise ValueError("Product not found.")
        original_price_cents = product["original_price_cents"] if product["original_price_cents"] is not None else product["our_current_price_cents"]
        conn.execute(
            """
            INSERT INTO pricing_review_decisions(product_id, review_status, original_price_cents, suggested_new_price_cents,
                applied_rule_codes_json, notes, reviewer, reviewed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                review_status=excluded.review_status,
                original_price_cents=COALESCE(pricing_review_decisions.original_price_cents, excluded.original_price_cents),
                suggested_new_price_cents=excluded.suggested_new_price_cents,
                applied_rule_codes_json=excluded.applied_rule_codes_json,
                notes=excluded.notes,
                reviewer=excluded.reviewer,
                reviewed_at=excluded.reviewed_at,
                updated_at=excluded.updated_at
            """,
            (product_id, review_status, original_price_cents, suggested_new_price_cents, applied_rule_codes_json, notes.strip(), reviewer.strip(), reviewed_at, now, now),
        )
        if suggested_new_price_cents is not None:
            conn.execute(
                """
                UPDATE internal_product_state
                SET our_current_price_cents=?, updated_at=?
                WHERE product_id=?
                """,
                (suggested_new_price_cents, now, product_id),
            )


def undo_saved_review_decision(database: Path, *, product_id: int) -> None:
    """Restore the catalog price captured before a comparison save."""
    now = utc_now()
    with connect_database(database) as conn:
        decision = conn.execute(
            "SELECT original_price_cents FROM pricing_review_decisions WHERE product_id=?",
            (product_id,),
        ).fetchone()
        if decision is None or decision["original_price_cents"] is None:
            raise ValueError("There is no saved price change to undo for this product.")
        conn.execute(
            "UPDATE internal_product_state SET our_current_price_cents=?, updated_at=? WHERE product_id=?",
            (decision["original_price_cents"], now, product_id),
        )
        conn.execute("DELETE FROM pricing_review_decisions WHERE product_id=?", (product_id,))


def save_bulk_review_decision(
    database: Path,
    *,
    product_ids: list[int],
    review_status: str,
    notes: str = "",
) -> int:
    saved = 0
    for product_id in product_ids:
        save_review_decision(
            database,
            product_id=product_id,
            review_status=review_status,
            notes=notes,
        )
        saved += 1
    return saved


def pending_review_count(database: Path) -> int:
    return review_queue(database, status=PENDING_REVIEW, page_size=25).counts.get(PENDING_REVIEW, 0)


def _annotated_rows(database: Path, filters: ComparisonFilters = ComparisonFilters()) -> list[dict[str, Any]]:
    rows = comparison_rows(database, filters)
    rules = list_pricing_rules(database, enabled_only=True)
    overrides = list_manufacturer_rule_overrides(database)
    for row in rows:
        bucket, reason = _classify_row(row)
        row["review_bucket"] = bucket
        row["review_reason"] = reason
        saved_codes = _saved_rule_codes(row.get("applied_rule_codes_json"))
        effective_rules = rules_for_manufacturer(rules, overrides, row.get("manufacturer", ""))
        row["manufacturer_rule_override"] = next(
            (override for override in overrides if override.manufacturer == row.get("manufacturer") and override.is_enabled),
            None,
        )
        suggestion = suggest_price(row, effective_rules, selected_rule_codes=saved_codes if saved_codes else None)
        row["rule_suggestion"] = suggestion
        row["rule_flags"] = {item["rule_code"]: item for item in suggestion["applied_rules"]}
        if not row.get("suggested_new_price"):
            row["suggested_new_price"] = suggestion["suggested_price"]
        row["our_current_price_display"] = _format_display_money(row.get("our_current_price"))
        row["current_margin_pct"] = row.get("our_gross_margin_pct") or ""
        row["price_difference_class"] = _price_difference_class(row)
        row["updated_margin_pct"] = _updated_margin_pct(row)
        row["estimated_annual_impact"] = _estimated_annual_impact(row)
        row["recommended_action"] = _recommended_action(row)
    return rows


def suggested_price_for_product(database: Path, product_id: int, selected_rule_codes: list[str]) -> dict[str, Any]:
    rules = list_pricing_rules(database, enabled_only=True)
    overrides = list_manufacturer_rule_overrides(database)
    row = next((item for item in comparison_rows(database) if item["product_id"] == product_id), None)
    if row is None:
        raise ValueError("Product not found.")
    return suggest_price(row, rules_for_manufacturer(rules, overrides, row.get("manufacturer", "")), selected_rule_codes=set(selected_rule_codes))


def _selected_status(status: str) -> str:
    return status if status in REVIEW_STATUSES or status == ALL_STATUSES else PENDING_REVIEW


def _selected_bucket(bucket: str) -> str:
    return bucket if bucket in REVIEW_BUCKETS else ALL_BUCKETS


def _filter_status(rows: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    if status == ALL_STATUSES:
        return rows
    return [row for row in rows if row["review_status"] == status]


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in REVIEW_STATUSES}
    counts[ALL_STATUSES] = len(rows)
    for row in rows:
        counts[row["review_status"]] = counts.get(row["review_status"], 0) + 1
    return counts


def _bucket_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {bucket: 0 for bucket in REVIEW_BUCKETS}
    counts[ALL_BUCKETS] = len(rows)
    for row in rows:
        counts[row["review_bucket"]] = counts.get(row["review_bucket"], 0) + 1
    return counts


def _classify_row(row: dict[str, Any]) -> tuple[str, str]:
    if row.get("motosport_hidden_price"):
        return "Hidden Price", "MotoSport shows a cart-hidden price that needs review before acting."
    if not row.get("lowest_competitor_name"):
        return "Missing Competitor Price", "No supported competitor price has been found yet."
    if not row.get("current_cost"):
        return "Missing Cost", "Current cost is missing, so margin cannot be checked."
    if row.get("price_difference_cents") is None:
        return "Needs Data", "The app does not have enough price data to calculate a gap."
    if row["price_difference_cents"] > 0:
        return "Our Price Higher", "Our price is above the lowest competitor price found."
    if row["price_difference_cents"] < 0:
        return "Our Price Lower", "Our price is below the lowest competitor price found."
    return "Price Match", "Our price matches the lowest competitor price found."


def _recommended_action(row: dict[str, Any]) -> str:
    bucket = row.get("review_bucket")
    if bucket == "Hidden Price":
        return "Review hidden cart price"
    if bucket == "Missing Competitor Price":
        return "Check competitor coverage"
    if bucket == "Missing Cost":
        return "Add cost before approval"
    if bucket == "Our Price Higher":
        return "Review suggested decrease"
    if bucket == "Our Price Lower":
        return "Review margin opportunity"
    if bucket == "Price Match":
        return "No price change needed"
    return "Review data"


def _review_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    approved_with_price = [row for row in rows if row.get("review_status") == "Approved" and row.get("suggested_new_price")]
    increases = [row for row in rows if (_price_delta(row) or Decimal("0")) > 0]
    decreases = [row for row in rows if (_price_delta(row) or Decimal("0")) < 0]
    impact = sum(((_price_delta(row) or Decimal("0")) * _quantity(row.get("units_sold_12m")) for row in approved_with_price), Decimal("0"))
    return {
        "rows": len(rows),
        "ready_for_export": len(approved_with_price),
        "suggested_increases": len(increases),
        "suggested_decreases": len(decreases),
        "estimated_annual_impact": _format_money(impact),
    }


def _updated_margin_pct(row: dict[str, Any]) -> str:
    updated = _decimal_money(row.get("suggested_new_price"))
    cost = _decimal_money(row.get("current_cost"))
    if updated in (None, Decimal("0")) or cost is None:
        return ""
    return format(((updated - cost) / updated * Decimal("100")).quantize(Decimal("0.01")), "f")


def _estimated_annual_impact(row: dict[str, Any]) -> str:
    delta = _price_delta(row)
    if delta is None:
        return ""
    return _format_money(delta * _quantity(row.get("units_sold_12M") or row.get("units_sold_12m")))


def _price_delta(row: dict[str, Any]) -> Decimal | None:
    updated = _decimal_money(row.get("suggested_new_price"))
    current = _decimal_money(row.get("our_current_price"))
    if updated is None or current is None:
        return None
    return updated - current


def _decimal_money(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace("$", "").replace(",", ""))
    except InvalidOperation:
        return None


def _quantity(value: object) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


def _format_money(value: Decimal) -> str:
    value = Decimal(value)
    return format(value.quantize(Decimal("0.01")), "f")


def _format_display_money(value: object) -> str:
    money = _decimal_money(value)
    return "" if money is None else _format_money(money)


def _price_difference_class(row: dict[str, Any]) -> str:
    cents = row.get("price_difference_cents")
    if cents is None:
        return ""
    if cents > 0:
        return "price-difference-higher"
    if cents < 0:
        return "price-difference-lower"
    return ""


def _saved_rule_codes(value: str | None) -> set[str]:
    if not value:
        return set()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(item) for item in parsed}


def _money_to_cents(value: str) -> int | None:
    cleaned = value.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Suggested price must be a valid dollar amount.") from exc
    if amount < 0:
        raise ValueError("Suggested price cannot be negative.")
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
