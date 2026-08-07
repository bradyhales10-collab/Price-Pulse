from __future__ import annotations

from app.collection_jobs import _competitor_progress_summary


def test_login_required_gets_a_friendly_actionable_summary() -> None:
    progress = {
        "status": "login_required",
        "message": "Partzilla needs you to sign in again before prices can be checked.",
        "competitor_key": "partzilla",
        "rows": [],
        "completed": 0,
        "total": 25,
        "competitor": "partzilla",
    }

    summary = _competitor_progress_summary("partzilla", progress)

    assert summary["status"] == "login_required"
    assert summary["needs_login"] is True
    assert summary["needs_attention"] is True
    # The detail shown to the user should be the human-readable message,
    # not a generic "checked 0 of 25 parts" line or a raw file-path error.
    assert summary["detail"] == "Partzilla needs you to sign in again before prices can be checked."


def test_completed_competitor_is_not_flagged_for_login() -> None:
    progress = {
        "status": "completed",
        "rows": [],
        "completed": 10,
        "total": 10,
        "competitor": "chaparral",
    }

    summary = _competitor_progress_summary("chaparral", progress)

    assert summary["status"] == "completed"
    assert summary["needs_login"] is False
    assert summary["needs_attention"] is False


def test_local_job_can_report_login_required_status(tmp_path, monkeypatch) -> None:
    """A missing sign-in must survive as `login_required` and not be
    downgraded to a generic `failed`, which is what the user used to see."""
    import app.collection_jobs as jobs

    monkeypatch.setattr(jobs, "JOB_DIR", tmp_path)
    job_dir = tmp_path / "job-1"
    job_dir.mkdir()
    (job_dir / "job.json").write_text('{"job_id": "job-1", "status": "running"}', encoding="utf-8")

    result = jobs.complete_local_job(
        "job-1",
        status="login_required",
        message="Partzilla needs you to sign in.",
        agent_id="test-agent",
    )

    assert result["status"] == "login_required"
    assert "sign in" in str(result["message"]).lower()


def test_unknown_job_status_still_falls_back_to_failed(tmp_path, monkeypatch) -> None:
    import app.collection_jobs as jobs

    monkeypatch.setattr(jobs, "JOB_DIR", tmp_path)
    job_dir = tmp_path / "job-2"
    job_dir.mkdir()
    (job_dir / "job.json").write_text('{"job_id": "job-2", "status": "running"}', encoding="utf-8")

    result = jobs.complete_local_job(
        "job-2", status="not-a-real-status", message="x", agent_id="test-agent"
    )

    assert result["status"] == "failed"


# --- Request pacing for large runs -------------------------------------------


def test_delay_floor_scales_with_how_many_parts_a_run_covers() -> None:
    """Routine runs stay fast; the floor only climbs for genuinely large ones.
    Thresholds were raised after a 100-part run felt noticeably slow at a
    2 second floor that was protecting against a much bigger crawl."""
    from app.collection import minimum_delay_for_run

    assert minimum_delay_for_run(7) == 1
    assert minimum_delay_for_run(100) == 1
    assert minimum_delay_for_run(250) == 1
    assert minimum_delay_for_run(251) == 2
    assert minimum_delay_for_run(1000) == 2
    assert minimum_delay_for_run(1001) == 3
    assert minimum_delay_for_run(5000) == 3


def test_configured_delay_is_raised_but_never_lowered() -> None:
    from app.collection import effective_delay_seconds

    # Too small for the run size, so it is raised.
    assert effective_delay_seconds(1, 3000) == 3
    # Already generous, so it is left alone.
    assert effective_delay_seconds(10, 3000) == 10
    assert effective_delay_seconds(10, 5) == 10


def test_validate_delay_rejects_a_small_gap_for_a_large_run() -> None:
    from app.collection import validate_delay

    validate_delay(1, part_count=10)
    validate_delay(3, part_count=5000)
    try:
        validate_delay(1, part_count=5000)
    except ValueError as exc:
        assert "5000" in str(exc)
    else:
        raise AssertionError("a 1s gap should be refused for a 5000 part run")


def test_delay_is_jittered_so_the_timing_is_not_metronomic() -> None:
    """Perfectly even request timing is itself a sign of automation."""
    from app.collection import jittered_delay

    samples = [jittered_delay(4) for _ in range(200)]

    assert len({round(value, 3) for value in samples}) > 50, "delays should vary"
    assert all(3.0 <= value <= 5.0 for value in samples), "jitter should stay near the target"
    assert jittered_delay(0) == 0.0


