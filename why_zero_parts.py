"""Show why a competitor planned zero parts in the last run.

plan_collection skips a part when the product or that competitor's listing is
missing from its local database, and reports those separately. This runs the
same planning against the last run's own files and prints the counts.

    .venv\\Scripts\\python.exe why_zero_parts.py partzilla
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGENT_JOBS = ROOT / "data" / "output" / "local_bridge" / "agent_jobs"


def main() -> int:
    competitor = (sys.argv[1] if len(sys.argv) > 1 else "partzilla").strip().lower()
    if not AGENT_JOBS.exists():
        print(f"No run data found at {AGENT_JOBS}")
        return 1
    jobs = sorted(AGENT_JOBS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    job = next((j for j in jobs if (j / "collector-input.csv").exists()), None)
    if job is None:
        print("No run with an input file found.")
        return 1

    input_csv = job / "collector-input.csv"
    database = job / f"collector-{competitor}.db"
    print(f"Run:        {job.name}")
    print(f"Input:      {input_csv}")
    print(f"Database:   {database}  (exists: {database.exists()})")
    print("")

    from app.collection import plan_collection
    from app.database import connect_database
    from app.input_loader import load_parts_csv

    records = load_parts_csv(input_csv).records
    print(f"Parts in the input file: {len(records)}")
    if not database.exists():
        print("That competitor has no database from this run, so nothing could be planned.")
        return 1

    with connect_database(database) as conn:
        plan = plan_collection(conn, records, input_csv, max_parts=max(len(records), 1), competitor_key=competitor)
        listings = conn.execute(
            "SELECT COUNT(*) FROM competitor_listings l JOIN competitors c"
            " ON c.competitor_id=l.competitor_id AND c.competitor_code=?",
            (competitor,),
        ).fetchone()[0]
        products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    print(f"Products in that database:        {products}")
    print(f"{competitor} listings in it:      {listings}")
    print("")
    print(f"Parts actually planned:           {len(plan.planned_parts)}")
    print(f"Skipped, product not found:       {len(plan.missing_products)}")
    print(f"Skipped, no {competitor} listing: {len(plan.missing_listings)}")
    print("")
    if plan.missing_listings:
        print("Examples with no listing:")
        for part in plan.missing_listings[:5]:
            print(f"  {part}")
    if plan.missing_products:
        print("Examples with no product row:")
        for part in plan.missing_products[:5]:
            print(f"  {part}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
