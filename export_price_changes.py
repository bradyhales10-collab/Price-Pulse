from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from app.config import DEFAULT_DATABASE_PATH, OUTPUT_DIR
from app.database import cents_to_money, connect_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Partzilla price changes.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR / "partzilla_price_changes.csv")
    args = parser.parse_args()
    rows = export_price_changes(args.database, args.output)
    print(f"Rows exported: {len(rows)}")
    print(f"Output: {args.output}")
    return 0


def export_price_changes(database: Path = DEFAULT_DATABASE_PATH, output: Path = OUTPUT_DIR / "partzilla_price_changes.csv") -> list[dict[str, str]]:
    rows = _rows(database)
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "effective_at","manufacturer","oem_part_number","product_name","change_type","previous_price","new_price",
        "dollar_change","percent_change","previous_availability","new_availability","previous_superseded_by","new_superseded_by",
    ]
    with output.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def _rows(database: Path) -> list[dict[str, str]]:
    with connect_database(database) as conn:
        rows = conn.execute(
            """
            SELECT h.*, p.manufacturer, p.oem_part_number, s.product_name
            FROM listing_history h
            JOIN competitor_listings l ON l.listing_id=h.listing_id
            JOIN products p ON p.product_id=l.product_id
            LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
            ORDER BY h.effective_at DESC
            """
        ).fetchall()
    output = []
    for row in rows:
        details = json.loads(row["change_details_json"]) if row["change_details_json"] else {}
        output.append(
            {
                "effective_at": row["effective_at"],
                "manufacturer": row["manufacturer"],
                "oem_part_number": row["oem_part_number"],
                "product_name": row["product_name"] or "",
                "change_type": row["change_type"],
                "previous_price": cents_to_money(row["previous_selling_price_cents"]),
                "new_price": cents_to_money(row["new_selling_price_cents"]),
                "dollar_change": details.get("dollar_change", ""),
                "percent_change": details.get("percent_change", ""),
                "previous_availability": row["previous_availability_status"] or "",
                "new_availability": row["new_availability_status"] or "",
                "previous_superseded_by": row["previous_superseded_by_raw"] or "",
                "new_superseded_by": row["new_superseded_by_raw"] or "",
            }
        )
    return output


if __name__ == "__main__":
    sys.exit(main())
