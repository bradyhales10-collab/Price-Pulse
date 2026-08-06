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


def test_two_sequential_sign_in_expirations_correctly_accumulate_to_the_full_total(monkeypatch, tmp_path) -> None:
    """Reproduces the exact scenario reported: a sign-in expires, is fixed,
    expires again, is fixed again, and the run finishes having only checked a
    small number of parts in that final stretch. Traced end to end through the
    real _run_job_body: 1000 planned, 400 checked before the first expiry, 574
    more checked before the second (974 cumulative), then the remaining 26
    checked successfully. The small final number is the size of that last
    batch, not the whole result - 400 + 574 + 26 = 1000, and the completion
    message should say so explicitly rather than leave that ambiguous.
    """
    import csv
    import json
    from unittest.mock import patch

    import local_collector_agent as agent
    from app.database import connect_database, create_scan_run, seed_partzilla

    attempts = {"count": 0}
    reported_progress: list[dict[str, object]] = []
    final_message = {}

    def fake_request_json(url, auth_header, *, method="GET", payload=None, allow_empty=False):
        if url.endswith("/cancelled"):
            return {"cancelled": False}
        if "/progress/" in url and payload:
            reported_progress.append(dict(payload))
        if "/complete?" in url and payload:
            final_message.update(payload)
        return {}

    def fake_run_competitor(
        input_path, local_db, max_parts, competitor, runner_args, *,
        expected_run_id, progress_callback, should_cancel,
    ):
        attempts["count"] += 1
        attempt_number = attempts["count"]
        with connect_database(local_db) as conn:
            competitor_id = seed_partzilla(conn)
            real_run_id = create_scan_run(conn, competitor_id=competitor_id, requested_part_count=max_parts)
            assert real_run_id == expected_run_id
            listings = conn.execute(
                "SELECT l.listing_id FROM competitor_listings l "
                "JOIN products p ON p.product_id=l.product_id ORDER BY p.oem_part_number"
            ).fetchall()

        checked_here = {1: 400, 2: 574}.get(attempt_number, len(listings))
        checked_ids = [row["listing_id"] for row in listings[:checked_here]]
        with connect_database(local_db) as conn:
            for listing_id in checked_ids:
                conn.execute(
                    "INSERT INTO scan_events(scan_run_id, listing_id, checked_at, page_classification, "
                    "session_status, navigation_succeeded, price_found, parse_warning_count) "
                    "VALUES (?,?,?,?,?,?,?,0)",
                    (real_run_id, listing_id, "2026-08-06T00:00:00Z", "normal_product", "authenticated", 1, 1),
                )

        completed_this_attempt = attempt_number >= 3
        (tmp_path / f"progress-{expected_run_id}-{competitor}.json").write_text(
            json.dumps(
                {
                    "status": "completed" if completed_this_attempt else "failed",
                    "stop_reason": None if completed_this_attempt else "authentication_lost",
                    "completed": checked_here,
                    "total": max_parts,
                }
            ),
            encoding="utf-8",
        )
        return tmp_path / f"summary-{expected_run_id}.csv"

    input_csv = tmp_path / "src.csv"
    columns = [
        "Test_Case_ID", "Manufacturer", "OEM_Part_Number", "Search_Observed_Product_Name",
        "Search_Observed_MSRP", "Expected_Partzilla_URL", "Test_Purpose", "Verified_Date", "Source_URL",
    ]
    with input_csv.open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        for index in range(1000):
            writer.writerow([f"T{index}", "Kawasaki", f"PART-{index:04d}", "", "", "", "", "", ""])

    with (
        patch.object(agent, "_request_json", fake_request_json),
        patch.object(agent, "_download", lambda url, path, auth: path.write_bytes(input_csv.read_bytes())),
        patch.object(agent, "saved_session_is_usable", lambda key: (True, "saved")),
        patch.object(agent, "verify_saved_session", lambda *a, **k: (True, "confirmed")),
        patch.object(agent, "first_probe_part", lambda path, key: None),
        patch.object(agent, "delete_auth_state", lambda key: None),
        patch.object(agent, "_run_competitor", fake_run_competitor),
        patch.object(agent, "_upload", lambda *a, **k: {"status": "imported"}),
        patch.object(agent, "_open_login_refresh", lambda request: None),
        patch.object(
            agent,
            "_wait_for_saved_sign_in",
            lambda competitor, *, report, should_cancel, total, **k: (True, "saved"),
        ),
        patch.object(agent, "BRIDGE_DIR", tmp_path),
    ):
        agent._run_job_body(
            {"job_id": "job-double-retry", "competitors": ["partzilla"], "input_url": "/x", "planned_count": 1000},
            {},
            "http://server",
            None,
            "agent-1",
        )

    assert attempts["count"] == 3, "expected exactly three attempts: original plus two retries"

    resume_messages = [item for item in reported_progress if "Resuming" in str(item.get("message", ""))]
    assert len(resume_messages) == 2
    assert resume_messages[0]["completed"] == 400
    assert resume_messages[1]["completed"] == 974, (
        "the second resume must report the cumulative total (400 + 574), not just "
        "the 574 checked in the most recent attempt alone"
    )

    assert final_message.get("status") == "completed"
    assert "1000" in final_message.get("message", ""), (
        "the completion message should state the real total explicitly, so a small "
        "final batch (26 parts) is never mistaken for the whole result"
    )
