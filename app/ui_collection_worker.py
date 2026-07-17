from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from app.database import utc_now


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a UI-launched collection job and update job metadata.")
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--input-file", type=Path, required=True)
    parser.add_argument("--delay-seconds", type=int, required=True)
    parser.add_argument("--collection-mode", default="full_browser")
    parser.add_argument("--competitor", action="append", default=["partzilla"])
    args = parser.parse_args()

    job_json = args.job_dir / "job.json"
    metadata = _read_metadata(job_json)
    metadata["worker_pid"] = _current_pid()
    metadata["status"] = "running"
    metadata["updated_at"] = utc_now()
    _write_metadata(job_json, metadata)

    command = [
        sys.executable,
        "-u",
        "collect_parts.py",
        "--file",
        str(args.input_file),
        "--max-parts",
        str(metadata.get("planned_count", 0)),
        "--save-to-database",
        "--database",
        str(args.database),
        "--competitor",
        args.competitor[0],
        "--collection-mode",
        args.collection_mode,
        "--delay-seconds",
        str(args.delay_seconds),
        "--yes",
    ]

    metadata["collector_command"] = command
    metadata["updated_at"] = utc_now()
    _write_metadata(job_json, metadata)

    stdout_path = args.job_dir / "stdout.log"
    stderr_path = args.job_dir / "stderr.log"
    try:
        with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open("a", encoding="utf-8") as stderr:
            process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], stdout=stdout, stderr=stderr)
            metadata["collector_pid"] = process.pid
            metadata["updated_at"] = utc_now()
            _write_metadata(job_json, metadata)
            return_code = process.wait()
    except Exception as exc:
        metadata["status"] = "failed"
        metadata["finished_at"] = utc_now()
        metadata["message"] = str(exc)
        _write_metadata(job_json, metadata)
        return 1

    metadata["return_code"] = return_code
    metadata["finished_at"] = utc_now()
    metadata["status"] = "completed" if return_code == 0 else "failed"
    metadata["message"] = _last_log_line(stderr_path) or _last_log_line(stdout_path) or f"Collector exited with code {return_code}."
    _write_metadata(job_json, metadata)
    return return_code


def _read_metadata(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_metadata(path: Path, metadata: dict[str, object]) -> None:
    path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def _last_log_line(path: Path) -> str:
    if not path.exists():
        return ""
    lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _current_pid() -> int:
    import os

    return os.getpid()


if __name__ == "__main__":
    raise SystemExit(main())
