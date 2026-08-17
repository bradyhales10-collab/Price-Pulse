from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.xlsx_utils import write_workbook


def review_columns() -> list[str]:
    """Export columns, with one price column per registered competitor.

    Built from the competitor registry rather than hardcoded, so adding a
    competitor puts it in the export automatically. The previous fixed list
    silently omitted RevZilla, making an export look like RevZilla had found
    nothing when it had simply never been included.

    Original_Price is deliberately absent: it was always empty, since nothing
    populates it, so it only added a blank column to every export.
    """
    from app.competitors.registry import list_competitors, short_display_name

    competitor_columns = [f"{short_display_name(a)}_Price" for a in list_competitors()]
    return [
        "Internal_SKU",
        "Manufacturer",
        "OEM_Part_Number",
        "Product_Name",
        *competitor_columns,
        "Lowest_Competitor",
        "Lowest_Competitor_Price",
        "Gap_Vs_Lowest",
        "Our_Current_Price",
        "Calc_Cost",
        "Margin_Pct",
        "Suggested_Price",
        "Updated_Price",
        "New_Margin_Pct",
        # The new engine, shown alongside rather than replacing the existing
        # suggestion, so the two can be compared on real parts in a
        # spreadsheet rather than one product page at a time.
        "Type_Of_Part",
        "Category_Confidence",
        "Qty_Sold_Annualized",
        "Sensitivity",
        "Sensitivity_Score",
        "New_Action",
        "New_Suggested_Price",
        "New_Margin_At_Suggested",
        "Competitor_Confidence",
        "Raw_Lowest_Competitor_Name",
        "Raw_Lowest_Competitor_Price",
        "Lowest_Validated_Competitor_Price",
        "Validated_Competitor_Median",
        "Valid_Competitor_Count",
        "Excluded_Competitor_Count",
        "Excluded_Competitors",
        "Annual_Competitive_Price_Exposure",
        "Pricing_Rule_Applied",
        "Target_Tier_Qualification",
        "Target_Percent_Of_Lowest",
        "Competitive_Target_Before_Margin_Floor",
        "Why",
    ]


def export_review(
    rows: list[dict[str, Any]], output_dir: Path, *, minimum_margin: Decimal = Decimal("20")
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"Pricing_Update_Review_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.xlsx"
    from app.competitors.registry import list_competitors
    from app.pricing_view import recommendation_for_row

    review_rows = [review_columns()]
    for row in rows:
        # A failure here must not lose the whole export, so a part that cannot
        # be assessed simply has empty columns.
        try:
            recommendation = recommendation_for_row(row, minimum_margin=minimum_margin) or {}
        except Exception:
            recommendation = {}
        review_rows.append(
            [
                row.get("internal_sku", ""),
                row.get("manufacturer", ""),
                row.get("oem_part_number", ""),
                row.get("product_name", ""),
                *[_num(row.get(f"{a.competitor_key}_selling_price")) for a in list_competitors()],
                row.get("lowest_competitor_name", ""),
                _num(row.get("lowest_competitor_price")),
                _num(row.get("difference_vs_lowest_competitor")),
                _num(row.get("our_current_price")),
                _num(row.get("current_cost")),
                _pct(row.get("our_gross_margin_pct")),
                _num((row.get("rule_suggestion") or {}).get("suggested_price") if isinstance(row.get("rule_suggestion"), dict) else ""),
                _num(row.get("suggested_new_price")),
                _pct(row.get("updated_margin_pct")),
                recommendation.get("category", ""),
                recommendation.get("category_confidence", ""),
                recommendation.get("annualized_qty", ""),
                recommendation.get("sensitivity", ""),
                recommendation.get("sensitivity_score", ""),
                recommendation.get("action_label", ""),
                _num(recommendation.get("recommended_price")),
                _num(recommendation.get("projected_margin_pct")),
                recommendation.get("competitor_confidence", ""),
                recommendation.get("raw_lowest_name", ""),
                _num(recommendation.get("raw_lowest_price")),
                _num(recommendation.get("lowest_valid")),
                _num(recommendation.get("median_valid")),
                recommendation.get("valid_competitor_count", ""),
                recommendation.get("excluded_competitor_count", ""),
                recommendation.get("excluded_competitors", ""),
                _num(recommendation.get("annual_competitive_price_exposure")),
                recommendation.get("rule_applied", ""),
                recommendation.get("target_tier_qualification", ""),
                _num(recommendation.get("target_percent_of_lowest")),
                _num(recommendation.get("competitive_target_price")),
                recommendation.get("reason", ""),
            ]
        )
    write_workbook(
        path,
        {
            "Pricing Review": review_rows,
            "Export Summary": [["Rows Exported", len(rows)], ["Generated At", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")]],
            "Instructions": [["This export mirrors the Price Comparison working list through New Margin."], ["When a row is Approved, Updated_Price is also written back as the current product price."]],
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
