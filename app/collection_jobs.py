from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import threading
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.auth_session import MissingAuthStateError, auth_state_exists, require_competitor_auth_state
from app.competitors.registry import get_competitor
from app.config import DATA_DIR
from app.database import cents_to_money, connect_database, utc_now
from app.input_loader import FIELDNAMES

JOB_DIR = DATA_DIR / "output" / "ui_collection_jobs"
LOCAL_AGENT_STATUS_FILE = JOB_DIR / "local_agent_status.json"
LOCAL_LOGIN_REQUEST_DIR = JOB_DIR / "local_login_requests"
MAX_UI_COLLECTION_PARTS = 50
MIN_UI_DELAY_SECONDS = 1
DEFAULT_PRICE_COLLECTION_MODE = "lightweight_browser"
DEFAULT_PRICE_HEADLESS = True
ACTIVE_JOB_STATUSES = {"queued_local", "running"}
_LOCAL_JOB_LOCK = threading.Lock()


def request_job_cancellation(job_id: str) -> dict[str, object]:
    """Ask a queued or running job to stop.

    This only sets a flag. The job actually stops once the collector agent
    notices it on its next check and finalizes the job as cancelled, or once
    the person restarts the Browser Helper, since that also frees it up to
    pick up a new job regardless of what this one is doing.
    """
    path = JOB_DIR / job_id / "job.json"
    if not path.exists():
        return {"status": "not_found", "job_id": job_id}
    with _LOCAL_JOB_LOCK:
        metadata = _read_json(path)
        if str(metadata.get("status") or "") not in ACTIVE_JOB_STATUSES:
            return metadata
        metadata["cancel_requested"] = True
        metadata["cancel_requested_at"] = utc_now()
        metadata["message"] = "Cancelling this price check. This finishes within a few seconds."
        _write_json(path, metadata)
        return metadata


def is_job_cancelled(job_id: str) -> bool:
    path = JOB_DIR / job_id / "job.json"
    if not path.exists():
        return False
    return bool(_read_json(path).get("cancel_requested"))


CANCEL_GRACE_SECONDS = 10


def _finalize_cancellation_if_due(path: Path, metadata: dict[str, object]) -> dict[str, object]:
    """Finish a cancellation ourselves if the agent has not acknowledged it.

    The agent checks for a cancellation between parts, so acknowledging it
    normally takes a moment. This is the backstop: if the agent is stuck,
    closed, or simply never checks again, the person still gets unstuck
    rather than waiting on cooperation from a process that may not respond.
    """
    if not metadata.get("cancel_requested"):
        return metadata
    if str(metadata.get("status") or "") not in ACTIVE_JOB_STATUSES:
        return metadata
    requested_at = str(metadata.get("cancel_requested_at") or "")
    try:
        stamp = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    except ValueError:
        stamp = None
    if stamp is None or (datetime.now(UTC) - stamp.astimezone(UTC)).total_seconds() >= CANCEL_GRACE_SECONDS:
        metadata["status"] = "cancelled"
        metadata["message"] = "Price check cancelled."
        metadata["updated_at"] = utc_now()
        metadata["finished_at"] = utc_now()
        _write_json(path, metadata)
    return metadata


@dataclass(frozen=True)
class PlannedCollectionPart:
    manufacturer: str
    oem_part_number: str
    our_current_price: str
    partzilla_price: str
    last_checked: str | None


