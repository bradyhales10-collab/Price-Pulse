"""Print the raw stored values for one part, straight from the database.

    .venv\\Scripts\\python.exe check_one_product.py 99530-10114-00
"""

from __future__ import annotations

import sys

from app.config import DEFAULT_DATABASE_PATH
from app.database import connect_database


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: check_one_product.py <OEM part number>")
        return 1
    part_number = sys.argv[1]

    with connect_database(DEFAULT_DATABASE_PATH) as conn:
        product = conn.execute(
            "SELECT product_id, manufacturer, oem_part_number, product_name FROM products WHERE oem_part_number = ?",
            (part_number,),
        ).fetchone()
        if product is None:
            print(f"No product found with OEM part number '{part_number}'.")
            return 1

        print("products row:")
        print(dict(product))

        state = conn.execute(
            "SELECT * FROM internal_product_state WHERE product_id = ?",
            (product["product_id"],),
        ).fetchone()
        print("")
        print("internal_product_state row:")
        print(dict(state) if state else "NOT FOUND - this part has no internal price/cost record at all")

    return 0


if __name__ == "__main__":
    sys.exit(main())
