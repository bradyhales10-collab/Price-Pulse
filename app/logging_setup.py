from __future__ import annotations

import logging
import sys
from pathlib import Path

from app.config import LOGS_DIR, ensure_data_directories


def setup_logging(log_file: Path | None = None) -> Path:
    ensure_data_directories()
    resolved_log_file = log_file or LOGS_DIR / "partzilla_probe.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(resolved_log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )

    return resolved_log_file
