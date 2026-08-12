"""Move Price Pulse data to another computer.

The code comes from GitHub, but the data deliberately does not: the database
and the saved sign-ins are excluded from the repository, and the sign-ins are
credentials. So a fresh checkout starts empty. This packages what a second
machine needs into one file, and restores it there.

On the computer that has the data:
    .venv\\Scripts\\python.exe move_to_another_computer.py backup

On the other computer, after cloning and running Start Part Pulse.cmd once:
    .venv\\Scripts\\python.exe move_to_another_computer.py restore <zip file>
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "data" / "database" / "pricing_monitor.db"
PRIVATE_DIR = ROOT / "data" / "private"
INPUT_DIR = ROOT / "data" / "input"


def backup() -> int:
    if not DATABASE.exists():
        print(f"No database found at {DATABASE}")
        print("Upload a parts file and run a price check first.")
        return 1

    name = f"PricePulse_Data_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.zip"
    destination = ROOT / name

    included: list[str] = []
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(DATABASE, "data/database/pricing_monitor.db")
        included.append(f"database ({DATABASE.stat().st_size / 1_000_000:.1f} MB)")

        if PRIVATE_DIR.exists():
            for path in PRIVATE_DIR.rglob("*"):
                if path.is_file():
                    archive.write(path, str(Path("data/private") / path.relative_to(PRIVATE_DIR)))
            included.append("saved sign-ins")

        if INPUT_DIR.exists():
            for path in INPUT_DIR.glob("*.csv"):
                archive.write(path, f"data/input/{path.name}")
            included.append("input files")

    print("=" * 62)
    print("  Data packaged")
    print("=" * 62)
    print("")
    for item in included:
        print(f"  included: {item}")
    print("")
    print(f"File: {destination}")
    print(f"Size: {destination.stat().st_size / 1_000_000:.1f} MB")
    print("")
    print("This contains your saved sign-ins, so treat it like a password:")
    print("copy it directly to the other computer rather than emailing it.")
    return 0


def restore(archive_path: Path) -> int:
    if not archive_path.exists():
        print(f"No such file: {archive_path}")
        return 1

    if DATABASE.exists():
        backup_name = DATABASE.with_suffix(f".db.replaced-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
        shutil.copy2(DATABASE, backup_name)
        print(f"This computer already had a database. Kept a copy at:\n  {backup_name}\n")

    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(ROOT)
        restored = len(archive.namelist())

    print("=" * 62)
    print("  Data restored")
    print("=" * 62)
    print("")
    print(f"  {restored} file(s) restored from {archive_path.name}")
    print(f"  database: {'yes' if DATABASE.exists() else 'MISSING'}")
    print(f"  sign-ins: {'yes' if PRIVATE_DIR.exists() else 'none included'}")
    print("")
    print('Now double-click "Start Part Pulse.cmd" and open the dashboard.')
    print("Your parts, prices and review decisions should all be there.")
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"backup", "restore"}:
        print(__doc__)
        return 1
    if sys.argv[1] == "backup":
        return backup()
    if len(sys.argv) < 3:
        print("Usage: move_to_another_computer.py restore <zip file>")
        return 1
    return restore(Path(sys.argv[2]))


if __name__ == "__main__":
    sys.exit(main())
