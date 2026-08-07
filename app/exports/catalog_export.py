"""Export the product catalog to a spreadsheet.

Mirrors what the catalog screen shows, including one price column per
registered competitor, so adding a competitor cannot silently leave it out of
an export.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.xlsx_utils import write_workbook


def catalog_columns() -> list[str]:
    from app.competitors.registry import list_competitors, short_display_name

    competitor_columns = [f"{short_display_name(adapter)}_Price" for adapter in list_competitors()]
    return [
        "Manufacturer",
        "OEM_Part_Number",
        "Internal_SKU",
        "Product_Name",
        *competitor_columns,
        "Lowest_Competitor_Price",
        "Gap_Vs_Lowest",
        "Our_Current_Price",
        "Calc_Cost",
        "Our_Margin_Pct",
        "Inventory",
        "Scan_Priority",
        "Last_Checked",
        "Needs_Review",
    ]


def export_catalog(products: list[dict[str, Any]], output_dir: Path) -> Path:
    from app.competitors.registry import list_competitors

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"Product_Catalog_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.xlsx"

    rows: list[list[Any]] = [catalog_columns()]
    for product in products:
        rows.append(
            [
                product.get("manufacturer", ""),
                product.get("oem_part_number", ""),
                product.get("internal_sku", ""),
                product.get("product_name", ""),
                *[
                    _num((product.get(adapter.competitor_key) or {}).get("price"))
                    for adapter in list_competitors()
                ],
                _num(product.get("lowest_competitor_price")),
                _num(product.get("difference_vs_lowest_competitor")),
                _num(product.get("our_current_price")),
                _num(product.get("current_cost")),
                _pct(product.get("our_margin_pct")),
                product.get("inventory_qty", ""),
                product.get("scan_priority", ""),
                product.get("last_checked_at", ""),
                "Yes" if product.get("needs_review") else "No",
            ]
        )

    write_workbook(
        path,
        {
            "Product Catalog": rows,
            "Export Summary": [
                ["Rows Exported", len(products)],
                ["Generated At", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")],
            ],
        },
    )
    return path


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(value: Any) -> float | None:
    number = _num(value)
    return None if number is None else number / 100
