"""Show the actual error behind an 'error' result in the most recent price check.

The dashboard only shows a coarse summary like '2 error'. The real exception
message is recorded per-row but not shown there. This reads it straight out
of the progress file so it can be shared without digging through the code or
the database.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent / "data" / "output" / "local_bridge"


def find_progress_files(competitor: str | None = None) -> list[Path]:
    if not BRIDGE_DIR.exists():
        return []
    pattern = f"progress-*-{competitor}.json" if competitor else "progress-*.json"
    return sorted(BRIDGE_DIR.rglob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)


def main() -> int:
    competitor = sys.argv[1] if len(sys.argv) > 1 else None
    files = find_progress_files(competitor)
    if not files:
        label = f" for {competitor}" if competitor else ""
        print(f"No price check progress found{label}.")
        print("Run a price check first, then run this again right after.")
        return 1

    shown = 0
    for path in files[:5]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = data.get("rows") or []
        errors = [row for row in rows if isinstance(row, dict) and row.get("result_type") == "error"]
        if not errors:
            continue
        print(f"=== {path.name} ===")
        print(f"    competitor: {data.get('competitor', '?')}")
        for row in errors:
            print(f"    part {row.get('oem_part_number', '?')}: {row.get('status_reason', '(no message recorded)')}")
        print("")
        shown += 1
        if shown >= 3:
            break

    if not shown:
        label = f" for {competitor}" if competitor else ""
        print(f"No 'error' rows found in recent progress files{label}.")
        print("If the run failed a different way (blocked, no price found, etc.), that is not this kind of error.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
