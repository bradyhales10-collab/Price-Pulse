from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

from app.auth_session import auth_state_exists, delete_auth_state
from app.competitors.registry import get_competitor
from app.local_agent_credentials import unprotect_password
from app.models import PartRecord
from local_collector import (
    BRIDGE_DIR,
    CollectionCancelled,
    _auth_header,
    _download,
    _run_competitor,
    _upload,
    prepare_local_database,
)
from setup_local_collector_agent import normalize_server_url

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "data" / "private" / "local_collector_agent.json"
LOGGER = logging.getLogger("part-pulse-local-agent")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for Part Pulse price-check jobs and run them on this computer.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="Check for one job and then exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = _load_config(args.config)
    _configure_logging()
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

    while True:
        try:
            login_request = _request_json(
                f"{server_url}/collector/agent/login/next?{urllib.parse.urlencode({'agent_id': agent_id})}",
                auth_header,
                method="POST",
                allow_empty=True,
            )
            if login_request:
                _open_login_refresh(login_request)
                if args.once:
                    return 0
                time.sleep(poll_seconds)
                continue
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


def _open_login_refresh(request: dict[str, object]) -> None:
    competitor = str(request.get("competitor_key") or "").strip().lower()
    scripts = {
        "partzilla": ROOT / "Refresh Partzilla Login.cmd",
        "chaparral": ROOT / "Refresh Chaparral Login.cmd",
    }
    adapter = get_competitor(competitor)
    manufacturer = adapter.supported_manufacturers[0] if adapter.supported_manufacturers else "Honda"
    login_url = adapter.build_product_url(PartRecord("LOGIN", manufacturer, "41080-1514"))
    script = scripts.get(competitor)
    if _recent_login_helper_opened(competitor):
        LOGGER.info("Skipping %s login refresh because a helper was opened recently", competitor)
        return
    LOGGER.info("Opening %s login refresh helper", competitor)
    if os.name == "nt" and script is not None and script.exists():
        subprocess.Popen(
            ["cmd.exe", "/k", "call", str(script)],
            cwd=ROOT,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
    else:
        subprocess.Popen(
            [sys.executable, str(ROOT / "auth_bootstrap.py"), "--competitor", competitor, "--url", login_url],
            cwd=ROOT,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0,
        )


def _recent_login_helper_opened(competitor: str) -> bool:
    marker = BRIDGE_DIR / f"login-helper-opened-{competitor}.json"
    now = time.time()
    try:
        if marker.exists():
            data = json.loads(marker.read_text(encoding="utf-8"))
            opened_at = float(data.get("opened_at") or 0)
            if now - opened_at < 45:
                return True
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"competitor": competitor, "opened_at": now}), encoding="utf-8")
    except Exception as exc:
        LOGGER.warning("Could not update %s login helper marker: %s", competitor, exc)
    return False


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
    needs_sign_in = [
        competitor
        for competitor in competitors
        if get_competitor(competitor).requires_login and not auth_state_exists(competitor)
    ]
    if needs_sign_in:
        names = [get_competitor(key).display_name for key in needs_sign_in]
        joined = ", ".join(names)
        LOGGER.info("Sign-in needed before collecting: %s", joined)
        for key in needs_sign_in:
            adapter = get_competitor(key)
            _open_login_refresh({"competitor_key": key, "display_name": adapter.display_name})
        message = (
            f"Sign in to {joined} first. A sign-in window has opened on this computer. "
            f"Sign in there, close the window, then start the price check again. "
            f"No prices were checked, so nothing was changed."
        )
        for competitor in needs_sign_in:
            try:
                _request_json(
                    f"{server_url}/collector/agent/jobs/{job_id}/progress/{competitor}?{urllib.parse.urlencode({'agent_id': agent_id})}",
                    auth_header,
                    method="POST",
                    payload={
                        "status": "login_required",
                        "message": message,
                        "competitor": competitor,
                        "competitor_key": competitor,
                        "rows": [],
                        "completed": 0,
                        "total": max_parts,
                    },
                )
            except Exception:
                LOGGER.exception("Could not report sign-in requirement for %s", competitor)
        _request_json(
            f"{server_url}/collector/agent/jobs/{job_id}/complete?{urllib.parse.urlencode({'agent_id': agent_id})}",
            auth_header,
            method="POST",
            payload={"status": "login_required", "message": message},
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

    def run_competitor(local_job: tuple[str, Path, int]) -> dict[str, object]:
        competitor, local_db, expected_run_id = local_job

        def send_progress(progress: dict[str, object]) -> None:
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
        if adapter.requires_login and not auth_state_exists(adapter.competitor_key):
            # Without a saved sign-in, collect_parts.py would exit non-zero and the
            # user would only see a generic failure. Report it clearly instead and
            # open the sign-in window on this computer so they can fix it directly.
            LOGGER.info("%s has no saved sign-in; opening the sign-in helper", competitor)
            progress = {
                "status": "login_required",
                "message": (
                    f"{adapter.display_name} needs you to sign in. A sign-in window has opened on "
                    f"this computer. Sign in, then start the price check again."
                ),
                "competitor": competitor,
                "competitor_key": adapter.competitor_key,
                "rows": [],
                "completed": 0,
                "total": max_parts,
            }
            send_progress(progress)
            _open_login_refresh(
                {"competitor_key": adapter.competitor_key, "display_name": adapter.display_name}
            )
            return progress

        def should_cancel() -> bool:
            try:
                result = _request_json(
                    f"{server_url}/collector/agent/jobs/{job_id}/cancelled", auth_header
                )
            except Exception:
                return False
            return bool(result and result.get("cancelled"))

        try:
            summary = _run_competitor(
                input_path,
                local_db,
                max_parts,
                competitor,
                runner_args,
                expected_run_id=expected_run_id,
                progress_callback=send_progress,
                should_cancel=should_cancel,
            )
        except CollectionCancelled:
            progress = {
                "status": "cancelled",
                "message": "Cancelled.",
                "competitor": competitor,
                "competitor_key": competitor,
                "rows": [],
                "completed": 0,
                "total": max_parts,
            }
            send_progress(progress)
            return progress
        upload_result = _upload(
            f"{server_url}/collector/results/upload?{urllib.parse.urlencode({'competitor': competitor, 'filename': summary.name, 'job_id': job_id})}",
            summary,
            auth_header,
        )
        LOGGER.info("%s job %s upload: %s", competitor, job_id, upload_result)
        progress_path = BRIDGE_DIR / f"progress-{expected_run_id}-{competitor}.json"
        outcome = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {"status": "completed"}

        # A saved sign-in that has since expired would otherwise fail every run
        # forever: the file still exists, so the pre-check above never fires and
        # the sign-in window never reopens. Clear it and ask for a fresh sign-in.
        if str(outcome.get("stop_reason") or "") == "authentication_lost":
            LOGGER.info("%s sign-in has expired; clearing it and reopening the sign-in helper", competitor)
            try:
                delete_auth_state(adapter.competitor_key)
            except Exception as exc:
                LOGGER.warning("Could not remove the expired %s sign-in: %s", competitor, exc)
            outcome = {
                "status": "login_required",
                "message": (
                    f"Your saved {adapter.display_name} sign-in has expired. A sign-in window has "
                    f"opened on this computer. Sign in, then start the price check again."
                ),
                "competitor": competitor,
                "competitor_key": adapter.competitor_key,
                "rows": outcome.get("rows") or [],
                "completed": outcome.get("completed") or 0,
                "total": outcome.get("total") or max_parts,
            }
            send_progress(outcome)
            _open_login_refresh(
                {"competitor_key": adapter.competitor_key, "display_name": adapter.display_name}
            )
        return outcome

    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as executor:
        futures = {executor.submit(run_competitor, local_job): local_job[0] for local_job in jobs}
        for future in as_completed(futures):
            competitor = futures[future]
            try:
                outcomes[competitor] = future.result()
            except Exception as exc:
                LOGGER.exception("%s failed during job %s", competitor, job_id)
                outcomes[competitor] = {"status": "failed", "message": str(exc)}

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
            f"opened on the computer running the Browser Helper. Sign in there, then start the "
            f"price check again."
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
    _request_json(
        f"{server_url}/collector/agent/jobs/{job_id}/complete?{urllib.parse.urlencode({'agent_id': agent_id})}",
        auth_header,
        method="POST",
        payload={"status": status, "message": message},
    )


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
