"""End-to-end proof that a failure anywhere in the per-part loop - not only in
the collector call itself - is caught, logged with a full traceback, and
reported as a clear failure, instead of silently ending the run partway
through with a status that looks like a normal finish.

This is the exact shape of bug behind a real report: Chaparral stopped at 95
of 994 parts with status "completed_with_warnings", no stop_reason recorded
anywhere, and no crash file. The only place in the loop that was not wrapped
in a try/except was everything other than the collector call itself -
recording progress, checking the stop condition, and so on.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from unittest.mock import patch

import collect_parts
from app.collection import CollectionRow
from app.database import connect_database, initialize_database, normalize_part_number, utc_now
from app.input_loader import load_parts_csv


class _FakePage:
    def set_default_timeout(self, timeout) -> None:
        return None

    def set_default_navigation_timeout(self, timeout) -> None:
        return None


class _FakeContext:
    def new_page(self):
        return _FakePage()

    def route(self, *args, **kwargs) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeBrowser:
    def new_context(self, **kwargs):
        return _FakeContext()

    def close(self) -> None:
        return None


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    @property
    def chromium(self):
        class _Chromium:
            def launch(self, **kwargs):
                return _FakeBrowser()

        return _Chromium()


def _fake_collection_row(planned, competitor_key: str) -> CollectionRow:
    return CollectionRow(
        run_order=planned.run_order,
        scan_run_id=1,
        scan_event_id=None,
        manufacturer=planned.manufacturer,
        oem_part_number=planned.oem_part_number,
        normalized_manufacturer=planned.manufacturer,
        competitor=competitor_key,
        manufacturer_supported=True,
        lookup_status="price_found",
        status_reason="",
        observed_part_number=planned.oem_part_number,
        product_name="x",
        checked_at=utc_now(),
        http_status=200,
        page_classification="normal_product",
        session_status="public",
        selling_price="10.00",
        reference_price=None,
        savings_percent=None,
        price_display_type="regular",
        previous_selling_price=None,
        result_type="first_observation",
        price_changed=False,
        availability_raw="",
        previous_availability_status=None,
        availability_status="in_stock",
        supersession_detected=False,
        superseded_by_raw=None,
        price_source_category=f"{competitor_key}_search",
        price_corroboration_count=1,
        price_parse_confidence="high",
        parse_confidence="high",
        warning_count=0,
        warnings="",
        observation_json_path="",
    )


def _build_plan(tmp_path: Path, part_count: int):
    input_csv = tmp_path / "parts.csv"
    columns = [
        "Test_Case_ID", "Manufacturer", "OEM_Part_Number", "Search_Observed_Product_Name",
        "Search_Observed_MSRP", "Expected_Partzilla_URL", "Test_Purpose", "Verified_Date", "Source_URL",
    ]
    with input_csv.open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        for index in range(part_count):
            writer.writerow([f"T{index}", "Polaris", f"PART-{index:04d}", "", "", "", "", "", ""])

    database = tmp_path / "chaparral.db"
    initialize_database(database)
    now = utc_now()
    with connect_database(database) as conn:
        for index in range(part_count):
            part_number = f"PART-{index:04d}"
            conn.execute(
                "INSERT INTO products(product_id, manufacturer, oem_part_number, normalized_part_number, "
                "product_name, is_active, created_at, updated_at) VALUES (?,?,?,?, 'X',1,?,?)",
                (index + 1, "Polaris", part_number, normalize_part_number(part_number), now, now),
            )
            conn.execute(
                "INSERT INTO internal_product_state(product_id, internal_sku, our_current_price_cents, "
                "is_active, updated_at) VALUES (?,?,1000,1,?)",
                (index + 1, f"SKU{index}", now),
            )

    with connect_database(database) as conn:
        from app.collection import plan_collection

        records = load_parts_csv(input_csv).records
        collect_parts.ensure_competitor_listings(conn, records, "chaparral")
        plan = plan_collection(conn, records, input_csv, part_count, competitor_key="chaparral")

    return input_csv, database, plan


def test_a_failure_outside_the_collector_call_is_caught_not_silent(tmp_path) -> None:
    """Everything in the per-part loop other than the collector call itself
    used to be unprotected: recording progress, checking whether to stop,
    counting consecutive errors. A failure there ended the run silently,
    with no stop_reason and no crash file, looking exactly like a clean but
    partial finish."""
    input_csv, database, plan = _build_plan(tmp_path, part_count=10)
    args = argparse.Namespace(
        file=input_csv,
        database=database,
        max_parts=10,
        delay_seconds=0,
        collection_mode="lightweight_browser",
        headless=True,
        progress_file=str(tmp_path / "progress.json"),
    )

    calls = {"count": 0}
    real_write_progress = collect_parts._write_progress

    def flaky_write_progress(args, result, plan, *, status, started_monotonic):
        calls["count"] += 1
        if calls["count"] == 4:
            raise ValueError("simulated failure outside the collector call")
        return real_write_progress(args, result, plan, status=status, started_monotonic=started_monotonic)

    def fake_collector(database, page, planned, scan_run_id, settings, *, delay_seconds):
        return _fake_collection_row(planned, "chaparral")

    with (
        patch.object(collect_parts, "sync_playwright", lambda: _FakePlaywright()),
        patch.object(collect_parts, "PRODUCTION_COLLECTORS", {"chaparral": fake_collector}),
        patch.object(collect_parts, "_write_progress", flaky_write_progress),
        patch.object(collect_parts, "OUTPUT_DIR", tmp_path),
    ):
        collect_parts.run_collection(args, plan)

    crash_files = list((tmp_path / "collection_crashes").glob("*loop*"))
    assert len(crash_files) == 1
    crash_text = crash_files[0].read_text(encoding="utf-8")
    assert "parts completed before this: 3 of 10" in crash_text
    assert "ValueError" in crash_text
    assert "Traceback" in crash_text

    with connect_database(database) as conn:
        row = conn.execute("SELECT run_status FROM scan_runs ORDER BY scan_run_id DESC LIMIT 1").fetchone()
    assert row["run_status"] == "failed"


def test_a_normal_run_with_no_failures_writes_no_loop_crash_file(tmp_path) -> None:
    """The new backstop must not fire, or interfere, when nothing goes wrong."""
    input_csv, database, plan = _build_plan(tmp_path, part_count=5)
    args = argparse.Namespace(
        file=input_csv,
        database=database,
        max_parts=5,
        delay_seconds=0,
        collection_mode="lightweight_browser",
        headless=True,
        progress_file=str(tmp_path / "progress.json"),
    )

    def fake_collector(database, page, planned, scan_run_id, settings, *, delay_seconds):
        return _fake_collection_row(planned, "chaparral")

    with (
        patch.object(collect_parts, "sync_playwright", lambda: _FakePlaywright()),
        patch.object(collect_parts, "PRODUCTION_COLLECTORS", {"chaparral": fake_collector}),
        patch.object(collect_parts, "OUTPUT_DIR", tmp_path),
    ):
        collect_parts.run_collection(args, plan)

    crash_dir = tmp_path / "collection_crashes"
    assert not crash_dir.exists() or not list(crash_dir.glob("*loop*"))

    with connect_database(database) as conn:
        row = conn.execute("SELECT run_status FROM scan_runs ORDER BY scan_run_id DESC LIMIT 1").fetchone()
    assert row["run_status"] == "completed"


def test_a_closed_browser_stops_the_run_immediately() -> None:
    """A real 997-part RevZilla run stopped at part 316 having recorded
    "Page.goto: Target page, context or browser has been closed" four times.
    Once the browser is gone nothing can succeed, so counting those as page
    errors wasted four parts producing identical failures and then reported it
    as though the pages were at fault."""
    from collect_parts import browser_is_gone

    for message in (
        "Page.goto: Target page, context or browser has been closed",
        "Page.content: Target page, context or browser has been closed",
        "Target closed",
        "Connection closed while reading from the driver",
    ):
        assert browser_is_gone(message), message


def test_an_ordinary_page_failure_is_not_mistaken_for_a_closed_browser() -> None:
    """Stopping the whole run on a recoverable error would be worse than the
    problem being fixed, so this has to be specific."""
    from collect_parts import browser_is_gone

    for message in (
        "Timeout 8000ms exceeded",
        "net::ERR_NAME_NOT_RESOLVED",
        "Locator.inner_text: Timeout 5000ms exceeded",
        "",
        None,
    ):
        assert not browser_is_gone(message), message


def test_the_stop_reason_says_the_browser_closed() -> None:
    """Someone reading the result should not have to infer this from a count of
    page errors."""
    from pathlib import Path

    source = Path("collect_parts.py").read_text(encoding="utf-8")

    assert "the browser closed unexpectedly at part" in source
    assert "browser_is_gone(row.status_reason)" in source
