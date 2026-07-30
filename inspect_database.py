from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import DEFAULT_DATABASE_PATH
from app.database import cents_to_money, connect_database, table_counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Part Pulse database contents.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--part-number")
    args = parser.parse_args()
    with connect_database(args.database) as conn:
        if args.part_number:
            _print_part(conn, args.part_number)
        else:
            _print_summary(conn)
    return 0


def _print_summary(conn) -> None:
    counts = table_counts(conn)
    print("DATABASE SUMMARY")
    labels = {
        "products": "Products",
        "competitors": "Competitors",
        "competitor_listings": "Listings",
        "scan_runs": "Scan runs",
        "scan_events": "Scan events",
        "current_listing_state": "Current states",
        "listing_history": "History records",
    }
    for key, label in labels.items():
        print(f"{label}: {counts[key]}")
    print()
    print("CURRENT PARTZILLA PRICES")
    rows = conn.execute(
        """
        SELECT p.oem_part_number, s.product_name, s.selling_price_cents, s.availability_raw,
               s.availability_status, s.supersession_detected, s.last_successful_check_at, s.last_changed_at
        FROM current_listing_state s
        JOIN competitor_listings l ON l.listing_id=s.listing_id
        JOIN products p ON p.product_id=l.product_id
        ORDER BY p.oem_part_number
        """
    ).fetchall()
    for row in rows:
        print(
            f"{row['oem_part_number']} | {row['product_name'] or ''} | {cents_to_money(row['selling_price_cents'])} | "
            f"{row['availability_raw'] or row['availability_status'] or ''} | {bool(row['supersession_detected'])} | "
            f"{row['last_successful_check_at']} | {row['last_changed_at']}"
        )
    print()
    print("RECENT PRICE CHANGES")
    for row in conn.execute(
        """
        SELECT h.effective_at, p.oem_part_number, h.change_type, h.previous_selling_price_cents, h.new_selling_price_cents,
               h.change_details_json
        FROM listing_history h
        JOIN competitor_listings l ON l.listing_id=h.listing_id
        JOIN products p ON p.product_id=l.product_id
        ORDER BY h.effective_at DESC LIMIT 20
        """
    ):
        print(
            f"{row['effective_at']} | {row['oem_part_number']} | {row['change_type']} | "
            f"{cents_to_money(row['previous_selling_price_cents'])} -> {cents_to_money(row['new_selling_price_cents'])}"
        )


def _print_part(conn, part_number: str) -> None:
    row = conn.execute(
        """
        SELECT p.*, l.*, s.* FROM products p
        JOIN competitor_listings l ON l.product_id=p.product_id
        LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
        WHERE p.normalized_part_number=?
        """,
        (part_number.strip().upper(),),
    ).fetchone()
    if row is None:
        print(f"Part not found: {part_number}")
        return
    print("PRODUCT")
    print(f"{row['manufacturer']} {row['oem_part_number']} {row['product_name'] or ''}")
    print("LISTING")
    print(f"{row['canonical_url']}")
    print("CURRENT STATE")
    print(f"Price: {cents_to_money(row['selling_price_cents'])}")
    print(f"Availability: {row['availability_raw'] or row['availability_status'] or ''}")
    print(f"Superseded: {bool(row['supersession_detected'])} {row['superseded_by_raw'] or ''}")
    print("SCAN EVENTS")
    for event in conn.execute("SELECT * FROM scan_events WHERE listing_id=? ORDER BY checked_at", (row["listing_id"],)):
        print(f"{event['checked_at']} | {event['page_classification']} | price_found={bool(event['price_found'])} | warnings={event['parse_warnings'] or ''}")
    print("CHANGE HISTORY")
    for hist in conn.execute("SELECT * FROM listing_history WHERE listing_id=? ORDER BY effective_at", (row["listing_id"],)):
        print(f"{hist['effective_at']} | {hist['change_type']} | {cents_to_money(hist['previous_selling_price_cents'])} -> {cents_to_money(hist['new_selling_price_cents'])}")


if __name__ == "__main__":
    sys.exit(main())
