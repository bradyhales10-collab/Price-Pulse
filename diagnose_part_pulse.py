"""Report what is and is not running, in plain English.

Written after a price check sat on "waiting" with no browser opening. That
symptom has one common cause, the Browser Helper not running, but nothing on
screen said so. This checks each piece in turn and names the fix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.collection_jobs import JOB_DIR, local_agent_status
from app.config import DATA_DIR, DEFAULT_DATABASE_PATH

ROOT = Path(__file__).resolve().parent
AGENT_CONFIG = DATA_DIR / "private" / "local_collector_agent.json"
STUCK_AFTER_SECONDS = 60


def _line(ok: bool, label: str, detail: str = "") -> None:
    mark = "  OK  " if ok else "NOT OK"
    print(f"  [{mark}] {label}")
    if detail:
        print(f"           {detail}")


def _code_version() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=ROOT,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _playwright_browser_installed() -> bool:
    """Playwright keeps downloaded browsers under the user's local app data."""
    candidates = [
        Path.home() / "AppData" / "Local" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ]
    for base in candidates:
        if base.exists() and any(base.glob("chromium*")):
            return True
    return False


def _queued_jobs() -> list[tuple[str, dict]]:
    if not JOB_DIR.exists():
        return []
    jobs: list[tuple[str, dict]] = []
    for job_json in sorted(JOB_DIR.glob("*/job.json")):
        try:
            jobs.append((job_json.parent.name, json.loads(job_json.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return jobs


def _age_seconds(value: str) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return (datetime.now(UTC) - stamp.astimezone(UTC)).total_seconds()


def _running_part_pulse_processes() -> list[dict[str, str]]:
    """Every python.exe process whose console window is titled like a Part
    Pulse process, with its PID and how long it has been running.

    Uses tasklist's window-title matching rather than WMI's CommandLine
    property. CommandLine can silently come back blank on a locked-down
    machine without administrator rights, which would make an old, stray
    process invisible to this check precisely when it matters most. tasklist
    does not have that restriction, and it is exactly what Start/Repair/Stop
    Part Pulse.cmd use to find these same processes to end them.
    """
    if os.name != "nt":
        return []
    found: list[dict[str, str]] = []
    for title in ("Part Pulse Browser Helper", "Part Pulse Dashboard"):
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"WINDOWTITLE eq {title}*", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            continue
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if len(lines) < 2:
            continue  # Header only, or tasklist reported no matching process.
        for line in lines[1:]:
            fields = [field.strip('"') for field in line.split('","')]
            if len(fields) < 2:
                continue
            found.append({"title": title, "image": fields[0].strip('"'), "pid": fields[1]})
    return found


def main() -> int:
    print("===============================")
    print("  Part Pulse Check")
    print("===============================")
    print("")
    print(f"  Program version: {_code_version()}")
    print("")

    problems: list[str] = []

    # 1. Browser Helper set up at all.
    if AGENT_CONFIG.exists():
        _line(True, "Browser Helper is set up on this computer")
    else:
        _line(False, "Browser Helper has never been set up")
        problems.append('Double-click "Setup Part Pulse Collector.cmd" once.')

    # 2. Browser Helper actually running right now.
    status = local_agent_status()
    if status.get("connected"):
        _line(True, "Browser Helper is running and connected")
    else:
        last_seen = str(status.get("last_seen") or "")
        age = _age_seconds(last_seen) if AGENT_CONFIG.exists() else None
        if age is None:
            detail = "It has not connected since this database was created."
        elif age < 3600:
            detail = f"Last seen about {int(age / 60)} minutes ago."
        else:
            detail = f"Last seen about {int(age / 3600)} hours ago."
        _line(False, "Browser Helper is NOT running", detail)
        problems.append('Double-click "Start Part Pulse.cmd". This is the usual fix.')

    # 3. The controlled browser is downloaded.
    if _playwright_browser_installed():
        _line(True, "The browser Part Pulse controls is installed")
    else:
        _line(False, "The browser Part Pulse controls is missing")
        problems.append('Double-click "Repair Part Pulse.cmd" to reinstall it.')

    # 4. Database present.
    if DEFAULT_DATABASE_PATH.exists():
        _line(True, f"Database found ({DEFAULT_DATABASE_PATH.name})")
    else:
        _line(False, "No database yet")
        problems.append("Upload a parts file in Part Pulse first.")

    # 5. Jobs waiting with nobody to run them.
    waiting = [
        (job_id, meta)
        for job_id, meta in _queued_jobs()
        if meta.get("status") == "queued_local"
    ]
    if not waiting:
        _line(True, "No price checks are stuck waiting")
    else:
        stale = []
        for job_id, meta in waiting:
            age = _age_seconds(meta.get("updated_at") or meta.get("created_at") or "")
            if age is None or age > STUCK_AFTER_SECONDS:
                stale.append((job_id, age))
        if stale:
            job_id, age = stale[0]
            detail = f"Job {job_id} has been waiting"
            if age is not None:
                detail += f" about {int(age / 60)} minutes"
            detail += "."
            _line(False, f"{len(stale)} price check(s) stuck waiting", detail)
            problems.append(
                "A waiting price check means no Browser Helper picked it up. "
                'Start Part Pulse, then press "Start Checking Prices" again.'
            )
        else:
            _line(True, "A price check was just queued and should start shortly")

    # 6. Every production collector's code can actually find what it calls.
    # A NameError like this only shows up when the collector actually runs
    # mid price-check; checking the referenced names here catches it before
    # that, without needing a live browser.
    try:
        import collect_parts

        broken = []
        for key, collector in collect_parts.PRODUCTION_COLLECTORS.items():
            code = getattr(collector, "__code__", None)
            if code is None:
                broken.append(f"{key}: not a function")
                continue
            missing = [
                name
                for name in code.co_names
                if name not in collect_parts.__dict__ and name not in dir(__builtins__)
            ]
            if missing:
                broken.append(f"{key}: cannot find {', '.join(missing)}")
        if broken:
            _line(False, "Some competitors are not wired up correctly", "; ".join(broken))
            problems.append(
                'Double-click "Repair Part Pulse.cmd". This resets the program files from GitHub '
                "and clears any stale cached code, which is the usual cause of this."
            )
        else:
            _line(True, "All competitors are wired up correctly")
    except Exception as exc:
        _line(False, "Could not check the price-check code", str(exc))
        problems.append('Double-click "Repair Part Pulse.cmd".')

    # 7. Part Pulse processes actually running right now.
    processes = _running_part_pulse_processes()
    if os.name == "nt":
        helper_running = any(proc["title"] == "Part Pulse Browser Helper" for proc in processes)
        dashboard_running = any(proc["title"] == "Part Pulse Dashboard" for proc in processes)
        if helper_running and dashboard_running:
            _line(True, "Browser Helper and Dashboard windows are both running")
        elif helper_running or dashboard_running:
            missing = "Dashboard" if helper_running else "Browser Helper"
            _line(False, f"The {missing} window is not running")
            problems.append('Double-click "Start Part Pulse.cmd" to start both pieces together.')
        else:
            _line(False, "Neither the Browser Helper nor Dashboard window is running")
            problems.append('Double-click "Start Part Pulse.cmd".')
        print(
            "           (If a price check still seems stuck on old behavior even after this looks "
            'healthy, run "Repair Part Pulse.cmd" - it now reliably closes and restarts both, '
            "rather than depending on reading another process's command line, which some machines "
            "restrict.)"
        )

    print("")
    print("===============================")
    if problems:
        print("  What to do")
        print("===============================")
        print("")
        for index, fix in enumerate(dict.fromkeys(problems), start=1):
            print(f"  {index}. {fix}")
    else:
        print("  Everything looks healthy.")
        print("===============================")
        print("")
        print("  If a price check still will not start, send this window")
        print("  to Claude along with what you clicked.")
    print("")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
