from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import DEFAULT_DATABASE_PATH
from app.database import SCHEMA_VERSION, connect_database, initialize_database, table_counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize the Part Pulse SQLite database.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    args = parser.parse_args()
    initialize_database(args.database)
    with connect_database(args.database) as conn:
        counts = table_counts(conn)
    print(f"Database path: {args.database}")
    print(f"Schema version: {SCHEMA_VERSION}")
    for table, count in counts.items():
        print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
