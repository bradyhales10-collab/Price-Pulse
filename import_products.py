from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import DEFAULT_DATABASE_PATH
from app.database import connect_database, initialize_database, upsert_product_and_listing
from app.input_loader import load_parts_csv


def main() -> int:
    parser = argparse.ArgumentParser(description="Import product master rows and Partzilla listings.")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = load_parts_csv(args.file)
    products_inserted = products_updated = listings_inserted = listings_updated = 0
    if not args.dry_run:
        initialize_database(args.database)
        with connect_database(args.database) as conn:
            with conn:
                for record in result.records:
                    _, _, product_inserted, listing_inserted = upsert_product_and_listing(conn, record)
                    products_inserted += int(product_inserted)
                    products_updated += int(not product_inserted)
                    listings_inserted += int(listing_inserted)
                    listings_updated += int(not listing_inserted)
    print(f"Rows read: {len(result.records)}")
    print(f"Products inserted: {products_inserted}")
    print(f"Products updated: {products_updated}")
    print(f"Listings inserted: {listings_inserted}")
    print(f"Listings updated: {listings_updated}")
    print(f"Invalid rows: {len(result.invalid_rows)}")
    print(f"Database path: {args.database}")
    if args.dry_run:
        print("Dry run: no database changes written")
    return 0 if not result.invalid_rows else 1


if __name__ == "__main__":
    sys.exit(main())
