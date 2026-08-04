"""Explain what happened in the most recent price check.

The dashboard says things like "Local collection failed for: chaparral,
partzilla", which does not say what went wrong or how many parts were affected.
Everything needed is already written to disk per run; this reads it and
summarises it in one place.

Run it from the Price-Pulse folder:
    .venv\\Scripts\\python.exe explain_last_run.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BRIDGE_DIR = ROOT / "data" / "output" / "local_bridge"
CRASH_DIR = ROOT / "data" / "output" / "collection_crashes"

# Result types that are a normal outcome rather than something to fix.
EXPECTED_RESULTS = {
    "selling_price_found",
    "no_change",
    "price_change",
    "multiple_changes",
    "manufacturer_not_carried",
}

PLAIN_ENGLISH = {
    "manufacturer_not_carried": "this competitor does not carry that brand (expected, not a fault)",
    "no_price_found": "the page loaded but showed no usable price",
    "part_not_found": "the competitor does not list that part",
    "lookup_failed": "the part could not be looked up",
    "error": "an unexpected error, see the crash files below",
    "navigation_error": "the page did not load",
    "authentication_lost": "the saved sign-in stopped working partway through",
    "blocked_or_rate_limited": "the site refused the request",
    "captcha_detected": "the site presented a challenge",
    "out_of_stock": "listed but out of stock, so the price was not used",
    "discontinued": "discontinued, so the price was not used",
    "price_ignored_missing_cents": "a price was found but looked truncated, so it was not used",
}


def latest_progress_files() -> dict[str, Path]:
    """Newest progress file per competitor."""
    if not BRIDGE_DIR.exists():
        return {}
    newest: dict[str, tuple[float, Path]] = {}
    for path in BRIDGE_DIR.glob("progress-*.json"):
        name = path.stem
        parts = name.split("-", 2)
        if len(parts) < 3:
            continue
        competitor = parts[2]
        stamp = path.stat().st_mtime
        if competitor not in newest or stamp > newest[competitor][0]:
            newest[competitor] = (stamp, path)
    return {competitor: path for competitor, (_, path) in newest.items()}


def describe(competitor: str, path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  Could not read {path.name}: {exc}")
        return

    rows = [row for row in (data.get("rows") or []) if isinstance(row, dict)]
    status = str(data.get("run_status") or data.get("status") or "unknown")
    completed = data.get("completed")
    total = data.get("total")
    when = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%b %d %H:%M UTC")

    print(f"  {competitor.upper()}")
    print(f"    finished {when}, status: {status}")
    if completed is not None and total:
        print(f"    checked {completed} of {total} parts")
    if data.get("stop_reason"):
        print(f"    stopped early because: {data['stop_reason']}")
    if data.get("message"):
        print(f"    message: {data['message']}")

    if not rows:
        print("    no per-part results were recorded")
        print("")
        return

    priced = [row for row in rows if row.get("selling_price")]
    print(f"    prices captured: {len(priced)} of {len(rows)} parts attempted")

    results = Counter(str(row.get("result_type") or "unknown") for row in rows)
    print("    outcomes:")
    for result, count in results.most_common():
        note = PLAIN_ENGLISH.get(result, "")
        flag = "" if result in EXPECTED_RESULTS else "  <-- worth a look"
        suffix = f" ({note})" if note else ""
        print(f"      {count:5d}  {result}{suffix}{flag}")

    reasons = Counter(
        str(row.get("status_reason") or "").strip()
        for row in rows
        if str(row.get("result_type") or "") not in EXPECTED_RESULTS
        and str(row.get("status_reason") or "").strip()
    )
    if reasons:
        print("    reasons given:")
        for reason, count in reasons.most_common(6):
            trimmed = reason if len(reason) <= 110 else reason[:107] + "..."
            print(f"      {count:5d}  {trimmed}")

    warnings = Counter()
    for row in rows:
        for warning in str(row.get("warnings") or "").split(";"):
            warning = warning.strip()
            if warning:
                warnings[warning] += 1
    if warnings:
        print("    warnings:")
        for warning, count in warnings.most_common(6):
            print(f"      {count:5d}  {warning}")

    examples = [
        row for row in rows if str(row.get("result_type") or "") not in EXPECTED_RESULTS
    ][:3]
    if examples:
        print("    examples:")
        for row in examples:
            print(
                f"      {row.get('oem_part_number', '?')} "
                f"({row.get('manufacturer', '?')}): {row.get('result_type', '?')}"
                f"{' - ' + str(row.get('status_reason'))[:70] if row.get('status_reason') else ''}"
            )
    print("")


def main() -> int:
    print("=" * 64)
    print("  What happened in the last price check")
    print("=" * 64)
    print("")

    files = latest_progress_files()
    if not files:
        print("No price check results found yet.")
        print("Run a price check first, then run this straight afterwards.")
        return 1

    for competitor in sorted(files):
        describe(competitor, files[competitor])

    crashes = sorted(CRASH_DIR.glob("*.txt")) if CRASH_DIR.exists() else []
    if crashes:
        print("=" * 64)
        print(f"  Unexpected errors ({len(crashes)} file(s))")
        print("=" * 64)
        print("")
        for path in crashes[:5]:
            print(f"  {path.name}")
            lines = [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()]
            for line in lines:
                if "Error" in line or "error" in line:
                    print(f"      {line.strip()[:120]}")
                    break
        if len(crashes) > 5:
            print(f"  ...and {len(crashes) - 5} more in {CRASH_DIR}")
        print("")
        print("  These are code faults rather than site behaviour. Send them to Claude.")
    else:
        print("No unexpected errors were recorded, so nothing crashed.")
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
