from __future__ import annotations

import argparse
import base64
import csv
import getpass
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


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
    parser.add_argument("--collection-mode", choices=["full_browser", "lightweight_browser"], default="lightweight_browser")
    parser.add_argument("--visible", action="store_true", help="Open a visible browser while checking prices.")
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
    local_db = BRIDGE_DIR / f"collector-{args.import_batch_id}-{int(time.time())}.db"
    for competitor in args.competitor:
        print(f"Checking {competitor} locally...")
        summary = _run_competitor(input_path, local_db, max_parts, competitor, args)
        print(f"Uploading {competitor} results...")
        result = _upload(
            f"{server_url}/collector/results/upload?{urllib.parse.urlencode({'competitor': competitor, 'filename': summary.name})}",
            summary,
            auth_header,
        )
        print(result)
    print("Done. Refresh Price Check in Part Pulse to see the updated comparison.")
    return 0


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
        raise SystemExit(f"Upload failed: HTTP {exc.code} {detail}") from exc


def _count_csv_rows(path: Path) -> int:
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        return sum(1 for _ in csv.DictReader(file))


def _run_competitor(input_path: Path, local_db: Path, max_parts: int, competitor: str, args: argparse.Namespace) -> Path:
    before = _summary_files()
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
    ]
    if not args.visible:
        command.append("--headless")
    subprocess.run(command, cwd=ROOT, check=True)
    after = [path for path in _summary_files() if path not in before]
    if not after:
        after = _summary_files()
    if not after:
        raise SystemExit(f"{competitor} did not create a collection_summary.csv file.")
    return max(after, key=lambda path: path.stat().st_mtime)


def _summary_files() -> list[Path]:
    return list((ROOT / "data" / "output" / "collection_runs").glob("*/collection_summary.csv"))


if __name__ == "__main__":
    raise SystemExit(main())
