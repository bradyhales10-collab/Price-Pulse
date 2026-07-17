from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.comparison import comparison_rows
from app.reviews import ALL_STATUSES, review_rows


def management_summary(database: Path) -> dict[str, Any]:
    rows = comparison_rows(database)
    reviewed_rows = review_rows(database, status=ALL_STATUSES)
    pending = [row for row in reviewed_rows if row["review_status"] == "Pending Review"]
    approved = [row for row in reviewed_rows if row["review_status"] == "Approved"]
    updated_rows = [row for row in reviewed_rows if _money(row.get("suggested_new_price")) is not None]
    increases = [row for row in updated_rows if _price_delta(row) and _price_delta(row) > 0]
    decreases = [row for row in updated_rows if _price_delta(row) and _price_delta(row) < 0]
    ready_for_export = [row for row in approved if _money(row.get("suggested_new_price")) is not None]
    annual_price_impact = sum(((_price_delta(row) or Decimal("0")) * _quantity(row.get("units_sold_12m")) for row in approved), Decimal("0"))
    return {
        "total_products": len(rows),
        "priced_by_competitor": sum(1 for row in rows if row.get("lowest_competitor_name")),
        "missing_competitor_price": sum(1 for row in rows if not row.get("lowest_competitor_name")),
        "hidden_price_review": sum(1 for row in rows if row.get("motosport_hidden_price")),
        "pending_review": len(pending),
        "approved_updates": len(approved),
        "ready_for_export": len(ready_for_export),
        "suggested_increases": len(increases),
        "suggested_decreases": len(decreases),
        "annual_price_impact": _format_money(annual_price_impact),
        "actions": [
            {"label": "Start Price Check", "value": len(rows), "href": "/imports", "detail": "Upload or rerun a parts file."},
            {"label": "Review Exceptions", "value": len(pending), "href": "/reviews", "detail": "Approve, hold, or investigate pricing decisions."},
            {"label": "Ready To Export", "value": len(ready_for_export), "href": "/reviews?status=Approved", "detail": "Approved rows with an updated price."},
            {"label": "Data Issues", "value": sum(1 for row in rows if not row.get("lowest_competitor_name") or not row.get("current_cost")), "href": "/quality", "detail": "Missing costs or competitor prices."},
        ],
    }


def _price_delta(row: dict[str, Any]) -> Decimal | None:
    updated = _money(row.get("suggested_new_price"))
    current = _money(row.get("our_current_price"))
    if updated is None or current is None:
        return None
    return updated - current


def _money(value: object) -> Decimal | None:
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
