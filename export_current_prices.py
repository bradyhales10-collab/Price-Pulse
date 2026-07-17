from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from app.config import DEFAULT_DATABASE_PATH, OUTPUT_DIR
from app.database import cents_to_money, connect_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Export current Partzilla prices.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR / "current_partzilla_prices.csv")
    args = parser.parse_args()
    rows = export_current_prices(args.database, args.output)
    print(f"Rows exported: {len(rows)}")
    print(f"Output: {args.output}")
    return 0


def export_current_prices(database: Path = DEFAULT_DATABASE_PATH, output: Path = OUTPUT_DIR / "current_partzilla_prices.csv") -> list[dict[str, str]]:
    rows = _rows(database)
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "manufacturer","oem_part_number","internal_sku","product_name","partzilla_price","currency_code",
        "partzilla_reference_price","savings_percent","price_display_type","selling_price_confidence","reference_price_confidence",
        "availability_raw","availability_status","supersession_detected","superseded_by_raw","price_confidence",
        "last_successful_check","last_changed","canonical_url",
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
            SELECT p.manufacturer, p.oem_part_number, p.internal_sku, s.product_name, s.selling_price_cents,
                   s.reference_price_cents, s.savings_percent, s.price_display_type, s.selling_price_confidence,
                   s.reference_price_confidence,
                   s.currency_code, s.availability_raw, s.availability_status, s.supersession_detected,
                   s.superseded_by_raw, s.price_parse_confidence, s.last_successful_check_at, s.last_changed_at,
                   l.canonical_url
            FROM current_listing_state s
            JOIN competitor_listings l ON l.listing_id=s.listing_id
            JOIN products p ON p.product_id=l.product_id
            ORDER BY p.oem_part_number
            """
        ).fetchall()
    return [
        {
            "manufacturer": row["manufacturer"],
            "oem_part_number": row["oem_part_number"],
            "internal_sku": row["internal_sku"] or "",
            "product_name": row["product_name"] or "",
            "partzilla_price": cents_to_money(row["selling_price_cents"]),
            "partzilla_reference_price": cents_to_money(row["reference_price_cents"]),
            "savings_percent": str(row["savings_percent"]) if row["savings_percent"] is not None else "",
            "price_display_type": row["price_display_type"] or "",
            "selling_price_confidence": row["selling_price_confidence"] or "",
            "reference_price_confidence": row["reference_price_confidence"] or "",
            "currency_code": row["currency_code"],
            "availability_raw": row["availability_raw"] or "",
            "availability_status": row["availability_status"] or "",
            "supersession_detected": str(bool(row["supersession_detected"])),
            "superseded_by_raw": row["superseded_by_raw"] or "",
            "price_confidence": row["price_parse_confidence"] or "",
            "last_successful_check": row["last_successful_check_at"] or "",
            "last_changed": row["last_changed_at"] or "",
            "canonical_url": row["canonical_url"],
        }
        for row in rows
    ]


if __name__ == "__main__":
    sys.exit(main())
