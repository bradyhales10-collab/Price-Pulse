"""Clear competitor prices of zero or less that were stored before validation.

A zero is not a real price. It becomes the lowest competitor price and would
drive a pricing decision to an absurd conclusion, so it is safer to have no
price than a wrong one. New zeros are now rejected when parsed and again when
stored; this clears any that were recorded earlier.

    .venv\\Scripts\\python.exe clear_zero_prices.py
"""

from __future__ import annotations

import sys

from app.config import DEFAULT_DATABASE_PATH
from app.database import connect_database, utc_now


def main() -> int:
    if not DEFAULT_DATABASE_PATH.exists():
        print(f"No database found at {DEFAULT_DATABASE_PATH}")
        return 1

    with connect_database(DEFAULT_DATABASE_PATH) as conn:
        affected = conn.execute(
            """
            SELECT c.competitor_code, p.manufacturer, p.oem_part_number,
                   s.selling_price_cents, s.reference_price_cents
            FROM current_listing_state s
            JOIN competitor_listings l ON l.listing_id = s.listing_id
            JOIN competitors c ON c.competitor_id = l.competitor_id
            JOIN products p ON p.product_id = l.product_id
            WHERE s.selling_price_cents <= 0 OR s.reference_price_cents <= 0
            """
        ).fetchall()

        if not affected:
            print("No prices of zero or less found. Nothing to clear.")
            return 0

        print(f"Found {len(affected)} price(s) of zero or less:")
        for row in affected:
            print(
                f"  {row['competitor_code']:10s} {row['manufacturer']:10s} "
                f"{row['oem_part_number']:20s} selling={row['selling_price_cents']} "
                f"reference={row['reference_price_cents']}"
            )

        conn.execute(
            "UPDATE current_listing_state SET selling_price_cents = NULL, updated_at = ? "
            "WHERE selling_price_cents <= 0",
            (utc_now(),),
        )
        conn.execute(
            "UPDATE current_listing_state SET reference_price_cents = NULL, updated_at = ? "
            "WHERE reference_price_cents <= 0",
            (utc_now(),),
        )

    print("")
    print("Cleared. Those parts now show no price for that competitor, which is")
    print("correct: the price was never real. They will be picked up properly on")
    print("the next price check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