def plan_ui_collection(database: Path, *, manufacturer: str = "", limit: int = 25) -> list[PlannedCollectionPart]:
    limit = min(max(1, limit), MAX_UI_COLLECTION_PARTS)
    clauses = ["ips.is_active=1"]
    params: list[str] = []
    if manufacturer:
        clauses.append("p.manufacturer=?")
        params.append(manufacturer)
    with connect_database(database) as conn:
        rows = conn.execute(
            f"""
            SELECT p.manufacturer, p.oem_part_number, ips.our_current_price_cents,
                   s.selling_price_cents, s.last_successful_check_at
            FROM internal_product_state ips
            JOIN products p ON p.product_id=ips.product_id
            LEFT JOIN competitor_listings l ON l.product_id=p.product_id
            LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(ips.scan_priority, ''), p.manufacturer, p.oem_part_number
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
    return [
        PlannedCollectionPart(
            row["manufacturer"],
            row["oem_part_number"],
            cents_to_money(row["our_current_price_cents"]),
            cents_to_money(row["selling_price_cents"]),
            row["last_successful_check_at"],
        )
        for row in rows
    ]


def plan_import_collection(database: Path, *, import_batch_id: int, limit: int | None = None) -> list[PlannedCollectionPart]:
    limit_clause = ""
    params: list[object] = [import_batch_id]
    if limit is not None:
        limit_clause = "LIMIT ?"
        params.append(max(1, limit))
    with connect_database(database) as conn:
        rows = conn.execute(
            f"""
            SELECT p.manufacturer, p.oem_part_number, ips.our_current_price_cents,
                   s.selling_price_cents, s.last_successful_check_at
            FROM internal_product_state ips
            JOIN products p ON p.product_id=ips.product_id
            LEFT JOIN competitor_listings l ON l.product_id=p.product_id
            LEFT JOIN current_listing_state s ON s.listing_id=l.listing_id
            WHERE ips.is_active=1 AND ips.source_import_batch_id=?
            ORDER BY COALESCE(ips.scan_priority, ''), p.manufacturer, p.oem_part_number
            {limit_clause}
            """,
            params,
        ).fetchall()
    return [
        PlannedCollectionPart(
            row["manufacturer"],
            row["oem_part_number"],
            cents_to_money(row["our_current_price_cents"]),
            cents_to_money(row["selling_price_cents"]),
            row["last_successful_check_at"],
        )
        for row in rows
    ]


def validate_collection_request(
    database: Path,
    parts: list[PlannedCollectionPart],
    *,
    confirmation: str,
    delay_seconds: int,
    competitor_keys: list[str] | None = None,
    max_parts: int | None = MAX_UI_COLLECTION_PARTS,
    require_saved_auth: bool = True,
) -> list[str]:
    if competitor_keys is None:
        competitor_keys = ["partzilla"]
    errors: list[str] = []
    if not competitor_keys:
        errors.append("Select at least one competitor to check.")
    if confirmation != "RUN":
        errors.append("Type RUN to confirm the test collection.")
    if not parts:
        errors.append("No active imported products are available to collect. Confirm an import first.")
    if not database.exists():
        errors.append("Database not found.")
    for competitor_key in competitor_keys:
        adapter = get_competitor(competitor_key)
        if require_saved_auth and adapter.requires_login and not auth_state_exists(adapter.competitor_key):
            errors.append(f"Saved {adapter.display_name} authentication state was not found. Run auth_bootstrap.py --competitor {adapter.competitor_key} first.")
    if max_parts is not None and len(parts) > max_parts:
        errors.append(f"Interface-launched test collections are limited to {max_parts} products.")
    if delay_seconds < MIN_UI_DELAY_SECONDS:
        errors.append("Delay must be at least 1 second.")
    if parts and active_job_exists():
        errors.append("Another collection job is already active.")
    return errors


def active_job_exists() -> bool:
    if not JOB_DIR.exists():
        return False
    for path in JOB_DIR.glob("*/job.json"):
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        status = str(metadata.get("status") or "")
        if status == "queued_local":
            return True
        if status == "running" and _running_job_is_active(path, metadata):
            return True
    return False


def _running_job_is_active(path: Path, metadata: dict[str, object]) -> bool:
    pid = metadata.get("pid")
    if isinstance(pid, int) and _pid_is_running(pid):
        return True
    if isinstance(pid, int):
        _mark_job_status(path, metadata, "failed", _job_error_message(path.parent))
        return False
    started_at = str(metadata.get("started_at") or "")
    if _started_more_than_hours_ago(started_at, 2):
        _mark_job_status(path, metadata, "stale", "Running job had no process id and is older than two hours.")
        return False
    return True


def start_collection_job(
    database: Path,
    parts: list[PlannedCollectionPart],
    *,
    delay_seconds: int = 1,
    collection_mode: str = "full_browser",
    competitor_keys: list[str] | None = None,
    launch_background: bool = False,
) -> str:
    competitor_keys = competitor_keys or ["partzilla"]
    job_id = utc_now().replace(":", "").replace("-", "")
    job_path = JOB_DIR / job_id
    job_path.mkdir(parents=True, exist_ok=True)
    input_path = job_path / "selected_parts.csv"
    with input_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        for index, part in enumerate(parts, start=1):
            writer.writerow(
                {
                    "Test_Case_ID": f"UI-{index}",
                    "Manufacturer": part.manufacturer,
                    "OEM_Part_Number": part.oem_part_number,
                    "Search_Observed_Product_Name": "",
                    "Search_Observed_MSRP": "",
                    "Expected_Partzilla_URL": "",
                    "Test_Purpose": "UI test collection",
                    "Verified_Date": "",
                    "Source_URL": "",
                }
            )
    metadata = {
        "job_id": job_id,
        "status": "prepared",
        "planned_count": len(parts),
        "started_at": utc_now(),
        "database": str(database),
        "input_file": str(input_path),
        "competitors": competitor_keys,
        "delay_seconds": delay_seconds,
        "manual_command": _manual_command(input_path=input_path, database=database, max_parts=len(parts), delay_seconds=delay_seconds, collection_mode=collection_mode, competitor_keys=competitor_keys),
    }
    (job_path / "job.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    if not launch_background:
        return job_id
    metadata["status"] = "running"
    (job_path / "job.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    args = [
        sys.executable,
        "-m",
        "app.ui_collection_worker",
        "--job-dir",
        str(job_path),
        "--database",
        str(database),
        "--input-file",
        str(input_path),
        "--collection-mode",
        collection_mode,
        "--delay-seconds",
        str(delay_seconds),
        "--competitor",
        competitor_keys[0],
    ]
    with (job_path / "worker_stdout.log").open("w", encoding="utf-8") as stdout, (job_path / "worker_stderr.log").open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(args, cwd=Path(__file__).resolve().parents[1], stdout=stdout, stderr=stderr)
    pid = getattr(process, "pid", None)
    if isinstance(pid, int):
        metadata["pid"] = pid
        metadata["worker_pid"] = pid
    (job_path / "job.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return job_id


def start_price_collection_job(
    database: Path,
    parts: list[PlannedCollectionPart],
    *,
    delay_seconds: int = 1,
    collection_mode: str = DEFAULT_PRICE_COLLECTION_MODE,
    headless: bool = DEFAULT_PRICE_HEADLESS,
    competitor_keys: list[str] | None = None,
) -> str:
    competitor_keys = competitor_keys or ["partzilla"]
    job_id = start_collection_job(
        database,
        parts,
        delay_seconds=delay_seconds,
        collection_mode=collection_mode,
        competitor_keys=competitor_keys,
        launch_background=False,
    )
    job_path = JOB_DIR / job_id
    job_json = job_path / "job.json"
    metadata = json.loads(job_json.read_text(encoding="utf-8"))
    progress_files = {key: str(_competitor_progress_file(job_path, key)) for key in competitor_keys}
    metadata.update(
        {
            "status": "running",
            "mode": "headless" if headless else "visible",
            "collection_mode": collection_mode,
            "competitors": competitor_keys,
            "progress_file": str(_competitor_progress_file(job_path, competitor_keys[0])),
            "progress_files": progress_files,
            "parallel_competitors": len(competitor_keys) > 1,
            "updated_at": utc_now(),
        }
    )
    job_json.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    thread = threading.Thread(
        target=_run_collection_thread,
        kwargs={
            "job_path": job_path,
            "database": database,
            "input_path": Path(str(metadata["input_file"])),
            "max_parts": len(parts),
            "delay_seconds": delay_seconds,
            "collection_mode": collection_mode,
            "headless": headless,
            "competitor_keys": competitor_keys,
        },
        daemon=True,
    )
    thread.start()
    return job_id


def queue_local_collection_job(
    database: Path,
    parts: list[PlannedCollectionPart],
    *,
    import_batch_id: int,
    delay_seconds: int = 1,
    competitor_keys: list[str] | None = None,
) -> str:
    competitor_keys = competitor_keys or ["partzilla"]
    job_id = start_collection_job(
        database,
        parts,
        delay_seconds=delay_seconds,
        collection_mode="full_browser",
        competitor_keys=competitor_keys,
        launch_background=False,
    )
    job_path = JOB_DIR / job_id
    job_json = job_path / "job.json"
    metadata = json.loads(job_json.read_text(encoding="utf-8"))
    metadata.update(
        {
            "status": "queued_local",
            "execution_target": "local_agent",
            "import_batch_id": import_batch_id,
            "collection_mode": "full_browser",
            "delay_seconds": delay_seconds,
            "mode": "visible",
            "competitors": competitor_keys,
            "parallel_competitors": len(competitor_keys) > 1,
            "progress_files": {key: str(_competitor_progress_file(job_path, key)) for key in competitor_keys},
            "message": "Waiting for the Part Pulse collector on your computer.",
            "updated_at": utc_now(),
        }
    )
    metadata.pop("manual_command", None)
    _write_json(job_json, metadata)
    return job_id


def claim_next_local_job(agent_id: str) -> dict[str, object] | None:
    register_local_agent(agent_id)
    with _LOCAL_JOB_LOCK:
        if not JOB_DIR.exists():
            return None
        for job_json in sorted(JOB_DIR.glob("*/job.json"), key=lambda path: path.stat().st_mtime):
            metadata = _read_json(job_json)
            if metadata.get("status") != "queued_local":
                continue
            metadata["status"] = "running"
            metadata["agent_id"] = agent_id
            metadata["claimed_at"] = utc_now()
            metadata["updated_at"] = utc_now()
            metadata["message"] = "Local collector connected. Opening competitor browsers."
            _write_json(job_json, metadata)
            return metadata
    return None


def queue_local_login_refresh(competitor_key: str) -> str:
    adapter = get_competitor(competitor_key)
    LOCAL_LOGIN_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    for existing_json in LOCAL_LOGIN_REQUEST_DIR.glob(f"*_{adapter.competitor_key}.json"):
        existing = _read_json(existing_json)
        if existing.get("status") in {"queued", "opened"}:
            existing["status"] = "superseded"
            existing["message"] = "A newer login refresh request replaced this one."
            existing["updated_at"] = utc_now()
            _write_json(existing_json, existing)
    request_id = utc_now().replace(":", "").replace("-", "")
    request_path = LOCAL_LOGIN_REQUEST_DIR / f"{request_id}_{adapter.competitor_key}.json"
    _write_json(
        request_path,
        {
            "request_id": request_id,
            "competitor_key": adapter.competitor_key,
            "display_name": adapter.display_name,
            "status": "queued",
            "message": f"Waiting for Desktop Collector to open {adapter.display_name} login.",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        },
    )
    return request_id


def claim_next_local_login_refresh(agent_id: str) -> dict[str, object] | None:
    register_local_agent(agent_id)
    LOCAL_LOGIN_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCAL_JOB_LOCK:
        for request_json in sorted(LOCAL_LOGIN_REQUEST_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime):
            request = _read_json(request_json)
            if request.get("status") != "queued":
                continue
            competitor_key = str(request.get("competitor_key") or "")
            for other_json in LOCAL_LOGIN_REQUEST_DIR.glob(f"*_{competitor_key}.json"):
                if other_json == request_json:
                    continue
                other = _read_json(other_json)
                if other.get("status") == "queued":
                    other["status"] = "superseded"
                    other["message"] = "Another login refresh request was already opened."
                    other["updated_at"] = utc_now()
                    _write_json(other_json, other)
            request["status"] = "opened"
            request["agent_id"] = agent_id
            request["claimed_at"] = utc_now()
            request["updated_at"] = utc_now()
            request["message"] = "Desktop Collector opened the login refresh helper."
            _write_json(request_json, request)
            return request
    return None


def register_local_agent(agent_id: str) -> dict[str, object]:
    status = {"agent_id": agent_id, "last_seen": utc_now(), "connected": True}
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(LOCAL_AGENT_STATUS_FILE, status)
    return status


def local_agent_status() -> dict[str, object]:
    status = _read_json(LOCAL_AGENT_STATUS_FILE)
    last_seen = str(status.get("last_seen") or "")
    connected = False
    try:
        seen_at = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        connected = datetime.now(UTC) - seen_at.astimezone(UTC) <= timedelta(seconds=15)
    except ValueError:
        pass
    status["connected"] = connected
    return status


def local_job_input_path(job_id: str) -> Path | None:
    path = JOB_DIR / job_id / "selected_parts.csv"
    return path if path.exists() else None


def update_local_job_progress(job_id: str, competitor_key: str, progress: dict[str, object], agent_id: str) -> dict[str, object]:
    job_path = JOB_DIR / job_id
    job_json = job_path / "job.json"
    metadata = _read_json(job_json)
    if not metadata:
        raise FileNotFoundError(f"Collection job {job_id} was not found.")
    competitors = [str(item) for item in metadata.get("competitors", [])]
    if competitor_key not in competitors:
        raise ValueError(f"{competitor_key} is not part of collection job {job_id}.")
    progress["competitor"] = competitor_key
    _write_json(_competitor_progress_file(job_path, competitor_key), progress)
    metadata["status"] = "running"
    metadata["agent_id"] = agent_id
    metadata["updated_at"] = utc_now()
    metadata["message"] = f"Checking prices locally. Latest update: {competitor_key}."
    _write_json(job_json, metadata)
    register_local_agent(agent_id)
    return metadata


def complete_local_job(job_id: str, *, status: str, message: str, agent_id: str) -> dict[str, object]:
    job_json = JOB_DIR / job_id / "job.json"
    metadata = _read_json(job_json)
    if not metadata:
        raise FileNotFoundError(f"Collection job {job_id} was not found.")
    allowed = {
        "completed",
        "completed_with_warnings",
        "failed",
        "login_required",
        "stopped_blocked",
        "stopped_challenge",
        "cancelled",
    }
    metadata["status"] = status if status in allowed else "failed"
    metadata["message"] = message
    metadata["agent_id"] = agent_id
    metadata["updated_at"] = utc_now()
    metadata["finished_at"] = utc_now()
    _write_json(job_json, metadata)
    register_local_agent(agent_id)
    return metadata


def job_status(job_id: str) -> dict[str, object]:
    path = JOB_DIR / job_id / "job.json"
    if not path.exists():
        return {"status": "not_found", "job_id": job_id}
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata = _finalize_cancellation_if_due(path, metadata)
    if metadata.get("status") == "running" and not _running_job_is_active(path, metadata):
        metadata = json.loads(path.read_text(encoding="utf-8"))
    if "manual_command" not in metadata and metadata.get("input_file") and metadata.get("database"):
        metadata["manual_command"] = _manual_command(
            input_path=Path(str(metadata["input_file"])),
            database=Path(str(metadata["database"])),
            max_parts=int(metadata.get("planned_count") or 25),
            delay_seconds=1,
            collection_mode=str(metadata.get("collection_mode") or DEFAULT_PRICE_COLLECTION_MODE),
            competitor_keys=[str(item) for item in metadata.get("competitors", ["partzilla"])],
        )
    progress_file = metadata.get("progress_file")
    progress_files = metadata.get("progress_files")
    if isinstance(progress_files, dict):
        by_competitor = {str(key): _read_progress(Path(str(path))) for key, path in progress_files.items()}
        metadata["progress_by_competitor"] = by_competitor
        metadata["progress"] = _aggregate_progress(by_competitor, metadata)
        metadata["competitor_summaries"] = [
            _competitor_progress_summary(key, by_competitor.get(key, {}))
            for key in [str(item) for item in metadata.get("competitors", by_competitor.keys())]
        ]
        failure_details = "; ".join(
            str(item["detail"]).rstrip(".")
            for item in metadata["competitor_summaries"]
            if item.get("actionable_failure") and item.get("detail")
        )
        metadata["failure_reason"] = f"{failure_details}." if failure_details else ""
    elif progress_file and Path(str(progress_file)).exists():
        metadata["progress"] = _read_progress(Path(str(progress_file)))
    return metadata


def cancel_all_active_jobs(reason: str = "Part Pulse was restarted.") -> int:
    """Finalize every queued or running job as cancelled.

    Used when Part Pulse restarts: no Browser Helper is running at that
    moment to contest it, so it is safe to finalize immediately rather than
    wait out the normal grace period. Restarting should mean a clean slate,
    not a stuck job silently kept alive for up to two hours.
    """
    if not JOB_DIR.exists():
        return 0
    cleared = 0
    with _LOCAL_JOB_LOCK:
        for job_json in JOB_DIR.glob("*/job.json"):
            metadata = _read_json(job_json)
            if str(metadata.get("status") or "") not in ACTIVE_JOB_STATUSES:
                continue
            metadata["status"] = "cancelled"
            metadata["message"] = reason
            metadata["updated_at"] = utc_now()
            metadata["finished_at"] = utc_now()
            _write_json(job_json, metadata)
            cleared += 1
    return cleared


def current_active_job() -> dict[str, object] | None:
    if not JOB_DIR.exists():
        return None
    matching: list[tuple[float, str]] = []
    for job_json in JOB_DIR.glob("*/job.json"):
        metadata = _read_json(job_json)
        metadata = _finalize_cancellation_if_due(job_json, metadata)
        if str(metadata.get("status") or "") not in ACTIVE_JOB_STATUSES:
            continue
        if metadata.get("status") == "running" and not _running_job_is_active(job_json, metadata):
            continue
        matching.append((job_json.stat().st_mtime, job_json.parent.name))
    return job_status(max(matching)[1]) if matching else None


def latest_job_for_import(import_batch_id: int) -> dict[str, object] | None:
    if not JOB_DIR.exists():
        return None
    matching: list[tuple[float, str]] = []
    for job_json in JOB_DIR.glob("*/job.json"):
        metadata = _read_json(job_json)
        if metadata.get("import_batch_id") != import_batch_id:
            continue
        matching.append((job_json.stat().st_mtime, job_json.parent.name))
    if not matching:
        return None
    return job_status(max(matching)[1])


def _competitor_progress_summary(competitor: str, progress: dict[str, object]) -> dict[str, object]:
    status = str(progress.get("run_status") or progress.get("status") or "waiting")
    completed = int(progress.get("completed") or 0)
    total = int(progress.get("total") or 0)
    stop_reason = str(progress.get("stop_reason") or "")
    issue_types = {
        "authentication_lost",
        "blocked",
        "cart_cleanup_failed",
        "challenge",
        "cleanup_failed",
        "error",
        "lookup_error",
        "lookup_failed",
        "navigation_error",
    }
    counts = Counter(
        str(row.get("result_type") or row.get("lookup_status") or "")
        for row in progress.get("rows") or []
        if isinstance(row, dict)
    )
    issues = [(key, count) for key, count in counts.items() if key in issue_types and count]
    details: list[str] = []
    if completed or total:
        details.append(f"checked {completed} of {total or completed} parts")
    if stop_reason:
        details.append(stop_reason.replace("_", " "))
    if issues:
        details.append(", ".join(f"{count} {key.replace('_', ' ')}" for key, count in issues))
    terminal_failures = {"failed", "stopped_blocked", "stopped_challenge"}
    needs_attention = status not in {"completed", "running", "queued_local", "waiting"} or bool(issues)
    display_name = {"motosport": "MotoSport", "partzilla": "Partzilla", "chaparral": "Chaparral"}.get(
        competitor,
        competitor.title(),
    )
    if status == "login_required":
        detail = str(progress.get("message") or f"{display_name} needs you to sign in again before prices can be checked.")
    else:
        detail = f"{display_name} {'; '.join(details)}." if details else ""
    return {
        "competitor": competitor,
        "status": status,
        "completed": completed,
        "total": total,
        "needs_attention": needs_attention,
        "actionable_failure": status in terminal_failures or bool(issues),
        "needs_login": status == "login_required",
        "detail": detail,
        "issue_counts": dict(issues),
    }


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _started_more_than_hours_ago(value: str, hours: int) -> bool:
    try:
        started = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(UTC) - started.astimezone(UTC) > timedelta(hours=hours)


def _mark_job_status(path: Path, metadata: dict[str, object], status: str, message: str) -> None:
    metadata["status"] = status
    metadata["finished_at"] = utc_now()
    metadata["message"] = message
    path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def _job_error_message(job_path: Path) -> str:
    for filename in ("stderr.log", "worker_stderr.log", "stdout.log", "worker_stdout.log"):
        log_path = job_path / filename
        if log_path.exists():
            lines = [line.strip() for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
            if lines:
                return lines[-1]
    return "Collector process is no longer running."


def _manual_command(*, input_path: Path, database: Path, max_parts: int, delay_seconds: int, collection_mode: str, competitor_keys: list[str] | None = None) -> str:
    competitor_args = " ".join(f"--competitor {key}" for key in (competitor_keys or ["partzilla"]))
    return (
        f'.\\.venv\\Scripts\\python.exe collect_parts.py --file "{input_path}" '
        f"--max-parts {max_parts} --save-to-database --database \"{database}\" "
        f"{competitor_args} --collection-mode {collection_mode} --delay-seconds {delay_seconds}"
    )


def _competitor_progress_file(job_path: Path, competitor_key: str) -> Path:
    safe_key = "".join(char for char in competitor_key.strip().lower() if char.isalnum() or char in ("_", "-")) or "competitor"
    return job_path / f"progress_{safe_key}.json"


def _run_collection_thread(
    *,
    job_path: Path,
    database: Path,
    input_path: Path,
    max_parts: int,
    delay_seconds: int,
    collection_mode: str,
    headless: bool,
    competitor_keys: list[str],
) -> None:
    job_json = job_path / "job.json"
    try:
        from app.collection import validate_delay
        from app.config import ensure_data_directories
        from app.database import initialize_database
        from app.input_loader import load_parts_csv
        from collect_parts import run_collection

        ensure_data_directories()
        validate_delay(delay_seconds)
        load_result = load_parts_csv(input_path)
        initialize_database(database)
        results: dict[str, dict[str, Any]] = {}
        result_lock = threading.Lock()

        def run_competitor(competitor_key: str) -> None:
            progress_file = _competitor_progress_file(job_path, competitor_key)
            try:
                adapter = get_competitor(competitor_key)
                if adapter.requires_login:
                    require_competitor_auth_state(adapter.competitor_key, competitor_name=adapter.display_name)
                return_code, progress = _run_collection_once(
                    run_collection=run_collection,
                    input_path=input_path,
                    max_parts=max_parts,
                    database=database,
                    delay_seconds=delay_seconds,
                    collection_mode=collection_mode,
                    headless=headless,
                    progress_file=progress_file,
                    competitor_key=competitor_key,
                )
                if headless and competitor_key == "partzilla" and _can_launch_visible_browser() and _should_retry_visible(progress):
                    return_code, progress = _run_collection_once(
                        run_collection=run_collection,
                        input_path=input_path,
                        max_parts=max_parts,
                        database=database,
                        delay_seconds=delay_seconds,
                        collection_mode=collection_mode,
                        headless=False,
                        progress_file=progress_file,
                        competitor_key=competitor_key,
                    )
                with result_lock:
                    results[competitor_key] = {"return_code": return_code, "progress": progress}
            except MissingAuthStateError:
                adapter = get_competitor(competitor_key)
                progress = {
                    "status": "login_required",
                    "message": f"{adapter.display_name} needs you to sign in again before prices can be checked.",
                    "competitor_key": competitor_key,
                    "rows": [],
                    "completed": 0,
                    "total": max_parts,
                    "competitor": competitor_key,
                }
                progress_file.write_text(json.dumps(progress, indent=2), encoding="utf-8")
                with result_lock:
                    results[competitor_key] = {"return_code": 1, "progress": progress}
            except Exception as exc:
                progress = {"status": "failed", "message": str(exc), "rows": [], "completed": 0, "total": max_parts, "competitor": competitor_key}
                progress_file.write_text(json.dumps(progress, indent=2), encoding="utf-8")
                with result_lock:
                    results[competitor_key] = {"return_code": 1, "progress": progress, "traceback": traceback.format_exc()}

        threads = [threading.Thread(target=run_competitor, args=(competitor_key,), daemon=True) for competitor_key in competitor_keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        metadata = json.loads(job_json.read_text(encoding="utf-8"))
        return_codes = [int(result.get("return_code") or 1) for result in results.values()]
        return_code = 0 if all(code == 0 for code in return_codes) else 1
        progress_by_competitor = {key: dict(value.get("progress") or {}) for key, value in results.items()}
        metadata["return_code"] = return_code
        metadata["competitor_results"] = results
        aggregate = _aggregate_progress(progress_by_competitor, metadata)
        run_status = str(aggregate.get("run_status") or aggregate.get("status") or "")
        metadata["status"] = run_status or ("completed" if return_code == 0 else "failed")
        metadata["finished_at"] = utc_now()
        metadata["message"] = _completion_message(return_code, aggregate)
        job_json.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    except Exception as exc:
        metadata = json.loads(job_json.read_text(encoding="utf-8")) if job_json.exists() else {}
        metadata["status"] = "failed"
        metadata["finished_at"] = utc_now()
        metadata["message"] = str(exc)
        metadata["traceback"] = traceback.format_exc()
        job_json.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        (job_path / "progress_failed.json").write_text(
            json.dumps({"status": "failed", "message": str(exc), "rows": [], "completed": 0, "total": max_parts}, indent=2),
            encoding="utf-8",
        )


def _run_collection_once(
    *,
    run_collection,
    input_path: Path,
    max_parts: int,
    database: Path,
    delay_seconds: int,
    collection_mode: str,
    headless: bool,
    progress_file: Path,
    competitor_key: str,
) -> tuple[int, dict[str, object]]:
    args = SimpleNamespace(
        file=input_path,
        max_parts=max_parts,
        database=database,
        delay_seconds=delay_seconds,
        collection_mode=collection_mode,
        dry_run=False,
        save_to_database=True,
        yes=True,
        headless=headless,
        progress_file=progress_file,
        competitor=[competitor_key],
        allow_experimental_competitors=False,
    )
    return_code = int(run_collection(args, _plan_for_args(args)))
    return return_code, _read_progress(progress_file)


def _plan_for_args(args):
    from app.collection import plan_collection
    from app.database import connect_database
    from app.input_loader import load_parts_csv
    from collect_parts import ensure_competitor_listings

    load_result = load_parts_csv(args.file)
    competitor_key = (getattr(args, "competitor", None) or ["partzilla"])[0]
    with connect_database(args.database) as conn:
        ensure_competitor_listings(conn, load_result.records, competitor_key)
        return plan_collection(conn, load_result.records, args.file, args.max_parts, invalid_rows=len(load_result.invalid_rows), competitor_key=competitor_key)


def _read_progress(progress_file: Path) -> dict[str, object]:
    if not progress_file.exists():
        return {}
    try:
        return json.loads(progress_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def _aggregate_progress(progress_by_competitor: dict[str, dict[str, object]], metadata: dict[str, object]) -> dict[str, object]:
    competitors = [str(item) for item in metadata.get("competitors", list(progress_by_competitor.keys()))]
    progresses = [progress_by_competitor.get(key, {}) for key in competitors]
    total = sum(int(progress.get("total") or 0) for progress in progresses)
    completed = sum(int(progress.get("completed") or 0) for progress in progresses)
    remaining = sum(int(progress.get("remaining") or 0) for progress in progresses)
    eta_values = [int(progress.get("eta_seconds") or 0) for progress in progresses if progress.get("eta_seconds") is not None]
    rows: list[dict[str, object]] = []
    for key, progress in progress_by_competitor.items():
        for row in progress.get("rows") or []:
            if isinstance(row, dict):
                item = dict(row)
                item.setdefault("competitor", key)
                rows.append(item)
    rows = rows[-50:]
    statuses = {str(progress.get("run_status") or progress.get("status") or "") for progress in progresses if progress}
    has_all_progress = len([progress for progress in progresses if progress]) == len(competitors)
    terminal_statuses = {"completed", "completed_with_warnings", "failed", "stopped_blocked", "stopped_challenge", "cancelled"}
    job_status_value = str(metadata.get("status") or "")
    if job_status_value == "cancelled":
        # The job was cancelled at the top level, which can happen even when a
        # competitor never reported back (an unresponsive Browser Helper). That
        # must win over whatever the per-competitor progress files still say.
        status = "cancelled"
    elif not has_all_progress or any(status not in terminal_statuses for status in statuses):
        status = "running"
    elif any(status in {"failed", "stopped_blocked", "stopped_challenge"} for status in statuses):
        status = "failed" if "failed" in statuses else sorted(statuses)[0]
    elif "cancelled" in statuses:
        status = "cancelled"
    elif all(str(progress.get("run_status") or progress.get("status") or "") in {"completed", "completed_with_warnings"} for progress in progresses):
        status = "completed_with_warnings" if "completed_with_warnings" in statuses else "completed"
    else:
        status = "running"
    return {
        "status": status,
        "run_status": status,
        "competitor": "multiple" if len(competitors) > 1 else (competitors[0] if competitors else ""),
        "competitors": competitors,
        "total": total or int(metadata.get("planned_count") or 0) * max(1, len(competitors)),
        "completed": completed,
        "remaining": remaining if total else max(0, int(metadata.get("planned_count") or 0) * max(1, len(competitors)) - completed),
        "eta_seconds": max(eta_values) if eta_values else None,
        "last_attempted_part": next((str(progress.get("last_attempted_part")) for progress in reversed(progresses) if progress.get("last_attempted_part")), None),
        "rows": rows,
    }


def _can_launch_visible_browser() -> bool:
    if os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _should_retry_visible(progress: dict[str, object]) -> bool:
    status = str(progress.get("run_status") or progress.get("status") or "")
    rows = progress.get("rows")
    completed = int(progress.get("completed") or 0)
    first_result = ""
    if isinstance(rows, list) and rows:
        first = rows[0]
        if isinstance(first, dict):
            first_result = str(first.get("result_type") or "")
    return completed <= 1 and (status in {"stopped_blocked", "stopped_challenge"} or first_result in {"blocked", "challenge"})


def _completion_message(return_code: int, progress: dict[str, object]) -> str:
    status = str(progress.get("run_status") or progress.get("status") or "")
    if status == "completed":
        return "Collection completed."
    if status == "completed_with_warnings":
        return "Collection completed with warnings."
    if status == "stopped_blocked":
        return "Collection stopped because Partzilla returned a block response."
    if status == "stopped_challenge":
        return "Collection stopped because Partzilla returned a challenge page."
    if return_code != 0:
        return f"Collector exited with code {return_code}."
    return status or "Collection finished."
