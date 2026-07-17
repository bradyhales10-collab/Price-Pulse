from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from app.config import DEFAULT_DATABASE_PATH
from app.database import initialize_database
from app.web.app import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local Part Pulse dashboard.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    initialize_database(args.database)
    app = create_app(args.database)
    print("Part Pulse is running:")
    print(f"http://{args.host}:{args.port}")
    print(f"Database: {args.database}")
    print("Press Ctrl+C to stop.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
