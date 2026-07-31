"""Build a competitor probe input file from your own catalog.

Hand-picking parts to test tells you little, because the sample may not reflect
what you actually sell. This exports the parts you sell most of, limited to the
manufacturers the competitor carries, so a probe answers the question that
matters: of the parts we compete on, how many does this competitor stock and
price?
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from app.competitors.registry import get_competitor
from app.config import DATA_DIR, DEFAULT_DATABASE_PATH
from app.web.queries import connect_readonly

HARD_MAX_PARTS = 25
OUTPUT_FIELDS = [
    "Test_Case_ID",
    "Manufacturer",
    "OEM_Part_Number",
    "Search_Observed_Product_Name",
    "Search_Observed_MSRP",
    "Expected_Partzilla_URL",
    "Test_Purpose",
    "Verified_Date",
    "Source_URL",
    "Internal_SKU",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a probe input file from your own catalog.")
    parser.add_argument("--competitor", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--max-parts", type=int, default=HARD_MAX_PARTS)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def select_parts(database: Path, competitor_key: str, max_parts: int) -> list[dict[str, object]]:
    """Best sellers first, restricted to manufacturers this competitor carries."""
    adapter = get_competitor(competitor_key)
    supported = list(adapter.supported_manufacturers)
    if not supported:
        return []

    placeholders = ",".join("?" for _ in supported)
    with connect_readonly(database) as conn:
        rows = conn.execute(
            f"""
            SELECT p.manufacturer, p.oem_part_number,
                   COALESCE(p.product_name, '') product_name,
                   COALESCE(ips.internal_sku, '') internal_sku,
                   COALESCE(ips.units_sold_12m, 0) units_sold_12m,
                   COALESCE(ips.inventory_qty, 0) inventory_qty
            FROM products p
            JOIN internal_product_state ips ON ips.product_id = p.product_id
            WHERE ips.is_active = 1
              AND p.manufacturer IN ({placeholders})
              AND TRIM(COALESCE(p.oem_part_number, '')) <> ''
            ORDER BY units_sold_12m DESC, p.oem_part_number COLLATE NOCASE
            LIMIT ?
            """,
            [*supported, max_parts],
        ).fetchall()
    return [dict(row) for row in rows]


def write_probe_file(path: Path, competitor_key: str, parts: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for index, part in enumerate(parts, start=1):
            writer.writerow(
                {
                    "Test_Case_ID": f"{competitor_key.upper()}-OWN-{index:03d}",
                    "Manufacturer": part["manufacturer"],
                    "OEM_Part_Number": part["oem_part_number"],
                    "Search_Observed_Product_Name": part["product_name"],
                    "Search_Observed_MSRP": "",
                    "Expected_Partzilla_URL": "",
                    "Test_Purpose": (
                        f"From our own catalog. Sold {part['units_sold_12m']} in 12 months, "
                        f"{part['inventory_qty']} on hand."
                    ),
                    "Verified_Date": "",
                    "Source_URL": "",
                    "Internal_SKU": part["internal_sku"],
                }
            )


def main() -> int:
    args = parse_args()
    if args.max_parts < 1 or args.max_parts > HARD_MAX_PARTS:
        print(f"Error: --max-parts must be between 1 and {HARD_MAX_PARTS}.")
        return 1
    try:
        adapter = get_competitor(args.competitor)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    if not args.database.exists():
        print(f"Error: no database at {args.database}. Upload a parts file in Part Pulse first.")
        return 1

    parts = select_parts(args.database, adapter.competitor_key, args.max_parts)
    if not parts:
        print(
            f"No parts found for {adapter.display_name}. It carries "
            f"{', '.join(adapter.supported_manufacturers) or 'nothing'}, "
            "and your catalog has no active parts for those manufacturers."
        )
        return 1

    output = args.output or (DATA_DIR / "input" / f"{adapter.competitor_key}_own_catalog_probe.csv")
    write_probe_file(output, adapter.competitor_key, parts)

    print(f"{adapter.display_name} carries: {', '.join(adapter.supported_manufacturers)}")
    print(f"Exported {len(parts)} of your own parts, best sellers first:")
    print("")
    for part in parts:
        print(
            f"  {str(part['manufacturer']):12s} {str(part['oem_part_number']):18s} "
            f"sold_12m={part['units_sold_12m']:<6} on_hand={part['inventory_qty']}"
        )
    print("")
    print(f"Probe file: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
