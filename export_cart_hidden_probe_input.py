from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from app.config import DATA_DIR, INPUT_DIR, ensure_data_directories


OUTPUT_FIELDS = [
    "manufacturer",
    "oem_part_number",
    "product_name",
    "product_url",
    "reference_price",
    "prior_probe_timestamp",
    "price_visibility",
    "result_type",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export cart-hidden MotoSport rows from a prior safe product-page probe.")
    parser.add_argument("--competitor", required=True)
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--probe-dir", type=Path)
    parser.add_argument("--output", type=Path, default=INPUT_DIR / "MotoSport_Cart_Hidden_Probe.csv")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.competitor != "motosport":
        print("Error: cart-hidden export currently supports MotoSport only.")
        return 1
    try:
        probe_dir = _resolve_probe_dir(args)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    rows = export_cart_hidden_rows(probe_dir, args.output)
    print(f"Exported {len(rows)} cart-hidden rows to {args.output}")
    return 0


def export_cart_hidden_rows(probe_dir: Path, output: Path) -> list[dict[str, str]]:
    ensure_data_directories()
    summary = probe_dir / "probe_summary.csv"
    if not summary.exists():
        raise ValueError(f"Missing probe summary: {summary}")
    prior_probe_timestamp = probe_dir.name
    output_rows: list[dict[str, str]] = []
    with summary.open("r", newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            if not _is_cart_hidden(row):
                continue
            output_rows.append(
                {
                    "manufacturer": row.get("manufacturer", ""),
                    "oem_part_number": row.get("oem_part_number", ""),
                    "product_name": row.get("product_name", ""),
                    "product_url": row.get("url", ""),
                    "reference_price": row.get("reference_price", ""),
                    "prior_probe_timestamp": prior_probe_timestamp,
                    "price_visibility": row.get("price_visibility", ""),
                    "result_type": row.get("result_type", ""),
                }
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(output_rows)
    return output_rows


def _resolve_probe_dir(args: argparse.Namespace) -> Path:
    if args.probe_dir:
        return args.probe_dir
    if not args.latest:
        raise ValueError("Pass --latest or --probe-dir.")
    base = DATA_DIR / "output" / "competitor_probes" / args.competitor
    dirs = sorted([path for path in base.glob("*") if path.is_dir()], key=lambda path: path.name, reverse=True)
    if not dirs:
        raise ValueError(f"No probe output directories found under {base}")
    return dirs[0]


def _is_cart_hidden(row: dict[str, str]) -> bool:
    return (
        row.get("price_visibility") == "see_price_in_cart"
        or row.get("result_type") == "price_hidden_in_cart"
        or row.get("cart_hidden_price", "").lower() == "true"
        or row.get("see_price_in_cart_detected", "").lower() == "true"
    )


if __name__ == "__main__":
    sys.exit(main())
