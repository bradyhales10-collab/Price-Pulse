"""Tests for the actual root cause behind two runs that crashed entirely.

Both crash files showed the identical traceback on two different competitors
at two different points in their runs: PermissionError [WinError 5] Access is
denied, from tmp.replace(path) inside the progress-writing code. That happens
when something else has the destination file open at that exact instant on
Windows - most likely the Browser Helper's own polling loop, reading the same
progress file roughly twice a second to forward it to the dashboard.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from app.atomic_write import replace_with_retry


def test_a_transient_windows_file_lock_is_retried_and_recovers() -> None:
    """The exact failure from both crash files: PermissionError on the second
    attempt, resolving itself immediately after, exactly like a fleeting lock
    held by a concurrent reader."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dest = tmp_path / "progress.json"
        dest.write_text("old", encoding="utf-8")
        src = tmp_path / "progress.tmp"
        src.write_text("new", encoding="utf-8")

        real_replace = Path.replace
        calls = {"count": 0}

        def flaky_replace(self, target):
            calls["count"] += 1
            if calls["count"] < 3:
                raise PermissionError("[WinError 5] Access is denied")
            return real_replace(self, target)

        with patch.object(Path, "replace", flaky_replace), patch("app.atomic_write.time.sleep", lambda seconds: None):
            replace_with_retry(src, dest)

        assert dest.read_text(encoding="utf-8") == "new"
        assert calls["count"] == 3


def test_a_permanently_locked_file_still_fails_cleanly_after_the_attempt_cap() -> None:
    """A genuine, sustained lock - not a fleeting one - must still raise
    rather than retry forever, so the existing crash-reporting safety net can
    catch it."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dest = tmp_path / "progress.json"
        dest.write_text("old", encoding="utf-8")
        src = tmp_path / "progress.tmp"
        src.write_text("new", encoding="utf-8")

        calls = {"count": 0}

        def always_fails(self, target):
            calls["count"] += 1
            raise PermissionError("[WinError 5] Access is denied")

        with patch.object(Path, "replace", always_fails), patch("app.atomic_write.time.sleep", lambda seconds: None):
            try:
                replace_with_retry(src, dest, attempts=4)
            except PermissionError:
                pass
            else:
                raise AssertionError("a permanently locked file should still raise")

        assert calls["count"] == 4


def test_a_normal_replace_with_no_lock_succeeds_immediately() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dest = tmp_path / "progress.json"
        dest.write_text("old", encoding="utf-8")
        src = tmp_path / "progress.tmp"
        src.write_text("new", encoding="utf-8")

        replace_with_retry(src, dest)

        assert dest.read_text(encoding="utf-8") == "new"
        assert not src.exists()


def test_only_permission_errors_are_retried() -> None:
    """A different kind of failure should not be silently retried and hidden;
    only the specific transient-lock error this exists for."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dest = tmp_path / "progress.json"
        src = tmp_path / "progress.tmp"
        src.write_text("new", encoding="utf-8")

        def raises_file_not_found(self, target):
            raise FileNotFoundError("gone")

        with patch.object(Path, "replace", raises_file_not_found):
            try:
                replace_with_retry(src, dest, attempts=3)
            except FileNotFoundError:
                pass
            else:
                raise AssertionError("a non-permission error should not be swallowed")


def test_collect_parts_and_the_dashboard_job_writer_both_use_the_shared_retry() -> None:
    """Both real crashes were the identical bug on two different write sites.
    Fixing only one would leave the other exposed to the same collision."""
    collect_parts_source = Path("collect_parts.py").read_text(encoding="utf-8")
    collection_jobs_source = Path("app/collection_jobs.py").read_text(encoding="utf-8")

    assert "replace_with_retry(tmp, path)" in collect_parts_source
    assert "replace_with_retry(temporary, path)" in collection_jobs_source
