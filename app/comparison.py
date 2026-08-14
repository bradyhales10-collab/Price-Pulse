from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.competitors.registry import list_competitors, short_display_name
from app.database import cents_to_money
from app.web.queries import connect_readonly

PENDING_REVIEW = "Pending Review"


@dataclass(frozen=True)
class ComparisonFilters:
    search: str = ""
    manufacturer: str = ""
    price_position: str = ""
    competitor_discounted: bool = False
    competitor_keys: tuple[str, ...] = ("partzilla", "motosport")
    scan_priority: str = ""
    missing_competitor_price: bool = False
    hidden_competitor_price: bool = False
    needs_review: bool = False
    review_state: str = ""
    import_batch_id: int | None = None


# Historical SQL aliases, kept so existing generated SQL stays byte-identical
# for the original three competitors. New competitors get generated aliases.
_LISTING_ALIAS = {"partzilla": "pl", "motosport": "ml", "chaparral": "cl"}
_STATE_ALIAS = {"partzilla": "ps", "motosport": "ms", "chaparral": "cs"}
_EVENT_ALIAS = {"motosport": "mse", "chaparral": "cse"}
_SAFE_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


def _sql_aliases(key: str) -> tuple[str, str, str]:
    if not _SAFE_KEY.match(key):
        raise ValueError(f"Competitor key is not safe to use in SQL: {key!r}")
    return (
        _LISTING_ALIAS.get(key, f"{key}_l"),
        _STATE_ALIAS.get(key, f"{key}_s"),
        _EVENT_ALIAS.get(key, f"{key}_e"),
    )


def competitor_state_aliases() -> dict[str, str]:
    return {adapter.competitor_key: _sql_aliases(adapter.competitor_key)[1] for adapter in list_competitors()}


def competitor_sql_parts() -> tuple[str, str, str]:
    """Build the per-competitor SELECT columns and JOINs from the registry.

    Registering a competitor adapter is enough for its prices to flow through
    this query; no SQL editing is needed to add one.
    """
    columns: list[str] = []
    joins: list[str] = []
    name_fallbacks: list[str] = []

    for index, adapter in enumerate(list_competitors()):
        key = adapter.competitor_key
        listing, state, event = _sql_aliases(key)
        name_fallbacks.append(f"{state}.product_name")
        joins.append(
            f"LEFT JOIN competitor_listings {listing} ON {listing}.product_id=p.product_id "
            f"AND {listing}.competitor_id=(SELECT competitor_id FROM competitors WHERE competitor_code='{key}')"
        )
        joins.append(f"LEFT JOIN current_listing_state {state} ON {state}.listing_id={listing}.listing_id")
        joins.append(
            f"LEFT JOIN scan_events {event} ON {event}.scan_event_id="
            f"(SELECT MAX(evt.scan_event_id) FROM scan_events evt WHERE evt.listing_id={listing}.listing_id)"
        )

        if key == "motosport":
            # MotoSport is the one competitor that can hide its price behind the
            # cart, so it falls back to the probe results table.
            columns.append(f"""
                   COALESCE({state}.selling_price_cents, mp.selling_price_cents) motosport_selling_price_cents,
                   COALESCE({state}.reference_price_cents, mp.reference_price_cents) motosport_reference_price_cents,
                   {state}.savings_percent motosport_savings_percent,
                   {event}.page_classification motosport_page_classification,
                   {event}.price_parse_confidence motosport_parse_confidence,
                   CASE WHEN {state}.selling_price_cents IS NOT NULL THEN 'visible' ELSE mp.price_visibility END motosport_price_visibility,
                   COALESCE({state}.price_display_type, mp.price_display_type) motosport_price_display_type,
                   CASE WHEN {state}.selling_price_cents IS NOT NULL THEN 'cart_price_found' ELSE mp.result_type END motosport_result_type,
                   CASE WHEN {state}.selling_price_cents IS NOT NULL THEN 'production' WHEN mp.probe_result_id IS NOT NULL THEN 'probe' ELSE '' END motosport_source""")
        else:
            columns.append(f"""
                   {state}.selling_price_cents {key}_selling_price_cents,
                   {state}.reference_price_cents {key}_reference_price_cents,
                   {state}.savings_percent {key}_savings_percent,
                   {event}.page_classification {key}_page_classification,
                   {event}.price_parse_confidence {key}_parse_confidence,
                   {state}.price_display_type {key}_price_display_type""")

        if index == 0:
            # Unprefixed fields kept for the primary competitor, as other code reads them.
            columns.append(f"""
                   {state}.price_display_type,
                   {state}.last_successful_check_at""")

    product_name = "COALESCE(" + ", ".join([*name_fallbacks, "p.product_name"]) + ") product_name"
    return ",".join(columns), "\n            ".join(joins), product_name


