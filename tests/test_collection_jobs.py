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
        ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", "data"),
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
        assert 'taskkill /F /FI "WINDOWTITLE eq Part Pulse Dashboard*"' in source, filename
