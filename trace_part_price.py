"""Trace one part's price from collection through to the database.

A price can be read correctly, written to the run's results file, and still
not reach the catalog if the upload that imports it never lands. This shows
each stage separately, so it is clear which one it stopped at.

    .venv\\Scripts\\python.exe trace_part_price.py partzilla 1334490
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "data" / "output" / "collection_runs"


def _when(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%b %d %H:%M UTC")


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: trace_part_price.py <competitor> <OEM part number>")
        return 1
    competitor, part = sys.argv[1].strip().lower(), sys.argv[2].strip()

    print("=" * 64)
    print(f"  Tracing {competitor} price for part {part}")
    print("=" * 64)

    # 1. What the collector concluded, from its own diagnostics.
    print("\n[1] What the collector recorded when it checked the page")
    diag_dirs = [
        ROOT / "data" / "output" / f"{competitor}_collection_diagnostics",
        ROOT / "data" / "output" / "authenticated_diagnostics",
    ]
    found_diag = False
    for base in diag_dirs:
        if not base.exists():
            continue
        matches = sorted(
            (d for d in base.iterdir() if d.is_dir() and part.upper() in d.name.upper()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for folder in matches[:3]:
            observation = folder / "observation.json"
            if not observation.exists():
                continue
            data = json.loads(observation.read_text(encoding="utf-8"))
            print(f"    {_when(observation)}  price={data.get('selling_price')}  warnings={data.get('parse_warnings')}")
            found_diag = True
    if not found_diag:
        print("    no diagnostics recorded for this part")

    # 2. Whether it reached the run's results file, which is what gets imported.
    print("\n[2] Whether it reached a run results file (this is what gets imported)")
    hits = 0
    if RUNS_DIR.exists():
        for summary in sorted(RUNS_DIR.glob("*/collection_summary.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                rows = list(csv.DictReader(summary.read_text(encoding="utf-8-sig").splitlines()))
            except Exception:
                continue
            for row in rows:
                if str(row.get("oem_part_number", "")).strip().upper() != part.upper():
                    continue
                if str(row.get("competitor", "")).strip().lower() != competitor:
                    continue
                hits += 1
                print(
                    f"    {_when(summary)}  run {summary.parent.name}"
                    f"  price={row.get('selling_price') or '(none)'}"
                    f"  result={row.get('result_type')}"
                )
    if not hits:
        print("    NOT in any run results file.")
        print("    The collector read a price but the run never wrote it to its results,")
        print("    so there is nothing for the import or the recovery tool to pick up.")

    # 3. What the database actually holds now.
    print("\n[3] What the database holds now")
    try:
        from app.config import DEFAULT_DATABASE_PATH
        from app.database import connect_database

        with connect_database(DEFAULT_DATABASE_PATH) as conn:
            row = conn.execute(
                """
                SELECT s.selling_price_cents, s.last_successful_check_at
                FROM current_listing_state s
                JOIN competitor_listings l ON l.listing_id = s.listing_id
                JOIN competitors c ON c.competitor_id = l.competitor_id
                JOIN products p ON p.product_id = l.product_id
                WHERE c.competitor_code = ? AND UPPER(p.oem_part_number) = ?
                """,
                (competitor, part.upper()),
            ).fetchone()
        if row is None:
            print(f"    no {competitor} listing row exists for this part")
        elif row["selling_price_cents"] is None:
            print(f"    listing exists but has no price (last checked {row['last_successful_check_at']})")
        else:
            print(f"    price {row['selling_price_cents'] / 100:.2f} (last checked {row['last_successful_check_at']})")
    except Exception as exc:
        print(f"    could not read the database: {exc}")

    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
