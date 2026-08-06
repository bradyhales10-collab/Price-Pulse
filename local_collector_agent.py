from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

from app.auth_session import delete_auth_state, saved_session_is_usable
from app.competitors.registry import get_competitor, login_page_url
from app.local_agent_credentials import unprotect_password
from app.session_check import first_probe_part, verify_saved_session
from local_collector import (
    BRIDGE_DIR,
    CollectionCancelled,
    _auth_header,
    _download,
    _run_competitor,
    already_attempted_part_keys,
    prepare_local_database,
    upload_with_retry,
)
from setup_local_collector_agent import normalize_server_url

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "data" / "private" / "local_collector_agent.json"
LOGGER = logging.getLogger("part-pulse-local-agent")
SINGLE_INSTANCE_PORT = 47653
LOGIN_WAIT_SECONDS = 900
LOGIN_HELPER_COOLDOWN_SECONDS = 10
_LOGIN_HELPERS: dict[str, subprocess.Popen] = {}
_LOGIN_HELPERS_LOCK = threading.Lock()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for Part Pulse price-check jobs and run them on this computer.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="Check for one job and then exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = _load_config(args.config)
    _configure_logging()
    instance_lock = _acquire_instance_lock()
    if instance_lock is None:
        LOGGER.info("Another Part Pulse collector is already running; this copy will exit.")
        return 0
    server_url = normalize_server_url(str(config["server_url"]))
    username = str(config.get("username") or "")
    password = ""
    if username and config.get("protected_password"):
        try:
            password = unprotect_password(str(config["protected_password"]))
        except Exception as exc:
            LOGGER.warning("Saved Desktop Collector password could not be opened. Continuing without web login: %s", exc)
            username = ""
    auth_header = _auth_header(username, password)
    agent_id = str(config.get("agent_id") or f"{socket.gethostname()}-collector")
    poll_seconds = max(2, int(config.get("poll_seconds") or 3))
    LOGGER.info("Part Pulse collector started as %s", agent_id)

    # Sign-in requests are polled on their own thread. _run_job blocks the main
    # loop for the whole length of a price check, so a Sign In button pressed
    # during a run used to queue a request that nothing ever picked up, and the
    # button appeared to do nothing at all.
    stop_login_poll = threading.Event()

    def poll_login_requests() -> None:
        while not stop_login_poll.wait(poll_seconds):
            try:
                request = _request_json(
                    f"{server_url}/collector/agent/login/next?{urllib.parse.urlencode({'agent_id': agent_id})}",
                    auth_header,
                    method="POST",
                    allow_empty=True,
                )
            except Exception:
                continue
            if request:
                try:
                    _open_login_refresh(request)
                except Exception:
                    LOGGER.exception("Could not open the sign-in window")

    if not args.once:
        threading.Thread(target=poll_login_requests, name="login-poll", daemon=True).start()

    while True:
        try:
            if args.once:
                login_request = _request_json(
                    f"{server_url}/collector/agent/login/next?{urllib.parse.urlencode({'agent_id': agent_id})}",
                    auth_header,
                    method="POST",
                    allow_empty=True,
                )
                if login_request:
                    _open_login_refresh(login_request)
                    return 0
            job = _request_json(
                f"{server_url}/collector/agent/jobs/next?{urllib.parse.urlencode({'agent_id': agent_id})}",
                auth_header,
                method="POST",
                allow_empty=True,
            )
            if job:
                _run_job(job, config, server_url, auth_header, agent_id)
                if args.once:
                    return 0
            elif args.once:
                return 0
        except Exception:
            LOGGER.exception("Collector could not contact Part Pulse or finish the current job")
            if args.once:
                return 1
        time.sleep(poll_seconds)


def _acquire_instance_lock(port: int = SINGLE_INSTANCE_PORT) -> socket.socket | None:
    """Keep one collector process active on this computer."""
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", port))
        lock.listen(1)
    except OSError:
        lock.close()
        return None
    return lock


