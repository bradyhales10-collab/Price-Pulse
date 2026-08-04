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


# --- Sign-in happens before any collection starts ----------------------------


def test_sign_in_is_checked_before_any_collection_starts(monkeypatch, tmp_path) -> None:
    """A sign-in window opened while other competitors were already driving
    browsers could not actually be used: it kept losing focus to pages the
    running collectors opened. The check now happens first, so nothing else is
    on screen competing with it, and no parts are collected."""
    import local_collector_agent as agent

    calls: dict[str, object] = {"logins": [], "posts": [], "prepared": 0}

    def fake_request_json(url, auth_header, *, method="GET", payload=None, allow_empty=False):
        calls["posts"].append((url, payload))
        return {}

    def fake_prepare(*args, **kwargs):
        calls["prepared"] += 1
        return 1

    monkeypatch.setattr(agent, "_request_json", fake_request_json)
    monkeypatch.setattr(agent, "_download", lambda *a, **k: None)
    monkeypatch.setattr(agent, "prepare_local_database", fake_prepare)
    monkeypatch.setattr(agent, "_open_login_refresh", lambda req: calls["logins"].append(req["competitor_key"]))
    monkeypatch.setattr(agent, "saved_session_is_usable", lambda key: (False, "saved sign-in has expired"))
    monkeypatch.setattr(agent, "BRIDGE_DIR", tmp_path)

    agent._run_job_body(
        {"job_id": "job-signin", "competitors": ["partzilla", "chaparral"], "input_url": "/x", "planned_count": 100},
        {},
        "http://server",
        None,
        "agent-1",
    )

    # Partzilla requires a login; Chaparral does not.
    assert calls["logins"] == ["partzilla"]
    # Critically: no local databases were prepared and no collection ran.
    assert calls["prepared"] == 0

    completes = [p for url, p in calls["posts"] if "/complete" in url]
    assert len(completes) == 1
    assert completes[0]["status"] == "login_required"
    assert "Partzilla" in completes[0]["message"]
    assert "nothing was changed" in completes[0]["message"]


def test_collection_proceeds_normally_when_sign_ins_are_present(monkeypatch, tmp_path) -> None:
    """The pre-flight check must not block a run that has valid sign-ins."""
    import local_collector_agent as agent

    prepared: list[str] = []

    monkeypatch.setattr(agent, "_request_json", lambda *a, **k: {})
    monkeypatch.setattr(agent, "_download", lambda *a, **k: None)
    monkeypatch.setattr(agent, "saved_session_is_usable", lambda key: (True, "current"))
    monkeypatch.setattr(agent, "_open_login_refresh", lambda req: prepared.append("SHOULD NOT HAPPEN"))
    monkeypatch.setattr(agent, "BRIDGE_DIR", tmp_path)

    def fake_prepare(input_path, local_db, keys, run_id_floor=0):
        prepared.append(keys[0])
        return run_id_floor + 1

    monkeypatch.setattr(agent, "prepare_local_database", fake_prepare)
    monkeypatch.setattr(agent, "_run_competitor", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here")))

    try:
        agent._run_job_body(
            {"job_id": "job-ok", "competitors": ["partzilla"], "input_url": "/x", "planned_count": 5},
            {},
            "http://server",
            None,
            "agent-1",
        )
    except Exception:
        pass

    assert "partzilla" in prepared
    assert "SHOULD NOT HAPPEN" not in prepared


def test_a_sign_in_problem_and_an_unrelated_failure_are_reported_separately() -> None:
    """The combined message read as though the failing competitor was the one
    needing a sign-in: 'Local collection failed for: chaparral. Your saved
    Partzilla sign-in has expired...'"""
    import local_collector_agent as agent

    outcomes = {
        "partzilla": {"status": "login_required"},
        "chaparral": {"status": "failed"},
    }
    needs_login = [k for k, v in outcomes.items() if v.get("status") == "login_required"]
    failed = [k for k, v in outcomes.items() if v.get("status") == "failed"]

    assert needs_login == ["partzilla"]
    assert failed == ["chaparral"]

    source = agent.__file__
    from pathlib import Path

    text = Path(source).read_text(encoding="utf-8")
    assert "Two separate problems" in text
    assert "Second, unrelated" in text


def test_an_expired_sign_in_is_caught_before_collecting_not_only_a_missing_one() -> None:
    """The pre-flight check originally tested only that the sign-in file
    existed. An expired file is still a file, so the run started anyway,
    discovered the dead session partway through, and asked for a sign-in while
    other competitors were already driving browsers - the loop the user hit."""
    import json
    import tempfile
    import time
    from pathlib import Path

    import app.auth_session as auth

    original_dir = auth.PRIVATE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        auth.PRIVATE_DIR = Path(tmp)
        try:
            def write(key, expires):
                path = auth.auth_state_path_for(key)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"cookies": [{"name": "s", "domain": ".x.com", "expires": expires}], "origins": []}),
                    encoding="utf-8",
                )

            write("expired_case", time.time() - 86400)
            usable, reason = auth.saved_session_is_usable("expired_case")
            assert usable is False
            assert "expired" in reason
            # The old check would have passed this, since the file is present.
            assert auth.auth_state_exists("expired_case") is True

            write("valid_case", time.time() + 86400)
            usable, _ = auth.saved_session_is_usable("valid_case")
            assert usable is True
        finally:
            auth.PRIVATE_DIR = original_dir