def comparison_rows(database: Path, filters: ComparisonFilters = ComparisonFilters()) -> list[dict[str, Any]]:
    where, params = _where(filters)
    columns_sql, joins_sql, product_name_sql = competitor_sql_parts()
    with connect_readonly(database) as conn:
        rows = conn.execute(f"""
            SELECT p.product_id, p.internal_sku product_internal_sku, p.manufacturer, p.oem_part_number,
                   {product_name_sql},
                   ips.internal_sku, ips.our_current_price_cents, ips.current_cost_cents,
                   ips.product_category, ips.units_sold_12m, ips.inventory_qty, ips.scan_priority,
                   ips.sales_period,
                   {columns_sql},
                   prd.review_status, prd.original_price_cents, prd.suggested_new_price_cents, prd.applied_rule_codes_json,
                   prd.notes, prd.reviewer, prd.reviewed_at
            FROM products p
            JOIN internal_product_state ips ON ips.product_id=p.product_id
            {joins_sql}
            LEFT JOIN competitor_probe_results mp ON mp.probe_result_id=(
                SELECT MAX(mp2.probe_result_id)
                FROM competitor_probe_results mp2
                WHERE mp2.competitor_key='motosport'
                  AND mp2.manufacturer=p.manufacturer
                  AND UPPER(mp2.oem_part_number)=UPPER(p.oem_part_number)
            )
            LEFT JOIN pricing_review_decisions prd ON prd.product_id=p.product_id
            WHERE {where}
            ORDER BY p.manufacturer COLLATE NOCASE, p.oem_part_number COLLATE NOCASE
        """, params).fetchall()
    output = [_comparison_row(dict(row)) for row in rows]
    if filters.price_position == "above":
        output = [row for row in output if row["price_difference_cents"] is not None and row["price_difference_cents"] > 0]
    elif filters.price_position == "below":
        output = [row for row in output if row["price_difference_cents"] is not None and row["price_difference_cents"] < 0]
    return sorted(output, key=lambda row: abs(row["price_difference_cents"] or 0), reverse=True)


def _where(filters: ComparisonFilters) -> tuple[str, list[Any]]:
    clauses = ["ips.is_active=1"]
    params: list[Any] = []
    if filters.search:
        clauses.append("(p.oem_part_number LIKE ? OR ips.internal_sku LIKE ? OR COALESCE(ps.product_name, p.product_name, '') LIKE ?)")
        like = f"%{filters.search}%"
        params.extend([like, like, like])
    if filters.manufacturer:
        clauses.append("p.manufacturer=?")
        params.append(filters.manufacturer)
    if filters.competitor_discounted:
        clauses.append("ps.price_display_type='discounted'")
    if filters.scan_priority:
        clauses.append("ips.scan_priority=?")
        params.append(filters.scan_priority)
    if filters.missing_competitor_price:
        no_price = [f"{alias}.selling_price_cents IS NULL" for alias in competitor_state_aliases().values()]
        no_price.append("mp.selling_price_cents IS NULL")
        clauses.append("(" + " AND ".join(no_price) + ")")
    if filters.hidden_competitor_price:
        clauses.append(
            """
            EXISTS (
                SELECT 1 FROM competitor_probe_results hp
                WHERE hp.competitor_key='motosport'
                  AND hp.manufacturer=p.manufacturer
                  AND UPPER(hp.oem_part_number)=UPPER(p.oem_part_number)
                  AND (hp.price_visibility='see_price_in_cart' OR hp.result_type='price_hidden_in_cart')
            )
            """
        )
    if filters.needs_review:
        clauses.append("COALESCE(prd.review_status, ?) = ?")
        params.extend([PENDING_REVIEW, PENDING_REVIEW])
    if filters.review_state == "reviewed":
        clauses.append("(prd.suggested_new_price_cents IS NOT NULL AND ips.our_current_price_cents = prd.suggested_new_price_cents)")
    elif filters.review_state == "pending":
        clauses.append("NOT (prd.suggested_new_price_cents IS NOT NULL AND ips.our_current_price_cents = prd.suggested_new_price_cents)")
    if filters.import_batch_id:
        clauses.append("ips.source_import_batch_id=?")
        params.append(filters.import_batch_id)
    return " AND ".join(clauses), params


