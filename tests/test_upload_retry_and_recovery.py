from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import local_collector as lc
import recover_lost_results as recover
from app.database import connect_database, initialize_database, normalize_part_number, utc_now

SUMMARY_CSV = (
    "competitor,manufacturer,oem_part_number,observed_part_number,result_type,selling_price,"
    "reference_price,savings_percent,price_display_type,availability_raw,availability_status,"
    "page_classification,session_status,parse_confidence,warnings,observation_json_path\n"
    "partzilla,Kawasaki,41080-1514,41080-1514,selling_price_found,12.34,,,regular,In Stock,in_stock,"
    "normal_product,public,high,,\n"
)


def test_a_transient_connection_failure_is_retried_and_recovers() -> None:
    """The exact failure mode reported: the Browser Helper's log showed
    'ConnectionRefusedError...actively refused it' while trying to reach the
    Dashboard. Before this, any single failed upload lost that competitor's
    results outright, since nothing ever tried again."""
    calls = {"count": 0}

    def flaky(url, path, auth_header):
        calls["count"] += 1
        if calls["count"] < 3:
            raise ConnectionRefusedError("actively refused it")
        return "imported"

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.csv"
        summary.write_text("a,b\n1,2\n", encoding="utf-8")
        with patch.object(lc, "_upload", flaky), patch.object(lc.time, "sleep", lambda seconds: None):
            result = lc.upload_with_retry("http://x", summary, None, attempts=5, delay_seconds=0)

    assert result == "imported"
    assert calls["count"] == 3


def test_a_real_server_error_is_not_retried() -> None:
    """A genuine problem on the server side will not be fixed by trying again,
    so this should fail immediately rather than waste time retrying it."""
    calls = {"count": 0}

    def server_error(url, path, auth_header):
        calls["count"] += 1
        raise RuntimeError("Upload failed: HTTP 500 server exploded")

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.csv"
        summary.write_text("a,b\n1,2\n", encoding="utf-8")
        with patch.object(lc, "_upload", server_error), patch.object(lc.time, "sleep", lambda seconds: None):
            try:
                lc.upload_with_retry("http://x", summary, None, attempts=5, delay_seconds=0)
            except RuntimeError:
                pass
            else:
                raise AssertionError("a real server error should have been raised")

    assert calls["count"] == 1


def test_a_sustained_outage_gives_up_after_the_attempt_cap() -> None:
    calls = {"count": 0}

    def always_fails(url, path, auth_header):
        calls["count"] += 1
        raise ConnectionRefusedError("still down")

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.csv"
        summary.write_text("a,b\n1,2\n", encoding="utf-8")
        with patch.object(lc, "_upload", always_fails), patch.object(lc.time, "sleep", lambda seconds: None):
            try:
                lc.upload_with_retry("http://x", summary, None, attempts=4, delay_seconds=0)
            except ConnectionRefusedError:
                pass
            else:
                raise AssertionError("a sustained outage should eventually raise")

    assert calls["count"] == 4


def test_the_agent_reports_the_local_file_location_when_upload_ultimately_fails() -> None:
    """Even after retrying, if the Dashboard is genuinely unreachable, the
    person needs to know their results are not lost - just not yet imported -
    and where to find them."""
    from pathlib import Path as _Path

    source = _Path("local_collector_agent.py").read_text(encoding="utf-8")

    assert "upload_with_retry" in source
    assert "recover_lost_results.py" in source
    assert "not lost" in source


def _write_recoverable_run(runs_dir: Path, run_id: str, competitor: str = "partzilla") -> None:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "collection_summary.csv").write_text(SUMMARY_CSV, encoding="utf-8")
    (run_dir / "run_metadata.json").write_text(
        json.dumps(
            {"competitor": competitor, "attempted_part_count": 1, "successful_part_count": 1, "started_at": "now"}
        ),
        encoding="utf-8",
    )


def _seed_product(db: Path) -> None:
    initialize_database(db)
    now = utc_now()
    with connect_database(db) as conn:
        conn.execute(
            "INSERT INTO products(product_id, manufacturer, oem_part_number, normalized_part_number, "
            "product_name, is_active, created_at, updated_at) VALUES (1,'Kawasaki','41080-1514',?, 'Disc',1,?,?)",
            (normalize_part_number("41080-1514"), now, now),
        )
        conn.execute(
            "INSERT INTO internal_product_state(product_id, internal_sku, our_current_price_cents, "
            "is_active, updated_at) VALUES (1,'SKU1',2000,1,?)",
            (now,),
        )


