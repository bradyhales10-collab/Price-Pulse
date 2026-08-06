"""End-to-end tests for resuming a price check after a mid-run sign-in expiry.

These reproduce, with real databases rather than mocks, exactly what happens
when a competitor's saved sign-in expires partway through a large run:
whether already-checked parts are correctly skipped on retry, and whether the
progress reported to the dashboard reflects that (rather than appearing to
reset to zero).
"""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path

from app.database import connect_database, create_scan_run, seed_partzilla
from local_collector import already_attempted_part_keys, prepare_local_database

COLUMNS = [
    "Test_Case_ID", "Manufacturer", "OEM_Part_Number", "Search_Observed_Product_Name",
    "Search_Observed_MSRP", "Expected_Partzilla_URL", "Test_Purpose", "Verified_Date", "Source_URL",
]


def _write_input_csv(path: Path, part_count: int) -> None:
    with path.open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(COLUMNS)
        for index in range(part_count):
            writer.writerow([f"T{index}", "Kawasaki", f"PART-{index:04d}", "", "", "", "", "", ""])


def _record_scan_events(local_db: Path, scan_run_id: int, listing_ids: list[int]) -> None:
    with connect_database(local_db) as conn:
        for listing_id in listing_ids:
            conn.execute(
                "INSERT INTO scan_events(scan_run_id, listing_id, checked_at, page_classification, "
                "session_status, navigation_succeeded, price_found, parse_warning_count) "
                "VALUES (?,?,?,?,?,?,?,0)",
                (scan_run_id, listing_id, "2026-08-06T00:00:00Z", "normal_product", "authenticated", 1, 1),
            )


def test_prepare_local_database_predicts_the_real_scan_run_id_correctly() -> None:
    """The retry mechanism depends on knowing, in advance, which scan_run_id a
    collection run will be assigned, so it can later look up what that run
    actually attempted. If this prediction were wrong, already_attempted_part_keys
    would silently look at the wrong (or a nonexistent) run and find nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_csv = tmp_path / "parts.csv"
        _write_input_csv(input_csv, 10)

        local_db = tmp_path / "collector-partzilla.db"
        predicted_run_id = prepare_local_database(input_csv, local_db, ["partzilla"], run_id_floor=5000)

        with connect_database(local_db) as conn:
            competitor_id = seed_partzilla(conn)
            real_run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=10)

        assert real_run_id == predicted_run_id


def test_a_mid_run_interruption_does_not_repeat_already_checked_parts() -> None:
    """The actual scenario reported: a 1000-part run has 200 parts genuinely
    checked before a sign-in expires. The retry must skip exactly those 200
    and attempt only the remaining 800, not start over from part one."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_csv = tmp_path / "parts.csv"
        _write_input_csv(input_csv, 1000)

        local_db = tmp_path / "collector-partzilla.db"
        run_id = prepare_local_database(input_csv, local_db, ["partzilla"], run_id_floor=5000)

        with connect_database(local_db) as conn:
            competitor_id = seed_partzilla(conn)
            real_run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=1000)
            assert real_run_id == run_id
            listings = conn.execute(
                "SELECT l.listing_id FROM competitor_listings l "
                "JOIN products p ON p.product_id=l.product_id "
                "ORDER BY p.oem_part_number LIMIT 200"
            ).fetchall()
        listing_ids = [row["listing_id"] for row in listings]
        _record_scan_events(local_db, real_run_id, listing_ids)

        already_attempted = already_attempted_part_keys(local_db, real_run_id)
        assert len(already_attempted) == 200

        retry_db = tmp_path / "collector-partzilla-retry.db"
        prepare_local_database(
            input_csv, retry_db, ["partzilla"], run_id_floor=9000, skip_keys=already_attempted
        )
        with connect_database(retry_db) as conn:
            remaining = conn.execute("SELECT COUNT(*) count FROM products").fetchone()["count"]

        assert remaining == 800, (
            f"expected 800 parts remaining after skipping 200 already-checked ones, got {remaining}. "
            "If this is wrong, a mid-run sign-in expiry would cause the retry to recheck everything, "
            "which looks exactly like the whole competitor restarting from scratch."
        )


def test_zero_already_attempted_parts_means_nothing_is_skipped() -> None:
    """If a competitor's session expires on the very first part, there is
    nothing to skip, and the retry should plan the full list - this is not a
    bug, just an empty overlap."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_csv = tmp_path / "parts.csv"
        _write_input_csv(input_csv, 50)

        local_db = tmp_path / "collector-partzilla.db"
        run_id = prepare_local_database(input_csv, local_db, ["partzilla"], run_id_floor=1000)
        with connect_database(local_db) as conn:
            seed_partzilla(conn)

        already_attempted = already_attempted_part_keys(local_db, run_id)
        assert already_attempted == set()

        retry_db = tmp_path / "collector-partzilla-retry.db"
        prepare_local_database(input_csv, retry_db, ["partzilla"], run_id_floor=2000, skip_keys=already_attempted)
        with connect_database(retry_db) as conn:
            remaining = conn.execute("SELECT COUNT(*) count FROM products").fetchone()["count"]
        assert remaining == 50


def test_already_attempted_part_keys_is_unaffected_by_the_50_row_progress_cap() -> None:
    """The progress payload keeps only the last 50 rows for display, but the
    skip decision must be based on the full scan_events history in the local
    database, not that truncated in-memory list - otherwise an interruption
    after part 300 would only remember the last 50 and redo the first 250."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_csv = tmp_path / "parts.csv"
        _write_input_csv(input_csv, 400)

        local_db = tmp_path / "collector-partzilla.db"
        prepare_local_database(input_csv, local_db, ["partzilla"], run_id_floor=3000)
        with connect_database(local_db) as conn:
            competitor_id = seed_partzilla(conn)
            real_run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=400)
            listings = conn.execute(
                "SELECT l.listing_id FROM competitor_listings l "
                "JOIN products p ON p.product_id=l.product_id "
                "ORDER BY p.oem_part_number LIMIT 300"
            ).fetchall()
        _record_scan_events(local_db, real_run_id, [row["listing_id"] for row in listings])

        already_attempted = already_attempted_part_keys(local_db, real_run_id)

        assert len(already_attempted) == 300, "must reflect all 300 attempts, not just the last 50"


def test_a_stale_or_unrelated_scan_run_id_finds_nothing_to_skip() -> None:
    """Looking up the wrong run (for example, a leftover ID from an earlier,
    unrelated attempt) must not silently match unrelated events."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_csv = tmp_path / "parts.csv"
        _write_input_csv(input_csv, 20)

        local_db = tmp_path / "collector-partzilla.db"
        prepare_local_database(input_csv, local_db, ["partzilla"], run_id_floor=1000)

        already_attempted = already_attempted_part_keys(local_db, scan_run_id=999999)
        assert already_attempted == set()


def test_a_missing_local_database_is_handled_without_crashing() -> None:
    already_attempted = already_attempted_part_keys(Path("/nonexistent/path.db"), scan_run_id=1)
    assert already_attempted == set()
