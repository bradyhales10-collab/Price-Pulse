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
