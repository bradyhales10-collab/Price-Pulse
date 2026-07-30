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
