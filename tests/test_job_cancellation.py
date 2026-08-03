from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.web.app import create_app

sys.path.insert(0, str(Path(__file__).parent))

from test_import_comparison_collection import _comparison_db  # noqa: E402


def _job_dir(tmp_path: Path, monkeypatch, job_id: str, status: str = "running") -> Path:
    import app.collection_jobs as cj

    job_dir = tmp_path / "jobs"
    job = job_dir / job_id
    job.mkdir(parents=True)
    (job / "job.json").write_text(
        json.dumps({"job_id": job_id, "status": status, "competitors": ["partzilla"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cj, "JOB_DIR", job_dir)
    return job


def test_requesting_cancellation_flags_the_job_without_finalizing_it(tmp_path, monkeypatch) -> None:
    """The agent gets a chance to acknowledge and stop cleanly first."""
    import app.collection_jobs as cj

    job = _job_dir(tmp_path, monkeypatch, "job-1")

    result = cj.request_job_cancellation("job-1")

    assert result["cancel_requested"] is True
    assert result["status"] == "running"
    assert cj.is_job_cancelled("job-1") is True

    on_disk = json.loads((job / "job.json").read_text(encoding="utf-8"))
    assert on_disk["cancel_requested"] is True


def test_cancelling_a_job_that_is_not_active_does_nothing(tmp_path, monkeypatch) -> None:
    import app.collection_jobs as cj

    _job_dir(tmp_path, monkeypatch, "job-2", status="completed")

    result = cj.request_job_cancellation("job-2")

    assert "cancel_requested" not in result
    assert cj.is_job_cancelled("job-2") is False


def test_cancelling_an_unknown_job_reports_not_found(tmp_path, monkeypatch) -> None:
    import app.collection_jobs as cj

    monkeypatch.setattr(cj, "JOB_DIR", tmp_path / "jobs")

    result = cj.request_job_cancellation("does-not-exist")

    assert result["status"] == "not_found"


def test_a_cancellation_finalizes_itself_even_if_nothing_ever_acknowledges_it(
    tmp_path, monkeypatch
) -> None:
    """The agent might be hung or closed. The person must still get unstuck
    without depending on it to cooperate."""
    import app.collection_jobs as cj

    job = _job_dir(tmp_path, monkeypatch, "job-3")
    cj.request_job_cancellation("job-3")
    meta = json.loads((job / "job.json").read_text(encoding="utf-8"))
    meta["cancel_requested_at"] = "2000-01-01T00:00:00Z"
    (job / "job.json").write_text(json.dumps(meta), encoding="utf-8")

    status = cj.job_status("job-3")

    assert status["status"] == "cancelled"


def test_a_cancellation_within_the_grace_period_is_not_finalized_yet(tmp_path, monkeypatch) -> None:
    """An agent that is actually still working should get its window to finish
    cleanly before this force-finalizes the job."""
    import app.collection_jobs as cj

    _job_dir(tmp_path, monkeypatch, "job-4")
    cj.request_job_cancellation("job-4")

    status = cj.job_status("job-4")

    assert status["status"] == "running"
    assert status["cancel_requested"] is True


def test_restarting_part_pulse_clears_every_stuck_job_immediately(tmp_path, monkeypatch) -> None:
    """No Browser Helper is running at restart, so any active job at that
    moment is guaranteed stale and can be finalized right away."""
    import app.collection_jobs as cj

    _job_dir(tmp_path, monkeypatch, "job-5", status="running")
    _job_dir(tmp_path, monkeypatch, "job-6", status="queued_local")
    _job_dir(tmp_path, monkeypatch, "job-7", status="completed")

    cleared = cj.cancel_all_active_jobs()

    assert cleared == 2
    assert cj.job_status("job-5")["status"] == "cancelled"
    assert cj.job_status("job-6")["status"] == "cancelled"
    assert cj.job_status("job-7")["status"] == "completed"


def test_a_cancelled_top_level_job_wins_even_if_a_competitor_never_reported_back(
    tmp_path, monkeypatch
) -> None:
    """This is the exact bug that would make Cancel look broken: an
    unresponsive competitor's stale progress file said 'running' forever,
    which used to override the job's own cancelled status."""
    import app.collection_jobs as cj

    job = _job_dir(tmp_path, monkeypatch, "job-8")
    (job / "progress-partzilla.json").write_text(
        json.dumps({"status": "running", "total": 100, "completed": 3}), encoding="utf-8"
    )
    meta = json.loads((job / "job.json").read_text(encoding="utf-8"))
    meta["progress_files"] = {"partzilla": str(job / "progress-partzilla.json")}
    meta["status"] = "cancelled"
    (job / "job.json").write_text(json.dumps(meta), encoding="utf-8")

    status = cj.job_status("job-8")

    assert status["progress"]["status"] == "cancelled"


def test_cancel_route_redirects_and_the_agent_endpoint_reports_it(tmp_path, monkeypatch) -> None:

    _job_dir(tmp_path, monkeypatch, "job-9")
    client = TestClient(create_app(_comparison_db("cancel_route.db")), raise_server_exceptions=False)

    assert client.get("/collector/agent/jobs/job-9/cancelled").json() == {"cancelled": False}

    response = client.post("/imports/jobs/job-9/cancel")

    assert response.status_code == 200  # followed the redirect
    assert "job_id=job-9" in str(response.url)
    assert client.get("/collector/agent/jobs/job-9/cancelled").json() == {"cancelled": True}


def test_cancel_button_appears_only_while_active_and_disappears_once_requested(
    tmp_path, monkeypatch
) -> None:
    _job_dir(tmp_path, monkeypatch, "job-10")
    client = TestClient(create_app(_comparison_db("cancel_button.db")), raise_server_exceptions=False)

    before = client.get("/imports?job_id=job-10").text
    assert 'action="/imports/jobs/job-10/cancel"' in before
    assert "Cancelling..." not in before

    client.post("/imports/jobs/job-10/cancel")
    after = client.get("/imports?job_id=job-10").text

    assert 'action="/imports/jobs/job-10/cancel"' not in after
    assert "Cancelling..." in after


def test_clear_stuck_jobs_script_clears_active_jobs(tmp_path, monkeypatch) -> None:
    import app.collection_jobs as cj
    import clear_stuck_jobs

    _job_dir(tmp_path, monkeypatch, "job-11", status="running")

    exit_code = clear_stuck_jobs.main()

    assert exit_code == 0
    assert cj.job_status("job-11")["status"] == "cancelled"


def test_run_competitor_raises_collection_cancelled_when_asked_to_stop() -> None:
    """The subprocess wait loop must actually terminate the process and raise,
    rather than silently continue or fail with an unrelated error."""
    import argparse
    import subprocess

    import local_collector

    class FakeProcess:
        def __init__(self):
            self.terminated = False
            self._polls = 0

        def poll(self):
            self._polls += 1
            return None if self._polls < 100 else 0

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    fake = FakeProcess()

    called = {"count": 0}

    def cancel_after_first_check():
        called["count"] += 1
        return True


    def fake_popen(command, cwd=None):
        return fake

    original_popen = subprocess.Popen
    original_sleep = local_collector.time.sleep
    local_collector.subprocess.Popen = fake_popen
    local_collector.time.sleep = lambda seconds: None
    try:
        try:
            local_collector._run_competitor(
                Path("in.csv"),
                Path("db.sqlite"),
                5,
                "partzilla",
                argparse.Namespace(collection_mode="full_browser", delay_seconds=1, headless=True),
                expected_run_id=1,
                should_cancel=cancel_after_first_check,
            )
        except local_collector.CollectionCancelled as exc:
            assert "partzilla" in str(exc)
        else:
            raise AssertionError("expected CollectionCancelled to be raised")
    finally:
        local_collector.subprocess.Popen = original_popen
        local_collector.time.sleep = original_sleep

    assert fake.terminated is True
    assert called["count"] >= 1


def test_seed_competitor_supports_every_registered_competitor(tmp_path) -> None:
    """The dispatcher used by the local collector's per-competitor database
    setup had not been updated for RevZilla. Selecting it in a real price
    check raised ValueError before any browser opened, on the very first
    setup step, with nothing on screen to explain why the job never moved."""
    from app.competitors.registry import list_competitors
    from app.database import connect_database, initialize_database, seed_competitor

    database = tmp_path / "seed.db"
    initialize_database(database)

    with connect_database(database) as conn:
        for adapter in list_competitors():
            competitor_id = seed_competitor(conn, adapter.competitor_key)
            assert isinstance(competitor_id, int)


def test_seed_competitor_still_rejects_a_truly_unknown_key(tmp_path) -> None:
    from app.database import connect_database, initialize_database, seed_competitor

    database = tmp_path / "seed2.db"
    initialize_database(database)

    with connect_database(database) as conn:
        try:
            seed_competitor(conn, "not-a-real-competitor")
        except ValueError as exc:
            assert "not-a-real-competitor" in str(exc)
        else:
            raise AssertionError("an unregistered competitor key should be rejected")


def test_local_database_prep_succeeds_for_every_competitor_with_a_real_file(tmp_path) -> None:
    """Reproduces the actual failure: a 100-part file, one local database
    built per competitor, RevZilla included."""
    import csv

    from local_collector import prepare_local_database

    input_csv = tmp_path / "parts.csv"
    columns = [
        "Test_Case_ID", "Manufacturer", "OEM_Part_Number", "Search_Observed_Product_Name",
        "Search_Observed_MSRP", "Expected_Partzilla_URL", "Test_Purpose", "Verified_Date", "Source_URL",
    ]
    makers = ["Polaris", "Yamaha", "Kawasaki", "Honda"]
    with input_csv.open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        for index in range(100):
            writer.writerow([f"T{index}", makers[index % 4], f"PART-{index:04d}", "", "", "", "", "", ""])

    for competitor in ("partzilla", "motosport", "chaparral", "revzilla"):
        local_db = tmp_path / f"collector-{competitor}.db"
        prepare_local_database(input_csv, local_db, [competitor], run_id_floor=1000)


def test_a_failure_before_any_competitor_starts_is_reported_not_silently_dropped(monkeypatch) -> None:
    """This is the actual bug: the per-competitor setup loop crashing used to
    propagate up, get caught by the top-level poll-loop handler, and just get
    logged. The job stayed 'running' forever with no explanation on screen.
    A setup failure must now be reported back as a failed job."""
    import local_collector_agent

    reported: dict[str, object] = {}

    def fake_request_json(url, auth_header, *, method="GET", payload=None, allow_empty=False):
        if url.endswith("/complete?agent_id=test-agent"):
            reported["status"] = payload.get("status")
            reported["message"] = payload.get("message")
        return {}

    def broken_run_job_body(job, config, server_url, auth_header, agent_id):
        raise ValueError("Unknown competitor: revzilla")

    monkeypatch.setattr(local_collector_agent, "_request_json", fake_request_json)
    monkeypatch.setattr(local_collector_agent, "_run_job_body", broken_run_job_body)

    local_collector_agent._run_job(
        {"job_id": "job-broken", "competitors": ["revzilla"]},
        {},
        "http://server",
        None,
        "test-agent",
    )

    assert reported["status"] == "failed"
    assert "revzilla" in reported["message"]