def _open_login_refresh(request: dict[str, object]) -> subprocess.Popen | None:
    competitor = str(request.get("competitor_key") or "").strip().lower()
    adapter = get_competitor(competitor)
    # The sign-in page, not a product page. A product page redirects a
    # signed-out visitor and loads tracking pages, which is what made the
    # window flicker between tabs and impossible to sign in on.
    login_url = login_page_url(adapter)
    with _LOGIN_HELPERS_LOCK:
        existing = _LOGIN_HELPERS.get(competitor)
        if existing is not None and existing.poll() is None:
            LOGGER.info("The %s login refresh helper is already open", competitor)
            return existing
        if existing is not None:
            _LOGIN_HELPERS.pop(competitor, None)
        if _recent_login_helper_opened(competitor):
            LOGGER.info("Skipping duplicate %s login refresh request", competitor)
            return None
        LOGGER.info("Opening %s login refresh helper", competitor)
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "auth_bootstrap.py"), "--competitor", competitor, "--url", login_url],
            cwd=ROOT,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0,
        )
        _LOGIN_HELPERS[competitor] = process
        return process


def _recent_login_helper_opened(competitor: str) -> bool:
    marker = BRIDGE_DIR / f"login-helper-opened-{competitor}.json"
    now = time.time()
    try:
        if marker.exists():
            data = json.loads(marker.read_text(encoding="utf-8"))
            opened_at = float(data.get("opened_at") or 0)
            if now - opened_at < LOGIN_HELPER_COOLDOWN_SECONDS:
                return True
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"competitor": competitor, "opened_at": now}), encoding="utf-8")
    except Exception as exc:
        LOGGER.warning("Could not update %s login helper marker: %s", competitor, exc)
    return False


def _wait_for_saved_sign_in(
    competitor: str,
    *,
    report,
    should_cancel,
    total: int,
    timeout_seconds: int = LOGIN_WAIT_SECONDS,
    poll_seconds: float = 2.0,
) -> tuple[bool, str]:
    """Pause a claimed job until the sign-in helper saves a usable session."""
    adapter = get_competitor(competitor)
    deadline = time.monotonic() + timeout_seconds
    last_reason = "waiting for sign-in"
    while time.monotonic() < deadline:
        if should_cancel():
            return (False, "cancelled")
        usable, last_reason = saved_session_is_usable(competitor)
        if usable:
            report(
                competitor,
                {
                    "status": "login_saved",
                    "message": f"{adapter.display_name} sign-in saved. Continuing this price check automatically.",
                    "competitor": competitor,
                    "competitor_key": competitor,
                    "rows": [],
                    "completed": 0,
                    "total": total,
                },
            )
            return (True, last_reason)
        report(
            competitor,
            {
                "status": "waiting_for_login",
                "message": (
                    f"Waiting for {adapter.display_name} sign-in on this computer. "
                    "Finish signing in; this price check will continue automatically."
                ),
                "competitor": competitor,
                "competitor_key": competitor,
                "rows": [],
                "completed": 0,
                "total": total,
            },
        )
        time.sleep(poll_seconds)
    return (False, f"sign-in was not completed within {timeout_seconds // 60} minutes ({last_reason})")


def _run_job(job: dict[str, object], config: dict[str, object], server_url: str, auth_header: str | None, agent_id: str) -> None:
    job_id = str(job["job_id"])
    try:
        _run_job_body(job, config, server_url, auth_header, agent_id)
    except Exception as exc:
        # A job was claimed (flipped to "running") before this point, so a
        # setup failure here must still be reported. Otherwise the job stays
        # "running" forever with nothing on screen to explain why, and no
        # amount of restarting the Browser Helper changes what is displayed,
        # since that status was never tied to whether a live process backs it.
        LOGGER.exception("Job %s failed before any competitor could start", job_id)
        try:
            _request_json(
                f"{server_url}/collector/agent/jobs/{job_id}/complete?{urllib.parse.urlencode({'agent_id': agent_id})}",
                auth_header,
                method="POST",
                payload={"status": "failed", "message": f"Could not start this price check: {exc}"},
            )
        except Exception:
            LOGGER.exception("Could not even report job %s as failed", job_id)