def test_a_competitor_without_a_production_collector_cannot_run_a_price_check() -> None:
    """Unrecognised competitors used to fall through to the Partzilla collector,
    which builds partzilla.com URLs, so a run would have scraped the wrong site
    and stored it under the wrong competitor."""
    import collect_parts

    for key in ("partzilla", "motosport", "chaparral", "revzilla"):
        collect_parts.assert_production_collector_exists(key)

    for key in ("someone-new", "not-registered"):
        try:
            collect_parts.assert_production_collector_exists(key)
        except ValueError as exc:
            assert "production collector" in str(exc)
        else:
            raise AssertionError(f"{key} should be refused for a production run")


# --- Diagnostics -------------------------------------------------------------


def test_diagnostic_reports_a_stuck_price_check(tmp_path, monkeypatch) -> None:
    """A price check sat on 'waiting' with nothing on screen explaining that no
    Browser Helper was running. The check must name that case."""
    import json
    from datetime import UTC, datetime, timedelta

    import diagnose_part_pulse as diagnose

    job_dir = tmp_path / "jobs"
    job = job_dir / "job-stuck"
    job.mkdir(parents=True)
    stale = (datetime.now(UTC) - timedelta(minutes=7)).isoformat().replace("+00:00", "Z")
    (job / "job.json").write_text(
        json.dumps({"job_id": "job-stuck", "status": "queued_local", "updated_at": stale}),
        encoding="utf-8",
    )
    monkeypatch.setattr(diagnose, "JOB_DIR", job_dir)

    queued = [meta for _, meta in diagnose._queued_jobs() if meta.get("status") == "queued_local"]

    assert len(queued) == 1
    age = diagnose._age_seconds(queued[0]["updated_at"])
    assert age is not None and age > diagnose.STUCK_AFTER_SECONDS


def test_diagnostic_handles_a_missing_or_unreadable_timestamp() -> None:
    import diagnose_part_pulse as diagnose

    assert diagnose._age_seconds("") is None
    assert diagnose._age_seconds("not-a-date") is None
    assert diagnose._age_seconds(None) is None