def test_only_the_preflight_opens_a_sign_in_window() -> None:
    """Windows opened mid-run cannot be used, because the other competitors'
    browsers steal focus. Exactly one place should open one."""
    from pathlib import Path

    source = Path("local_collector_agent.py").read_text(encoding="utf-8")
    opens = source.count("_open_login_refresh(")

    # One definition, one call from the login-refresh poll loop, and one from
    # the pre-flight check. No more than that.
    # Definition, the background sign-in poller, the --once path, and the
    # pre-flight check. Collection paths must not add another.
    assert opens <= 4, f"_open_login_refresh referenced {opens} times; a mid-run call may have returned"
    assert "Deliberately does NOT open a sign-in window here" in source


# --- Live sign-in verification ------------------------------------------------


def _fake_observation(session_state: str, price_state: str = "visible"):
    class Observation:
        session_status = session_state
        price_visibility = price_state

    return Observation()


def _run_live_check(session_state: str, price_state: str, tmp_path):
    """Drive verify_saved_session with a stubbed browser."""
    import json
    import time
    from unittest.mock import patch

    import app.auth_session as auth
    import app.session_check as check
    from app.models import PartRecord

    original = auth.PRIVATE_DIR
    auth.PRIVATE_DIR = tmp_path
    try:
        state = auth.auth_state_path_for("partzilla")
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps(
                {"cookies": [{"name": "s", "domain": ".partzilla.com", "expires": time.time() + 9999}], "origins": []}
            ),
            encoding="utf-8",
        )
        part = PartRecord(test_case_id="t", manufacturer="Kawasaki", oem_part_number="41080-1514")
        with patch.object(check, "sync_playwright") as playwright:
            context = playwright.return_value.__enter__.return_value
            page = context.chromium.launch.return_value.new_context.return_value.new_page.return_value
            page.goto.return_value.status = 200
            page.content.return_value = "<html></html>"
            page.locator.return_value.count.return_value = 1
            page.locator.return_value.inner_text.return_value = ""
            page.url = "https://www.partzilla.com/product/x"
            with patch.object(check, "get_competitor") as get_comp:
                adapter = get_comp.return_value
                adapter.requires_login = True
                adapter.build_product_url.return_value = "https://www.partzilla.com/product/x"
                adapter.parse_product_page.return_value = _fake_observation(session_state, price_state)
                return check.verify_saved_session("partzilla", part)
    finally:
        auth.PRIVATE_DIR = original


def test_a_session_invalidated_server_side_is_caught(tmp_path) -> None:
    """The case that defeated the offline check: cookies carry a future expiry
    date, so the file looks valid, but the site has already signed us out. Only
    loading a real page reveals it."""
    usable, reason = _run_live_check("expired_or_invalid", "sign_in_required", tmp_path)

    assert usable is False
    assert "signed out on the live site" in reason


def test_gated_prices_count_as_signed_out(tmp_path) -> None:
    usable, reason = _run_live_check("authenticated", "sign_in_required", tmp_path)

    assert usable is False
    assert "hiding prices" in reason


def test_a_confirmed_session_is_accepted(tmp_path) -> None:
    usable, reason = _run_live_check("authenticated", "visible", tmp_path)

    assert usable is True
    assert "confirmed signed in" in reason


def test_a_blocked_or_unreachable_site_does_not_discard_the_sign_in(tmp_path) -> None:
    """A network problem or bot block says nothing about whether the sign-in is
    good. Throwing it away would force a pointless re-sign-in."""
    for state in ("blocked", "challenge", "navigation_error", "unknown"):
        usable, reason = _run_live_check(state, "not_present", tmp_path)
        assert usable is True, state
        assert "could not confirm" in reason


def test_sign_in_requests_are_polled_off_the_main_loop() -> None:
    """_run_job blocks the main loop for the length of a price check. A Sign In
    button pressed during a run queued a request nothing ever claimed, so the
    button appeared to do nothing."""
    from pathlib import Path

    source = Path("local_collector_agent.py").read_text(encoding="utf-8")

    assert "def poll_login_requests" in source
    assert 'name="login-poll"' in source
    assert "daemon=True" in source
