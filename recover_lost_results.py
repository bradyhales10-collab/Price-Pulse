"""Recover price-check results that were collected but never made it into the
database, because uploading them to Part Pulse failed.

collect_parts.py always writes what it finds to
    data/output/collection_runs/<scan_run_id>/collection_summary.csv
independent of whether the Dashboard was reachable at the time. If the
Browser Helper could not upload that file - for example because the Dashboard
had stopped or restarted - the parts it checked would show as never checked,
even though the work was genuinely done and is sitting on disk.

This finds those summaries and imports them directly into the database, no
Dashboard connection required.

Run it from the Price-Pulse folder:
    .venv\\Scripts\\python.exe recover_lost_results.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.collector_bridge import import_collection_summary
from app.config import DEFAULT_DATABASE_PATH, OUTPUT_DIR

RUNS_DIR = OUTPUT_DIR / "collection_runs"
DEFAULT_LOOKBACK_HOURS = 72


def find_recoverable_runs(hours: int) -> list[Path]:
    """Every collection_summary.csv from the last `hours` hours, oldest first."""
    if not RUNS_DIR.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    found: list[tuple[float, Path]] = []
    for summary in RUNS_DIR.glob("*/collection_summary.csv"):
        if summary.stat().st_mtime >= cutoff.timestamp():
            found.append((summary.stat().st_mtime, summary))
    found.sort(key=lambda item: item[0])
    return [path for _, path in found]


def describe(summary: Path) -> str:
    metadata_path = summary.parent / "run_metadata.json"
    if not metadata_path.exists():
        return summary.parent.name
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return summary.parent.name
    competitor = metadata.get("competitor", "unknown")
    attempted = metadata.get("attempted_part_count", "?")
    successful = metadata.get("successful_part_count", "?")
    started = metadata.get("started_at", "")
    return f"{competitor}: {successful} of {attempted} parts had a result (started {started})"


def main() -> int:
    hours = DEFAULT_LOOKBACK_HOURS
    if len(sys.argv) > 1:
        try:
            hours = int(sys.argv[1])
        except ValueError:
            print(f"'{sys.argv[1]}' is not a number of hours.")
            return 1

    print("=" * 64)
    print("  Recovering price check results not yet in the database")
    print("=" * 64)
    print("")

    if not DEFAULT_DATABASE_PATH.exists():
        print(f"No database found at {DEFAULT_DATABASE_PATH}.")
        return 1

    summaries = find_recoverable_runs(hours)
    if not summaries:
        print(f"No collection results found from the last {hours} hours.")
        print("Nothing to recover.")
        return 0

    print(f"Found {len(summaries)} run(s) from the last {hours} hours:")
    print("")
    for summary in summaries:
        print(f"  {describe(summary)}")
    print("")
    print("Importing all of them now. This is safe to do even if some of these")
    print("were already imported successfully - it will not create bad data,")
    print("only, at worst, repeat a little history for a run that did not")
    print("actually need recovering.")
    print("")

    imported = 0
    failed = 0
    for summary in summaries:
        try:
            result = import_collection_summary(
                DEFAULT_DATABASE_PATH,
                summary_csv=summary.read_bytes(),
                fallback_competitor=None,
            )
            print(
                f"  Imported {summary.parent.name}: {result.successful_rows} of "
                f"{result.rows_imported} rows had a usable result."
            )
            imported += 1
        except Exception as exc:
            print(f"  Could not import {summary.parent.name}: {exc}")
            failed += 1

    print("")
    print("=" * 64)
    print(f"  Done. Imported {imported} run(s), {failed} failed.")
    print("=" * 64)
    if failed:
        print("")
        print("Send Claude the error(s) above for whichever runs failed to import.")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
