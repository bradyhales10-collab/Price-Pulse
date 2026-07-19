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
    "Partzilla_Price",
    "MotoSport_Price",
    "Chaparral_Price",
    "Lowest_Competitor",
    "Lowest_Competitor_Price",
    "Gap_Vs_Lowest",
    "Original_Price",
    "Our_Current_Price",
    "Calc_Cost",
    "Margin_Pct",
    "Suggested_Price",
    "Updated_Price",
    "New_Margin_Pct",
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
                _num(row.get("partzilla_selling_price")),
                _num(row.get("motosport_selling_price")),
                _num(row.get("chaparral_selling_price")),
                row.get("lowest_competitor_name", ""),
                _num(row.get("lowest_competitor_price")),
                _num(row.get("difference_vs_lowest_competitor")),
                _num(row.get("original_price")),
                _num(row.get("our_current_price")),
                _num(row.get("current_cost")),
                _pct(row.get("our_gross_margin_pct")),
                _num((row.get("rule_suggestion") or {}).get("suggested_price") if isinstance(row.get("rule_suggestion"), dict) else ""),
                _num(row.get("suggested_new_price")),
                _pct(row.get("updated_margin_pct")),
            ]
        )
    write_workbook(
        path,
        {
            "Pricing Review": review_rows,
            "Export Summary": [["Rows Exported", len(rows)], ["Generated At", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")]],
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
