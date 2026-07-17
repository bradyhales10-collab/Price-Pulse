from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.xlsx_utils import write_workbook


REVIEW_COLUMNS = [
    "Internal_SKU",
    "Manufacturer",
    "OEM_Part_Number",
    "Product_Name",
    "Our_Current_Price",
    "Calc_Cost",
    "Partzilla_Selling_Price",
    "Partzilla_Reference_Price",
    "Partzilla_Savings_Pct",
    "MotoSport_Selling_Price",
    "MotoSport_Reference_Price",
    "Chaparral_Selling_Price",
    "Chaparral_Reference_Price",
    "Price_Difference_Dollars",
    "Price_Difference_Pct",
    "Our_Gross_Margin_Pct",
    "Margin_At_Partzilla_Price",
    "Units_Sold_12M",
    "Inventory_Qty",
    "Scan_Priority",
    "Updated_Price",
    "Applied_Rule_Names",
    "Rule_Skip_Unsafe_Competitor_Data",
    "Rule_Use_Lowest_Competitor",
    "Rule_Round_To_99",
    "Rule_Protect_Minimum_Margin",
    "Rule_Effects",
    "Review_Status",
    "Notes",
]

RULE_EXPORT_COLUMNS = [
    ("skip_unsafe_competitor_data", "Skip Unsafe Competitor Data"),
    ("use_lowest_competitor", "Use Lowest Competitor"),
    ("round_to_99", "Round To .99"),
    ("protect_minimum_margin", "Protect Minimum Margin"),
]


def export_review(rows: list[dict[str, Any]], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"Pricing_Update_Review_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.xlsx"
    review_rows = [REVIEW_COLUMNS]
    for row in rows:
        review_rows.append(
            [
                row.get("internal_sku", ""),
                row.get("manufacturer", ""),
                row.get("oem_part_number", ""),
                row.get("product_name", ""),
                _num(row.get("our_current_price")),
                _num(row.get("current_cost")),
                _num(row.get("partzilla_selling_price")),
                _num(row.get("partzilla_reference_price")),
                _pct(row.get("partzilla_savings_percent")),
                _num(row.get("motosport_selling_price")),
                _num(row.get("motosport_reference_price")),
                _num(row.get("chaparral_selling_price")),
                _num(row.get("chaparral_reference_price")),
                _num(row.get("price_difference_dollars")),
                _pct(row.get("price_difference_pct")),
                _pct(row.get("our_gross_margin_pct")),
                _pct(row.get("margin_at_partzilla_price")),
                row.get("units_sold_12m"),
                row.get("inventory_qty"),
                row.get("scan_priority", ""),
                _num(row.get("suggested_new_price")),
                _applied_rule_names(row),
                *[_rule_selected(row, code) for code, _name in RULE_EXPORT_COLUMNS],
                _rule_effects(row),
                row.get("review_status", "Pending Review"),
                row.get("notes", ""),
            ]
        )
    write_workbook(
        path,
        {
            "Pricing Review": review_rows,
            "Export Summary": [["Rows Exported", len(rows)], ["Generated At", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")]],
            "Instructions": [["Updated_Price is the price set in Review Queue."], ["When a row is Approved, Updated_Price is also written back as the current product price."]],
        },
        styled_review=True,
    )
    return path


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _pct(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value) / 100


def _applied_rule_names(row: dict[str, Any]) -> str:
    rules = _applied_rules(row)
    return ", ".join(rule.get("rule_name", "") for rule in rules if rule.get("selected"))


def _rule_selected(row: dict[str, Any], rule_code: str) -> str:
    for rule in _applied_rules(row):
        if rule.get("rule_code") == rule_code:
            return "Yes" if rule.get("selected") else "No"
    return "No"


def _rule_effects(row: dict[str, Any]) -> str:
    return " | ".join(
        f"{rule.get('rule_name')}: {rule.get('effect')}"
        for rule in _applied_rules(row)
        if rule.get("selected")
    )


def _applied_rules(row: dict[str, Any]) -> list[dict[str, Any]]:
    suggestion = row.get("rule_suggestion") or {}
    rules = suggestion.get("applied_rules") if isinstance(suggestion, dict) else None
    return rules if isinstance(rules, list) else []
