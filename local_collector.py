from __future__ import annotations

import argparse
import base64
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import getpass
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from app.competitors.registry import get_competitor
from app.database import connect_database, initialize_database, seed_competitor, upsert_competitor_listing, upsert_product_and_listing
from app.input_loader import load_parts_csv
from app.manufacturer_registry import competitor_supports_manufacturer


ROOT = Path(__file__).resolve().parent
BRIDGE_DIR = ROOT / "data" / "output" / "local_bridge"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run price checks locally and upload results to the Part Pulse server.")
    parser.add_argument("--server-url", required=True, help="Example: http://141.148.156.56")
    parser.add_argument("--import-batch-id", type=int, required=True)
    parser.add_argument("--competitor", action="append", required=True, help="Use once per competitor, such as --competitor partzilla --competitor motosport")
    parser.add_argument("--username", help="Part Pulse web username if the server uses basic login.")
    parser.add_argument("--password", help="Part Pulse web password. If omitted, you will be prompted.")
    parser.add_argument("--delay-seconds", type=int, default=1)
    parser.add_argument("--collection-mode", choices=["full_browser", "lightweight_browser"], default="full_browser")
    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument("--visible", action="store_true", help="Open visible browser windows (the default).")
    browser_group.add_argument("--headless", action="store_true", help="Run without visible browser windows. This may be blocked by competitors.")
    parser.add_argument("--sequential", action="store_true", help="Check competitors one at a time instead of concurrently.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    password = args.password
    if args.username and password is None:
        password = getpass.getpass("Part Pulse password: ")
    auth_header = _auth_header(args.username, password)
    server_url = args.server_url.rstrip("/")
    input_path = BRIDGE_DIR / f"import-{args.import_batch_id}-collector-input.csv"
    print(f"Downloading import {args.import_batch_id} from Part Pulse...")
    _download(f"{server_url}/collector/imports/{args.import_batch_id}/input.csv", input_path, auth_header)
    max_parts = _count_csv_rows(input_path)
    if max_parts == 0:
        print("No parts were found in that import.")
        return 1
    print(f"Downloaded {max_parts} parts.")
    run_token = int(time.time() * 1000)
    jobs = []
    for index, competitor in enumerate(args.competitor):
        local_db = BRIDGE_DIR / f"collector-{args.import_batch_id}-{run_token}-{competitor}.db"
        run_id_floor = (run_token * 10) + (index * 2)
        expected_run_id = prepare_local_database(input_path, local_db, [competitor], run_id_floor=run_id_floor)
        jobs.append((competitor, local_db, expected_run_id))

    failures: list[str] = []
    if len(jobs) == 1 or args.sequential:
        for job in jobs:
            try:
                _collect_and_upload(job, input_path, max_parts, args, server_url, auth_header)
            except Exception as exc:
                failures.append(f"{job[0]}: {exc}")
                print(f"{job[0]} failed: {exc}")
    else:
        print(f"Opening {len(jobs)} competitor browser windows and checking them at the same time...")
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = {
                executor.submit(_collect_and_upload, job, input_path, max_parts, args, server_url, auth_header): job[0]
                for job in jobs
            }
            for future in as_completed(futures):
                competitor = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    failures.append(f"{competitor}: {exc}")
                    print(f"{competitor} failed: {exc}")
    if failures:
        print("One or more competitors did not finish:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Done. Refresh Price Check in Part Pulse to see the updated comparison.")
    return 0


def _collect_and_upload(
    job: tuple[str, Path, int],
    input_path: Path,
    max_parts: int,
    args: argparse.Namespace,
    server_url: str,
    auth_header: str | None,
) -> None:
    competitor, local_db, expected_run_id = job
    print(f"Checking {competitor} locally...")
    summary = _run_competitor(input_path, local_db, max_parts, competitor, args, expected_run_id=expected_run_id)
    print(f"Uploading {competitor} results...")
    result = _upload(
        f"{server_url}/collector/results/upload?{urllib.parse.urlencode({'competitor': competitor, 'filename': summary.name})}",
        summary,
        auth_header,
    )
    print(f"{competitor}: {result}")


def _auth_header(username: str | None, password: str | None) -> str | None:
    if not username:
        return None
    token = base64.b64encode(f"{username}:{password or ''}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _download(url: str, destination: Path, auth_header: str | None) -> None:
    request = urllib.request.Request(url)
    if auth_header:
        request.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            destination.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Download failed: HTTP {exc.code}") from exc


def _upload(url: str, path: Path, auth_header: str | None) -> str:
    request = urllib.request.Request(url, data=path.read_bytes(), method="POST")
    request.add_header("Content-Type", "text/csv")
    request.add_header("X-Filename", path.name)
    if auth_header:
        request.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Upload failed: HTTP {exc.code} {detail}") from exc


def _count_csv_rows(path: Path) -> int:
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        return sum(1 for _ in csv.DictReader(file))


def prepare_local_database(input_path: Path, local_db: Path, competitors: list[str], *, run_id_floor: int | None = None) -> int:
    load_result = load_parts_csv(input_path)
    initialize_database(local_db)
    with connect_database(local_db) as conn:
        for record in load_result.records:
            product_id, _, _, _ = upsert_product_and_listing(conn, record)
            for competitor in competitors:
                competitor_id = seed_competitor(conn, competitor)
                try:
                    canonical_url = get_competitor(competitor).build_product_url(record) if competitor_supports_manufacturer(competitor, record.manufacturer) else ""
                except Exception:
                    canonical_url = ""
                upsert_competitor_listing(
                    conn,
                    product_id=product_id,
                    competitor_id=competitor_id,
                    competitor_part_number=record.oem_part_number,
                    canonical_url=canonical_url,
                )
        if run_id_floor is not None:
            competitor_id = seed_competitor(conn, competitors[0])
            conn.execute(
                """
                INSERT INTO scan_runs(scan_run_id, competitor_id, started_at, completed_at, requested_part_count, run_status)
                VALUES (?, ?, '1970-01-01T00:00:00Z', '1970-01-01T00:00:00Z', 0, 'completed')
                """,
                (run_id_floor, competitor_id),
            )
            return run_id_floor + 1
    return 1


def _run_competitor(
    input_path: Path,
    local_db: Path,
    max_parts: int,
    competitor: str,
    args: argparse.Namespace,
    *,
    expected_run_id: int,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> Path:
    progress_file = BRIDGE_DIR / f"progress-{expected_run_id}-{competitor}.json"
    progress_file.unlink(missing_ok=True)
    command = [
        sys.executable,
        "collect_parts.py",
        "--file",
        str(input_path),
        "--max-parts",
        str(max_parts),
        "--save-to-database",
        "--database",
        str(local_db),
        "--competitor",
        competitor,
        "--collection-mode",
        args.collection_mode,
        "--delay-seconds",
        str(args.delay_seconds),
        "--yes",
        "--progress-file",
        str(progress_file),
    ]
    if args.headless:
        command.append("--headless")
    process = subprocess.Popen(command, cwd=ROOT)
    last_progress = ""
    while process.poll() is None:
        last_progress = _forward_progress(progress_file, progress_callback, last_progress)
        time.sleep(0.75)
    last_progress = _forward_progress(progress_file, progress_callback, last_progress)
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command)
    summary = ROOT / "data" / "output" / "collection_runs" / str(expected_run_id) / "collection_summary.csv"
    if not summary.exists():
        raise RuntimeError(f"{competitor} did not create a collection_summary.csv file.")
    return summary


def _forward_progress(
    progress_file: Path,
    callback: Callable[[dict[str, object]], None] | None,
    previous_serialized: str,
) -> str:
    if callback is None or not progress_file.exists():
        return previous_serialized
    try:
        serialized = progress_file.read_text(encoding="utf-8")
        if serialized == previous_serialized:
            return previous_serialized
        payload = json.loads(serialized)
    except (OSError, json.JSONDecodeError):
        return previous_serialized
    callback(payload)
    return serialized


if __name__ == "__main__":
    raise SystemExit(main())
