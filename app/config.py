from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
PRIVATE_DIR = DATA_DIR / "private"
VALIDATION_DIR = DATA_DIR / "validation"
DATABASE_DIR = DATA_DIR / "database"
DEFAULT_DATABASE_PATH = DATABASE_DIR / "pricing_monitor.db"
DIAGNOSTICS_DIR = OUTPUT_DIR / "diagnostics"
AUTHENTICATED_DIAGNOSTICS_DIR = OUTPUT_DIR / "authenticated_diagnostics"
SCREENSHOTS_DIR = OUTPUT_DIR / "screenshots"
HTML_DIR = OUTPUT_DIR / "html"
LOGS_DIR = OUTPUT_DIR / "logs"
PARTZILLA_AUTH_STATE_PATH = PRIVATE_DIR / "partzilla_auth_state.json"
STEP3_VALIDATION_MANIFEST = VALIDATION_DIR / "step3_validation_parts.csv"
STEP3_VALIDATION_SUMMARY = OUTPUT_DIR / "step3_validation_summary.csv"
STEP3_VALIDATION_REVIEW = OUTPUT_DIR / "step3_validation_review.txt"
AUTHENTICATED_VALIDATION_MANIFEST = VALIDATION_DIR / "authenticated_validation_parts.csv"
AUTHENTICATED_VALIDATION_SUMMARY = OUTPUT_DIR / "authenticated_validation_summary.csv"
AUTHENTICATED_VALIDATION_REVIEW = OUTPUT_DIR / "authenticated_validation_review.txt"

DEFAULT_INPUT_CSV = INPUT_DIR / "Partzilla_Kawasaki_Test_Parts.csv"
DEFAULT_VIEWPORT = {"width": 1366, "height": 900}
DEFAULT_RENDER_SETTLE_MS = 3000


@dataclass(frozen=True)
class ProbeSettings:
    headless: bool = False
    slow_mo: int = 0
    timeout: int = 30000
    render_settle_ms: int = DEFAULT_RENDER_SETTLE_MS


def ensure_data_directories() -> None:
    """Create expected runtime data folders."""
    for path in (
        INPUT_DIR,
        OUTPUT_DIR,
        PRIVATE_DIR,
        DATABASE_DIR,
        VALIDATION_DIR,
        DIAGNOSTICS_DIR,
        AUTHENTICATED_DIAGNOSTICS_DIR,
        SCREENSHOTS_DIR,
        HTML_DIR,
        LOGS_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
