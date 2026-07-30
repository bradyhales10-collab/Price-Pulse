from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime

from app.browser_probe import probe_partzilla_page
from app.config import DEFAULT_INPUT_CSV, DIAGNOSTICS_DIR, ProbeSettings, ensure_data_directories
from app.input_loader import PartNotFoundError, find_part_record, load_parts_csv
from app.logging_setup import setup_logging

LOGGER = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one controlled Partzilla browser probe.")
    parser.add_argument("--part-number", required=True, help="OEM part number to probe from the CSV.")
    parser.add_argument("--headless", action="store_true", help="Run Chromium without a visible window.")
    parser.add_argument("--slow-mo", type=int, default=0, help="Playwright slow motion in milliseconds.")
    parser.add_argument("--timeout", type=int, default=30000, help="Navigation timeout in milliseconds.")
    return parser.parse_args()


def write_startup_failure_report(exc: Exception) -> None:
    ensure_data_directories()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = DIAGNOSTICS_DIR / f"{stamp}_startup_failure.txt"
    report_path.write_text(
        "\n".join(
            [
                f"timestamp: {stamp}",
                "navigation_succeeded: False",
                "failure_stage: startup",
                f"exception_type: {type(exc).__name__}",
                f"exception_message: {exc}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Startup failure diagnostics saved to: {report_path}")


def main() -> int:
    args = parse_args()
    ensure_data_directories()
    log_file = setup_logging()
    LOGGER.info("Application log: %s", log_file)
    print(f"Application log: {log_file}")

    try:
        print(f"Loading input CSV: {DEFAULT_INPUT_CSV}")
        load_result = load_parts_csv(DEFAULT_INPUT_CSV)
        for invalid in load_result.invalid_rows:
            LOGGER.warning("Invalid CSV row %s: %s", invalid.row_number, invalid.reason)

        record = find_part_record(load_result.records, args.part_number)
        print(f"Found part {record.oem_part_number}; opening one Partzilla page...")
        diagnostics = probe_partzilla_page(
            record=record,
            settings=ProbeSettings(
                headless=args.headless,
                slow_mo=args.slow_mo,
                timeout=args.timeout,
            ),
        )
    except (FileNotFoundError, ValueError, PartNotFoundError) as exc:
        LOGGER.error("%s", exc)
        print(f"Error: {exc}")
        return 1
    except Exception as exc:
        LOGGER.exception("Probe startup failed.")
        print(f"Unexpected startup error: {type(exc).__name__}: {exc}")
        write_startup_failure_report(exc)
        return 1

    print(f"Final URL: {diagnostics.final_url or ''}")
    print(f"HTTP response status: {diagnostics.http_status if diagnostics.http_status is not None else ''}")
    print(f"Page title: {diagnostics.page_title or ''}")
    print(f"Detected signals: {', '.join(diagnostics.detected_signals) or 'None'}")
    print(f"Navigation succeeded: {diagnostics.navigation_succeeded}")
    if diagnostics.exception_message:
        print(f"Exception: {diagnostics.exception_message}")
    print(f"Screenshot: {diagnostics.screenshot_path or ''}")
    print(f"HTML: {diagnostics.html_path or ''}")
    print("Diagnostics saved under data/output/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