def _run_job_body(job: dict[str, object], config: dict[str, object], server_url: str, auth_header: str | None, agent_id: str) -> None:
    job_id = str(job["job_id"])
    competitors = [str(item) for item in job.get("competitors", [])]
    max_parts = int(job.get("planned_count") or 0)
    delay_seconds = int(job.get("delay_seconds") or 1)
    collection_mode = str(job.get("collection_mode") or "full_browser")
    headless = bool(config.get("headless", False))
    job_dir = BRIDGE_DIR / "agent_jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / "collector-input.csv"
    _download(f"{server_url}{job['input_url']}", input_path, auth_header)

    # Check every sign-in before any collection starts. Opening a sign-in
    # window while other competitors are already driving browsers made it
    # impossible to actually sign in: the window kept losing focus to pages
    # being opened by the running collectors. Stopping here means the sign-in
    # window is the only thing on screen.
    def report(competitor: str, payload: dict[str, object]) -> None:
        try:
            _request_json(
                f"{server_url}/collector/agent/jobs/{job_id}/progress/{competitor}?{urllib.parse.urlencode({'agent_id': agent_id})}",
                auth_header,
                method="POST",
                payload=payload,
            )
        except Exception:
            LOGGER.warning("Could not report progress for %s", competitor)

    def job_cancelled() -> bool:
        try:
            result = _request_json(
                f"{server_url}/collector/agent/jobs/{job_id}/cancelled", auth_header
            )
        except Exception:
            return False
        return bool(result and result.get("cancelled"))

    # Verify a login against one real product before any competitor browser
    # starts. Cookie expiry alone is not enough: Partzilla can invalidate a
    # still-current cookie server-side. Discovering that only after all four
    # competitors started made the sign-in appear to do nothing while the
    # others continued for several minutes.
    needs_sign_in = []
    sign_in_reasons: dict[str, str] = {}
    probe_parts: dict[str, object] = {}
    for competitor in competitors:
        if not get_competitor(competitor).requires_login:
            continue
        usable, reason = saved_session_is_usable(competitor)
        probe_part = first_probe_part(input_path, competitor)
        if probe_part is not None:
            probe_parts[competitor] = probe_part
        if usable and probe_part is not None:
            report(
                competitor,
                {
                    "status": "verifying_login",
                    "message": f"Checking the saved {get_competitor(competitor).display_name} sign-in before prices start.",
                    "competitor": competitor,
                    "competitor_key": competitor,
                    "rows": [],
                    "completed": 0,
                    "total": max_parts,
                },
            )
            usable, reason = verify_saved_session(
                competitor,
                probe_part,
                headless=False,
                timeout_ms=20_000,
            )
        if not usable:
            try:
                delete_auth_state(competitor)
            except Exception as exc:
                LOGGER.warning("Could not remove the invalid %s sign-in: %s", competitor, exc)
            needs_sign_in.append(competitor)
            sign_in_reasons[competitor] = reason

    if needs_sign_in:
        names = ", ".join(get_competitor(key).display_name for key in needs_sign_in)
        detail = "; ".join(
            f"{get_competitor(key).display_name}: {sign_in_reasons[key]}" for key in needs_sign_in
        )
        LOGGER.info("Sign-in needed before collecting: %s (%s)", names, detail)
        for key in needs_sign_in:
            ready = False
            reason = sign_in_reasons[key]
            for attempt in range(2):
                _open_login_refresh(
                    {"competitor_key": key, "display_name": get_competitor(key).display_name}
                )
                ready, reason = _wait_for_saved_sign_in(
                    key,
                    report=report,
                    should_cancel=job_cancelled,
                    total=max_parts,
                )
                if not ready:
                    break
                probe_part = probe_parts.get(key)
                if probe_part is None:
                    break
                report(
                    key,
                    {
                        "status": "verifying_login",
                        "message": f"Confirming the new {get_competitor(key).display_name} sign-in on a product page.",
                        "competitor": key,
                        "competitor_key": key,
                        "rows": [],
                        "completed": 0,
                        "total": max_parts,
                    },
                )
                ready, reason = verify_saved_session(
                    key,
                    probe_part,
                    headless=False,
                    timeout_ms=20_000,
                )
                if ready:
                    break
                try:
                    delete_auth_state(key)
                except Exception as exc:
                    LOGGER.warning("Could not remove the unconfirmed %s sign-in: %s", key, exc)
                LOGGER.info("The new %s sign-in was not confirmed (%s); reopening once", key, reason)
            if not ready:
                status = "cancelled" if reason == "cancelled" else "login_required"
                message = (
                    "Price check cancelled while waiting for sign-in."
                    if status == "cancelled"
                    else f"{get_competitor(key).display_name} {reason}. No prices were checked."
                )
                _request_json(
                    f"{server_url}/collector/agent/jobs/{job_id}/complete?{urllib.parse.urlencode({'agent_id': agent_id})}",
                    auth_header,
                    method="POST",
                    payload={"status": status, "message": message},
                )
                return

    run_token = int(time.time() * 1000)
    jobs: list[tuple[str, Path, int]] = []
    for index, competitor in enumerate(competitors):
        local_db = job_dir / f"collector-{competitor}.db"
        run_id_floor = (run_token * 10) + (index * 2)
        expected_run_id = prepare_local_database(input_path, local_db, [competitor], run_id_floor=run_id_floor)
        jobs.append((competitor, local_db, expected_run_id))

    runner_args = SimpleNamespace(
        collection_mode=collection_mode,
        delay_seconds=delay_seconds,
        headless=headless,
    )
    outcomes: dict[str, dict[str, object]] = {}
    progress_offsets: dict[str, int] = {}

    def run_competitor(local_job: tuple[str, Path, int]) -> dict[str, object]:
        competitor, local_db, expected_run_id = local_job

        def send_progress(progress: dict[str, object]) -> None:
            offset = progress_offsets.get(competitor)
            if offset:
                # A retry after sign-in only processes the remaining parts, so
                # collect_parts.py's own total/completed describe that smaller
                # remainder. Without adjusting them here, the screen would show
                # progress reset to 0 of a smaller number, which looks exactly
                # like the whole competitor restarted even though the already
                # -checked parts were not repeated.
                progress = dict(progress)
                progress["completed"] = offset + int(progress.get("completed") or 0)
                progress["total"] = max_parts
                progress["remaining"] = max(0, max_parts - progress["completed"])
            try:
                _request_json(
                    f"{server_url}/collector/agent/jobs/{job_id}/progress/{competitor}?{urllib.parse.urlencode({'agent_id': agent_id})}",
                    auth_header,
                    method="POST",
                    payload=progress,
                )
            except Exception as exc:
                LOGGER.warning("Could not send %s progress for job %s: %s", competitor, job_id, exc)

        adapter = get_competitor(competitor)

        def attempt(db_path: Path, run_id: int) -> dict[str, object]:
            """Run collect_parts.py once and report what happened.

            A competitor requiring login needing a fresh sign-in, whether
            noticed before starting or discovered mid-run, is reported back as
            status "login_required" rather than raised, so the caller can
            decide whether to wait and retry.
            """

            def should_cancel() -> bool:
                return job_cancelled()

            try:
                summary = _run_competitor(
                    input_path,
                    db_path,
                    max_parts,
                    competitor,
                    runner_args,
                    expected_run_id=run_id,
                    progress_callback=send_progress,
                    should_cancel=should_cancel,
                )
            except subprocess.CalledProcessError:
                progress_path = BRIDGE_DIR / f"progress-{run_id}-{competitor}.json"
                failed_progress = _read_progress(progress_path)
                if str(failed_progress.get("stop_reason") or "") == "saved_sign_in_unusable":
                    try:
                        delete_auth_state(adapter.competitor_key)
                    except Exception as exc:
                        LOGGER.warning("Could not remove the unusable %s sign-in: %s", competitor, exc)
                    progress = {
                        "status": "login_required",
                        "message": (
                            f"The saved {adapter.display_name} sign-in could not be loaded and was "
                            f"removed. This price check will pause for sign-in and then continue "
                            f"automatically."
                        ),
                        "competitor": competitor,
                        "competitor_key": adapter.competitor_key,
                        "rows": [],
                        "completed": 0,
                        "total": max_parts,
                    }
                    send_progress(progress)
                    return progress
                raise
            except CollectionCancelled as cancelled_exc:
                # Upload whatever was collected before the cancel. Previously
                # this returned immediately, before ever reaching the upload
                # below, so every price gathered during the run was discarded
                # the moment someone pressed Cancel.
                saved_message = "Cancelled."
                partial = getattr(cancelled_exc, "summary", None)
                if partial is not None and partial.exists():
                    try:
                        upload_with_retry(
                            f"{server_url}/collector/results/upload?{urllib.parse.urlencode({'competitor': competitor, 'filename': partial.name, 'job_id': job_id})}",
                            partial,
                            auth_header,
                        )
                        saved_message = "Cancelled. Prices already checked before cancelling were saved."
                        LOGGER.info("%s job %s: uploaded partial results after cancel", competitor, job_id)
                    except Exception:
                        LOGGER.exception("%s job %s: could not upload partial results after cancel", competitor, job_id)
                        saved_message = (
                            f"Cancelled. Prices checked before cancelling are saved on this computer at "
                            f"{partial.resolve()} and can be imported with recover_lost_results.py."
                        )
                progress = {
                    "status": "cancelled",
                    "message": saved_message,
                    "competitor": competitor,
                    "competitor_key": competitor,
                    "rows": [],
                    "completed": 0,
                    "total": max_parts,
                }
                send_progress(progress)
                return progress
            try:
                upload_result = upload_with_retry(
                    f"{server_url}/collector/results/upload?{urllib.parse.urlencode({'competitor': competitor, 'filename': summary.name, 'job_id': job_id})}",
                    summary,
                    auth_header,
                )
            except Exception as exc:
                # collect_parts.py already wrote these results to disk under
                # data/output/collection_runs/, independent of whether Part
                # Pulse could be reached. Reporting that path here means the
                # data is recoverable even after every retry has failed.
                LOGGER.exception("%s job %s: could not upload results after retrying", competitor, job_id)
                progress = {
                    "status": "failed",
                    "message": (
                        f"{adapter.display_name} finished checking prices, but the results could not "
                        f"be uploaded to Part Pulse ({exc}). They are saved on this computer at "
                        f"{summary.resolve()} and are not lost; run recover_lost_results.py to import them."
                    ),
                    "competitor": competitor,
                    "competitor_key": competitor,
                    "rows": [],
                    "completed": 0,
                    "total": max_parts,
                }
                send_progress(progress)
                return progress
            LOGGER.info("%s job %s upload: %s", competitor, job_id, upload_result)
            progress_path = BRIDGE_DIR / f"progress-{run_id}-{competitor}.json"
            outcome = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {"status": "completed"}

            # A saved sign-in that has since expired would otherwise fail every
            # run forever: the file still exists, so a plain existence check
            # never fires and the sign-in window never reopens. Clear it and
            # ask for a fresh sign-in.
            if str(outcome.get("stop_reason") or "") == "authentication_lost":
                LOGGER.info("%s sign-in has expired; clearing it and reopening the sign-in helper", competitor)
                try:
                    delete_auth_state(adapter.competitor_key)
                except Exception as exc:
                    LOGGER.warning("Could not remove the expired %s sign-in: %s", competitor, exc)
                outcome = {
                    "status": "login_required",
                    "message": (
                        f"Your saved {adapter.display_name} sign-in has expired. This price check is "
                        f"paused and will continue automatically after you sign in."
                    ),
                    "competitor": competitor,
                    "competitor_key": adapter.competitor_key,
                    "rows": outcome.get("rows") or [],
                    "completed": outcome.get("completed") or 0,
                    "total": outcome.get("total") or max_parts,
                }
                send_progress(outcome)
            return outcome

        usable, reason = saved_session_is_usable(adapter.competitor_key) if adapter.requires_login else (True, "")
        if usable:
            outcome = attempt(local_db, expected_run_id)
        else:
            # A backstop only. The pre-flight check before any collection began
            # should already have caught this.
            LOGGER.info("%s sign-in not usable (%s) before starting", competitor, reason)
            outcome = {
                "status": "login_required",
                "message": f"{adapter.display_name} needs you to sign in ({reason}).",
                "competitor": competitor,
                "competitor_key": adapter.competitor_key,
                "rows": [],
                "completed": 0,
                "total": max_parts,
            }
            send_progress(outcome)

        # If a site invalidated a session mid-run, or the sign-in was never
        # usable to begin with, open the sign-in window and wait for it right
        # here, in this competitor's own thread. The other competitors keep
        # running in their own threads throughout, since Python threads run
        # concurrently. Waiting for every other competitor to finish first,
        # which an earlier version of this did, meant a session expiring on
        # one competitor added its own wait time on top of the others instead
        # of overlapping with it - doubling the time a large run took.
        latest_db, latest_run_id = local_db, expected_run_id
        attempted_keys: set[tuple[str, str]] = set()
        retries = 0
        # No cap on the number of retries here. _wait_for_saved_sign_in already
        # gives up on its own if nobody signs in within its own timeout, which
        # is the actual protection against waiting forever unattended. A
        # separate numeric cap on top of that gave up on a competitor after 3
        # sign-ins even when the person kept signing in successfully every
        # time, with no way to resume afterward: the loop had already returned,
        # so signing in again had nothing left to reconnect to.
        while outcome.get("status") == "login_required":
            retries += 1
            _open_login_refresh({"competitor_key": competitor, "display_name": adapter.display_name})
            ready, reason = _wait_for_saved_sign_in(
                competitor,
                report=report,
                should_cancel=job_cancelled,
                total=max_parts,
            )
            if not ready:
                outcome = {
                    "status": "cancelled" if reason == "cancelled" else "login_required",
                    "message": reason,
                }
                break
            attempted_keys |= already_attempted_part_keys(latest_db, latest_run_id)
            if attempted_keys:
                report(
                    competitor,
                    {
                        "status": "running",
                        "message": (
                            f"Resuming {adapter.display_name}: {len(attempted_keys)} parts already "
                            f"checked before the sign-in expired will not be repeated."
                        ),
                        "competitor": competitor,
                        "competitor_key": competitor,
                        "rows": [],
                        "completed": len(attempted_keys),
                        "total": max_parts,
                    },
                )
            retry_db = job_dir / f"collector-{competitor}-retry-{retries}.db"
            retry_floor = (int(time.time() * 1000) * 10) + (retries * 2)
            retry_run_id = prepare_local_database(
                input_path,
                retry_db,
                [competitor],
                run_id_floor=retry_floor,
                skip_keys=attempted_keys,
            )
            progress_offsets[competitor] = len(attempted_keys)
            latest_db, latest_run_id = retry_db, retry_run_id
            try:
                outcome = attempt(retry_db, retry_run_id)
            except Exception as exc:
                LOGGER.exception("%s failed during automatic sign-in retry for job %s", competitor, job_id)
                outcome = {"status": "failed", "message": str(exc)}
                break
        return outcome

    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as executor:
        futures = {executor.submit(run_competitor, local_job): local_job[0] for local_job in jobs}
        for future in as_completed(futures):
            competitor = futures[future]
            try:
                outcomes[competitor] = future.result()
            except Exception as exc:
                # This used to update only the local outcomes dict, never
                # telling the server anything went wrong. A competitor whose
                # subprocess crashed after this point kept showing whatever
                # progress it last reported before the crash - a status that
                # looked clean and complete, forever, even though the agent
                # itself already knew it had actually failed.
                LOGGER.exception("%s failed during job %s", competitor, job_id)
                outcomes[competitor] = {"status": "failed", "message": str(exc)}
                report(
                    competitor,
                    {
                        "status": "failed",
                        "message": f"{get_competitor(competitor).display_name} stopped unexpectedly: {exc}",
                        "competitor": competitor,
                        "competitor_key": competitor,
                        "rows": [],
                        "completed": 0,
                        "total": max_parts,
                    },
                )

    failed = [key for key, value in outcomes.items() if value.get("status") == "failed"]
    needs_login = [key for key, value in outcomes.items() if value.get("status") == "login_required"]
    cancelled = [key for key, value in outcomes.items() if value.get("status") == "cancelled"]
    warned = [
        key
        for key, value in outcomes.items()
        if str(value.get("run_status") or value.get("status") or "")
        not in {"completed", "running", "login_required", "cancelled"}
    ]
    if cancelled:
        status = "cancelled"
        message = "Price check cancelled."
    elif needs_login:
        names = ", ".join(get_competitor(key).display_name for key in needs_login)
        sign_in_text = (
            f"{names} needs you to sign in before prices can be checked. A sign-in window has "
            f"opened on the computer running the Browser Helper. Sign in there; this price check "
            f"will continue automatically."
        )
        if failed:
            # Reported as two separate facts. Running them together read as
            # though the failing competitor was the one needing the sign-in.
            other = ", ".join(get_competitor(key).display_name for key in failed)
            status = "failed"
            message = f"Two separate problems. First: {sign_in_text} Second, unrelated: {other} could not be checked."
        else:
            status = "login_required"
            message = sign_in_text
    elif failed:
        status = "failed"
        message = f"Local collection failed for: {', '.join(failed)}."
    elif warned:
        status = "completed_with_warnings"
        message = f"Price check finished with warnings for: {', '.join(warned)}."
    else:
        status = "completed"
        message = "All selected competitor price checks finished and results were imported."
        # A competitor that needed a sign-in retry finishes on whatever was left
        # of the original total, which can be a small number. Stating the real
        # total explicitly here avoids that number being mistaken for the whole
        # result, since a small number immediately followed by "Completed" reads
        # as though very little was actually checked.
        resumed = {key: offset for key, offset in progress_offsets.items() if offset}
        if resumed:
            details = ", ".join(
                f"{get_competitor(key).display_name} needed a fresh sign-in partway through and "
                f"finished with all {max_parts} parts checked ({offset} recovered automatically)"
                for key, offset in resumed.items()
            )
            message = f"{message} {details}."
    _request_json(
        f"{server_url}/collector/agent/jobs/{job_id}/complete?{urllib.parse.urlencode({'agent_id': agent_id})}",
        auth_header,
        method="POST",
        payload={"status": status, "message": message},
    )


def _read_progress(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _request_json(
    url: str,
    auth_header: str | None,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    allow_empty: bool = False,
) -> dict[str, object] | None:
    data = json.dumps(payload).encode("utf-8") if payload is not None else (b"" if method == "POST" else None)
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    if auth_header:
        request.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        if allow_empty and exc.code == 204:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Part Pulse returned HTTP {exc.code}: {detail}") from exc
    if not content:
        return None if allow_empty else {}
    parsed = json.loads(content.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _load_config(path: Path) -> dict[str, object]:
    if not path.exists():
        raise SystemExit(f"Desktop collector setup is incomplete. Run setup_local_collector_agent.py first. Missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _configure_logging() -> None:
    log_path = BRIDGE_DIR / "local_collector_agent.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)


if __name__ == "__main__":
    raise SystemExit(main())