def test_diagnostic_runs_without_crashing_on_a_bare_machine() -> None:
    """It has to work when nothing is set up, since that is when it is needed."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "diagnose_part_pulse.py"], capture_output=True, text=True, timeout=60
    )

    assert "Part Pulse Check" in result.stdout
    # Exit code 1 simply means it found something to fix.
    assert result.returncode in {0, 1}


def test_repair_restarts_part_pulse_rather_than_leaving_it_stopped() -> None:
    """Repair stops every Part Pulse process. If it does not start them again,
    price checks queue with nothing to run them."""
    from pathlib import Path

    repair = Path("Repair Part Pulse.cmd").read_text(encoding="utf-8")

    assert "taskkill" in repair
    assert "Start Part Pulse.cmd" in repair
    assert "start " in repair


def test_diagnostic_reports_when_a_competitor_collector_cannot_find_what_it_calls(
    tmp_path, monkeypatch
) -> None:
    """Reproduces the real bug reported: a collector's code referencing a name
    that turned out not to exist at runtime, surfacing as a NameError only
    once a live price check reached that competitor. This must be caught by
    inspection, without needing a live browser."""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent
    broken_copy = tmp_path / "broken_pp"
    shutil.copytree(
        project_root,
        broken_copy,
        ignore=shutil.ignore_patterns(".venv", ".git", ".pytest_cache", "__pycache__", "data"),
    )
    source = (broken_copy / "collect_parts.py").read_text(encoding="utf-8")
    assert "def collect_one_search_based_part(" in source
    broken = source.replace(
        "def collect_one_search_based_part(", "def RENAMED_collect_one_search_based_part("
    )
    (broken_copy / "collect_parts.py").write_text(broken, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "diagnose_part_pulse.py"],
        capture_output=True,
        text=True,
        cwd=broken_copy,
        timeout=30,
    )

    assert "Some competitors are not wired up correctly" in result.stdout
    assert "revzilla" in result.stdout
    assert "collect_one_search_based_part" in result.stdout


def test_repair_clears_stale_python_bytecode_cache() -> None:
    """A stale __pycache__ was the leading suspect for code that had already
    been fixed on GitHub still misbehaving on the machine that pulled it."""
    from pathlib import Path

    repair = Path("Repair Part Pulse.cmd").read_text(encoding="utf-8")

    assert "__pycache__" in repair
    assert "rmdir" in repair


def test_diagnostic_detects_both_part_pulse_windows_running(monkeypatch) -> None:
    """Confirms the healthy case is recognized using the window-title based
    check, which replaced a WMI CommandLine check found to silently return
    blank without administrator rights on at least one real machine - making
    a genuinely stale process invisible to the exact check meant to find it."""
    import diagnose_part_pulse as diagnose

    monkeypatch.setattr(
        diagnose,
        "_running_part_pulse_processes",
        lambda: [
            {"title": "Part Pulse Browser Helper", "image": "python.exe", "pid": "111"},
            {"title": "Part Pulse Dashboard", "image": "python.exe", "pid": "222"},
        ],
    )

    processes = diagnose._running_part_pulse_processes()

    assert any(p["title"] == "Part Pulse Browser Helper" for p in processes)
    assert any(p["title"] == "Part Pulse Dashboard" for p in processes)


def test_diagnostic_detects_only_one_of_the_two_windows_running(monkeypatch) -> None:
    import diagnose_part_pulse as diagnose

    monkeypatch.setattr(
        diagnose,
        "_running_part_pulse_processes",
        lambda: [{"title": "Part Pulse Dashboard", "image": "python.exe", "pid": "222"}],
    )

    processes = diagnose._running_part_pulse_processes()

    assert not any(p["title"] == "Part Pulse Browser Helper" for p in processes)
    assert any(p["title"] == "Part Pulse Dashboard" for p in processes)


def test_process_check_is_skipped_gracefully_on_non_windows() -> None:
    import diagnose_part_pulse as diagnose

    if diagnose.os.name != "nt":
        assert diagnose._running_part_pulse_processes() == []


def test_the_launcher_scripts_kill_by_window_title_not_by_reading_command_lines() -> None:
    """A Get-CimInstance CommandLine check came back blank for a real,
    three-day-old stray process on a locked-down machine, meaning the old
    kill logic in these scripts had likely been silently matching nothing on
    that machine the entire time it was used. taskkill's window-title filter
    does not depend on that same permission."""
    from pathlib import Path

    for filename in ("Start Part Pulse.cmd", "Repair Part Pulse.cmd", "Stop Part Pulse.cmd"):
        source = Path(filename).read_text(encoding="utf-8")
        assert "Get-CimInstance" not in source, filename
        assert 'taskkill /F /FI "WINDOWTITLE eq Part Pulse Browser Helper*"' in source, filename
        assert 'taskkill /F /FI "WINDOWTITLE eq Part Pulse Collector*"' in source, filename
        assert 'taskkill /F /FI "WINDOWTITLE eq Part Pulse Dashboard*"' in source, filename


def test_run_summary_separates_real_problems_from_expected_outcomes(tmp_path, monkeypatch) -> None:
    """"Local collection failed for: chaparral, partzilla" does not say what went
    wrong or how many parts were affected. A brand a competitor simply does not
    carry is a normal outcome and must not be presented as a fault, or the real
    problems get lost in the noise."""
    import json

    import explain_last_run as explain

    monkeypatch.setattr(explain, "BRIDGE_DIR", tmp_path)
    monkeypatch.setattr(explain, "CRASH_DIR", tmp_path / "crashes")

    rows = (
        [{"oem_part_number": "A", "manufacturer": "Polaris", "result_type": "selling_price_found",
          "selling_price": "1.00", "status_reason": "", "warnings": ""}]
        + [{"oem_part_number": "B", "manufacturer": "CF Moto", "result_type": "manufacturer_not_carried",
            "selling_price": None, "status_reason": "", "warnings": ""}]
        + [{"oem_part_number": "C", "manufacturer": "Kawasaki", "result_type": "lookup_failed",
            "selling_price": None, "status_reason": "no exact match", "warnings": ""}]
    )
    (tmp_path / "progress-1-chaparral.json").write_text(
        json.dumps({"status": "failed", "completed": 3, "total": 3, "rows": rows}), encoding="utf-8"
    )

    files = explain.latest_progress_files()

    assert list(files) == ["chaparral"]
    assert "manufacturer_not_carried" in explain.EXPECTED_RESULTS
    assert "lookup_failed" not in explain.EXPECTED_RESULTS


def test_run_summary_picks_the_newest_file_per_competitor(tmp_path, monkeypatch) -> None:
    import json
    import os
    import time

    import explain_last_run as explain

    monkeypatch.setattr(explain, "BRIDGE_DIR", tmp_path)

    old = tmp_path / "progress-1-partzilla.json"
    new = tmp_path / "progress-2-partzilla.json"
    for path in (old, new):
        path.write_text(json.dumps({"status": "completed", "rows": []}), encoding="utf-8")
    past = time.time() - 3600
    os.utime(old, (past, past))

    files = explain.latest_progress_files()

    assert files["partzilla"].name == new.name


def test_run_summary_handles_no_results_without_crashing(tmp_path, monkeypatch) -> None:
    import explain_last_run as explain

    monkeypatch.setattr(explain, "BRIDGE_DIR", tmp_path)
    monkeypatch.setattr(explain, "CRASH_DIR", tmp_path / "none")

    assert explain.main() == 1


def test_consecutive_error_limit_scales_so_two_bad_pages_cannot_end_a_large_run() -> None:
    """A 1000-part MotoSport run stopped after 11 parts: one page took longer
    than five seconds to read and the next came back blocked, which hit a flat
    limit of two consecutive errors. Ten successful lookups were discarded
    along with the 989 not yet attempted. Two unlucky pages in a row is
    ordinary noise at volume; ten in a row is a site that has stopped
    answering."""
    from app.collection import consecutive_error_limit

    assert consecutive_error_limit(25) == 3
    assert consecutive_error_limit(250) == 3
    assert consecutive_error_limit(500) == 4
    assert consecutive_error_limit(1000) == 6
    assert consecutive_error_limit(5000) == 6

    # Kept modest on purpose: each error can cost several seconds of waiting,
    # so a generous limit turns a bad patch into a long stall rather than a
    # quick stop. An earlier value of 10, paired with a 15 second read timeout,
    # made a large run feel unusable.
    assert consecutive_error_limit(1000) * 8 <= 60

    # Never below the old behaviour: a small run still stops promptly.
    assert consecutive_error_limit(1) >= 2


def test_a_genuine_block_still_stops_a_run_immediately() -> None:
    """The scaled limit governs transient navigation failures only. A site
    actively refusing us must still stop the run at once rather than being
    retried nine more times."""
    from app.collection import CollectionRow, stop_status_for

    def row(result_type: str, http_status: int = 403) -> CollectionRow:
        return CollectionRow(
            run_order=1, scan_run_id=1, scan_event_id=None, manufacturer="Yamaha",
            oem_part_number="X", normalized_manufacturer="Yamaha", competitor="motosport",
            manufacturer_supported=True, lookup_status="", status_reason="",
            observed_part_number="X", product_name="", checked_at="", http_status=http_status,
            page_classification=result_type, session_status="public", selling_price=None,
            reference_price=None, savings_percent=None, price_display_type="unknown",
            previous_selling_price=None, result_type=result_type, price_changed=False,
            availability_raw="", previous_availability_status=None, availability_status="unknown",
            supersession_detected=False, superseded_by_raw=None, price_source_category="",
            price_corroboration_count=0, price_parse_confidence="low", parse_confidence="low",
            warning_count=0, warnings="", observation_json_path="",
        )

    assert stop_status_for(row("blocked")) is not None
    assert stop_status_for(row("challenge")) is not None
    # A transient navigation error is not itself a stop; it only counts toward
    # the consecutive limit.
    assert stop_status_for(row("navigation_error", http_status=200)) is None


def test_page_text_timeout_is_generous_enough_but_not_a_stall() -> None:
    """5 seconds was too tight and counted a settling page as an operational
    error. 15 seconds was too patient: every slow page stalled three times as
    long, and with the run no longer stopping after two errors those stalls
    repeated instead of ending it."""
    from pathlib import Path

    source = Path("collect_parts.py").read_text(encoding="utf-8")

    assert "BODY_TEXT_TIMEOUT_MS = 8000" in source
    assert "inner_text(timeout=5000)" not in source


def test_the_file_lock_retry_stays_cheap() -> None:
    """This runs on every progress write during a run and every job status
    write the dashboard makes, so a generous retry would add up across
    thousands of writes and make the application feel sluggish."""
    import inspect

    from app.atomic_write import replace_with_retry

    parameters = inspect.signature(replace_with_retry).parameters
    worst_case = parameters["attempts"].default * parameters["delay_seconds"].default

    assert worst_case <= 0.2, f"worst case {worst_case}s per write is too slow"