def _run_recover_main() -> int:
    original_argv = sys.argv
    sys.argv = ["recover_lost_results.py"]
    try:
        return recover.main()
    finally:
        sys.argv = original_argv


def test_recovery_imports_a_result_that_was_never_uploaded() -> None:
    """The concrete scenario this exists for: collect_parts.py wrote a real
    result to disk, but the upload that would have gotten it into the
    database never succeeded, so the part shows as never checked even though
    it genuinely was."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db = tmp_path / "pricing.db"
        _seed_product(db)

        runs_dir = tmp_path / "collection_runs"
        _write_recoverable_run(runs_dir, "12345")

        with patch.object(recover, "RUNS_DIR", runs_dir), patch.object(recover, "DEFAULT_DATABASE_PATH", db):
            exit_code = _run_recover_main()

        assert exit_code == 0
        with connect_database(db) as conn:
            row = conn.execute("SELECT selling_price_cents FROM current_listing_state").fetchone()
        assert row is not None
        assert row["selling_price_cents"] == 1234


def test_running_recovery_twice_does_not_corrupt_the_price() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db = tmp_path / "pricing.db"
        _seed_product(db)

        runs_dir = tmp_path / "collection_runs"
        _write_recoverable_run(runs_dir, "12345")

        with patch.object(recover, "RUNS_DIR", runs_dir), patch.object(recover, "DEFAULT_DATABASE_PATH", db):
            _run_recover_main()
            _run_recover_main()

        with connect_database(db) as conn:
            row = conn.execute("SELECT selling_price_cents FROM current_listing_state").fetchone()
        assert row["selling_price_cents"] == 1234


def test_recovery_only_looks_within_the_requested_time_window(monkeypatch) -> None:
    import os
    import time

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        runs_dir = tmp_path / "collection_runs"
        _write_recoverable_run(runs_dir, "old-run")
        old_summary = runs_dir / "old-run" / "collection_summary.csv"
        stale = time.time() - (100 * 3600)
        os.utime(old_summary, (stale, stale))
        os.utime(old_summary.parent, (stale, stale))

        with patch.object(recover, "RUNS_DIR", runs_dir):
            found = recover.find_recoverable_runs(72)

        assert found == []


def test_recovery_reports_nothing_to_do_when_there_is_nothing_recent(tmp_path) -> None:
    with patch.object(recover, "RUNS_DIR", tmp_path / "does_not_exist"), patch.object(
        recover, "DEFAULT_DATABASE_PATH", tmp_path / "db.sqlite"
    ):
        (tmp_path / "db.sqlite").touch()
        exit_code = _run_recover_main()

    assert exit_code == 0


def test_cancelling_a_run_still_saves_prices_already_collected(tmp_path, monkeypatch) -> None:
    """Cancelling used to discard everything: the cancellation handler returned
    before ever reaching the upload, so a run stopped after hundreds of real
    prices saved none of them."""
    import local_collector_agent as agent
    from local_collector import CollectionCancelled

    uploaded: list[str] = []
    reported: list[dict[str, object]] = []
    partial = tmp_path / "collection_summary.csv"
    partial.write_text(SUMMARY_CSV, encoding="utf-8")

    def fake_request_json(url, auth_header, *, method="GET", payload=None, allow_empty=False):
        if url.endswith("/cancelled"):
            return {"cancelled": False}
        if "/progress/" in url and payload:
            reported.append(dict(payload))
        return {}

    def cancelled_run(*args, **kwargs):
        raise CollectionCancelled("chaparral was cancelled.", partial)

    monkeypatch.setattr(agent, "_request_json", fake_request_json)
    monkeypatch.setattr(agent, "_download", lambda *a, **k: None)
    monkeypatch.setattr(agent, "saved_session_is_usable", lambda key: (True, "saved"))
    monkeypatch.setattr(agent, "verify_saved_session", lambda *a, **k: (True, "confirmed"))
    monkeypatch.setattr(agent, "first_probe_part", lambda path, key: None)
    monkeypatch.setattr(agent, "prepare_local_database", lambda *a, **k: 1)
    monkeypatch.setattr(agent, "_run_competitor", cancelled_run)
    monkeypatch.setattr(agent, "upload_with_retry", lambda url, path, auth: uploaded.append(str(path)))
    monkeypatch.setattr(agent, "BRIDGE_DIR", tmp_path)

    agent._run_job_body(
        {"job_id": "job-cancel", "competitors": ["chaparral"], "input_url": "/x", "planned_count": 100},
        {},
        "http://server",
        None,
        "agent-1",
    )

    assert uploaded == [str(partial)], "partial results must be uploaded, not discarded"
    cancelled_reports = [item for item in reported if item.get("status") == "cancelled"]
    assert cancelled_reports
    assert "were saved" in cancelled_reports[0]["message"]


def test_cancelling_before_anything_was_collected_uploads_nothing() -> None:
    """With no results file there is nothing to save, and that must not error."""
    from local_collector import CollectionCancelled

    exc = CollectionCancelled("cancelled")

    assert exc.summary is None


def test_rerunning_some_competitors_leaves_the_others_prices_alone(tmp_path) -> None:
    """Re-running only the competitors that failed must not disturb the ones
    that already completed. Without this guarantee, recovering a failed
    competitor would mean redoing every competitor, since a partial re-run
    could silently discard a full sweep that had already succeeded."""
    from app.collector_bridge import import_collection_summary
    from app.database import (
        connect_database,
        initialize_database,
        normalize_part_number,
        seed_chaparral,
        seed_motosport,
        seed_partzilla,
        seed_revzilla,
        utc_now,
    )

    database = tmp_path / "prices.db"
    initialize_database(database)
    now = utc_now()
    starting_prices = {"partzilla": 1000, "motosport": 2000, "chaparral": 3000, "revzilla": 4000}

    with connect_database(database) as conn:
        competitor_ids = {
            "partzilla": seed_partzilla(conn),
            "motosport": seed_motosport(conn),
            "chaparral": seed_chaparral(conn),
            "revzilla": seed_revzilla(conn),
        }
        conn.execute(
            "INSERT INTO products(product_id, manufacturer, oem_part_number, normalized_part_number, "
            "product_name, is_active, created_at, updated_at) VALUES (1,'Yamaha','1C3-1234',?, 'Part',1,?,?)",
            (normalize_part_number("1C3-1234"), now, now),
        )
        conn.execute(
            "INSERT INTO internal_product_state(product_id, internal_sku, our_current_price_cents, "
            "is_active, updated_at) VALUES (1,'SKU1',5000,1,?)",
            (now,),
        )
        for listing_id, (key, competitor_id) in enumerate(competitor_ids.items(), start=1):
            conn.execute(
                "INSERT INTO competitor_listings(listing_id, product_id, competitor_id, "
                "competitor_part_number, canonical_url, is_active, first_seen_at, last_seen_at, "
                "created_at, updated_at) VALUES (?,1,?,'1C3-1234','https://x',1,?,?,?,?)",
                (listing_id, competitor_id, now, now, now, now),
            )
            conn.execute(
                "INSERT INTO current_listing_state(listing_id, selling_price_cents, price_display_type, "
                "first_observed_at, last_successful_check_at, last_changed_at, updated_at) "
                "VALUES (?,?, 'regular', ?, ?, ?, ?)",
                (listing_id, starting_prices[key], now, now, now, now),
            )

    motosport_only = (
        "competitor,manufacturer,oem_part_number,observed_part_number,result_type,selling_price,"
        "reference_price,savings_percent,price_display_type,availability_raw,availability_status,"
        "page_classification,session_status,parse_confidence,warnings,observation_json_path\n"
        "motosport,Yamaha,1C3-1234,1C3-1234,price_change,77.77,,,regular,In Stock,in_stock,"
        "normal_product,public,high,,\n"
    )
    import_collection_summary(database, summary_csv=motosport_only.encode(), fallback_competitor="motosport")

    with connect_database(database) as conn:
        final = {
            row["competitor_code"]: row["selling_price_cents"]
            for row in conn.execute(
                "SELECT c.competitor_code, s.selling_price_cents FROM current_listing_state s "
                "JOIN competitor_listings l ON l.listing_id = s.listing_id "
                "JOIN competitors c ON c.competitor_id = l.competitor_id"
            )
        }

    assert final["motosport"] == 7777, "the competitor that was re-run should be updated"
    for untouched in ("partzilla", "chaparral", "revzilla"):
        assert final[untouched] == starting_prices[untouched], f"{untouched} was disturbed by a re-run"