def _comparison_row(row: dict[str, Any]) -> dict[str, Any]:
    # Keep the comparison baseline stable after a saved catalog price change.
    comparison_price_cents = row.get("original_price_cents")
    if comparison_price_cents is None:
        comparison_price_cents = row["our_current_price_cents"]
    our = _money(comparison_price_cents)
    competitor = _money(row["partzilla_selling_price_cents"])
    cost = _money(row["current_cost_cents"])
    diff = None if our is None or competitor is None else our - competitor
    row["our_current_price"] = cents_to_money(comparison_price_cents)
    row["current_cost"] = cents_to_money(row["current_cost_cents"])
    # Every registered competitor gets its money fields and status derived here,
    # so adding an adapter needs no changes in this function.
    for adapter in list_competitors():
        key = adapter.competitor_key
        row[f"{key}_selling_price"] = cents_to_money(row.get(f"{key}_selling_price_cents"))
        row[f"{key}_reference_price"] = cents_to_money(row.get(f"{key}_reference_price_cents"))
        if key == "motosport":
            continue  # Handled below: MotoSport can hide its price behind the cart.
        if row[f"{key}_selling_price"]:
            row[f"{key}_status"] = "Production"
        elif row.get(f"{key}_page_classification"):
            row[f"{key}_status"] = row.get(f"{key}_page_classification")
        else:
            row[f"{key}_status"] = ""
    row["motosport_hidden_price"] = row.get("motosport_price_visibility") == "see_price_in_cart" or row.get("motosport_result_type") == "price_hidden_in_cart"
    if row["motosport_hidden_price"]:
        row["motosport_status"] = "Needs Review / Hidden Price"
    elif row["motosport_selling_price"]:
        row["motosport_status"] = "Production" if row.get("motosport_source") == "production" else "Probe"
    elif row.get("motosport_page_classification"):
        row["motosport_status"] = row.get("motosport_page_classification")
    else:
        row["motosport_status"] = ""
    competitor_prices: dict[str, Decimal | None] = {}
    for adapter in list_competitors():
        key = adapter.competitor_key
        price = _money(row.get(f"{key}_selling_price_cents"))
        if key == "motosport" and row["motosport_hidden_price"]:
            price = None  # A cart-gated price is not a comparable price.
        competitor_prices[short_display_name(adapter)] = price
    available = {name: price for name, price in competitor_prices.items() if price is not None}
    lowest_name = min(available, key=available.get) if available else ""
    lowest_price = available[lowest_name] if lowest_name else None
    lowest_diff = None if our is None or lowest_price is None else our - lowest_price
    row["lowest_competitor_name"] = lowest_name
    row["lowest_competitor_key"] = lowest_name.lower()
    row["lowest_competitor_price"] = _format_decimal(lowest_price)
    row["difference_vs_lowest_competitor"] = _format_decimal(lowest_diff)
    if lowest_diff is None:
        row["gap_price_class"] = ""
    elif lowest_diff > 0:
        row["gap_price_class"] = "price-difference-higher"
    elif lowest_diff < 0:
        row["gap_price_class"] = "price-difference-lower"
    else:
        row["gap_price_class"] = ""
    if our is not None and available and any(our > price for price in available.values()):
        row["our_price_class"] = "price-above-competitor"
    elif our is not None and available and all(our < price for price in available.values()):
        row["our_price_class"] = "price-below-competitors"
    else:
        row["our_price_class"] = ""
    # Ordered cells for the UI, so templates iterate competitors instead of
    # naming them column by column.
    row["competitors"] = [
        {
            "key": adapter.competitor_key,
            "display_name": adapter.display_name,
            "short_name": short_display_name(adapter),
            "selling_price": row.get(f"{adapter.competitor_key}_selling_price"),
            "hidden_price": bool(row.get(f"{adapter.competitor_key}_hidden_price")),
            "status": row.get(f"{adapter.competitor_key}_status") or "",
            "is_lowest": row["lowest_competitor_key"] == adapter.competitor_key,
        }
        for adapter in list_competitors()
    ]
    row["price_difference_dollars"] = _format_decimal(diff)
    row["price_difference_cents"] = int(diff * Decimal("100")) if diff is not None else None
    row["price_difference_pct"] = _percent((our / competitor) - 1) if our is not None and competitor not in (None, Decimal("0")) else ""
    row["our_gross_margin_pct"] = _percent((our - cost) / our) if our not in (None, Decimal("0")) and cost is not None else ""
    row["margin_at_partzilla_price"] = _percent((competitor - cost) / competitor) if competitor not in (None, Decimal("0")) and cost is not None else ""
    row["review_status"] = row.get("review_status") or PENDING_REVIEW
    row["notes"] = row.get("notes") or ""
    row["suggested_new_price"] = cents_to_money(row.get("suggested_new_price_cents"))
    row["saved_to_catalog"] = row.get("suggested_new_price_cents") is not None and row.get("our_current_price_cents") == row.get("suggested_new_price_cents")
    row["reviewer"] = row.get("reviewer") or ""
    row["reviewed_at"] = row.get("reviewed_at") or ""
    row["applied_rule_codes_json"] = row.get("applied_rule_codes_json") or ""
    return row


def _money(cents: int | None) -> Decimal | None:
    return Decimal(cents) / Decimal("100") if cents is not None else None


def _format_decimal(value: Decimal | None) -> str:
    return "" if value is None else format(value.quantize(Decimal("0.01")), "f")


def _percent(value: Decimal) -> str:
    return format((value * Decimal("100")).quantize(Decimal("0.01")), "f")
